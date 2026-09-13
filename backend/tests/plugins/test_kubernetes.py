"""Tests for the KubernetesBackend."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.plugins.container.backend import ContainerRequest
from api.plugins.container.config import KubernetesConfig
from api.plugins.container.kubernetes import (
    ConfigMap,
    KubernetesBackend,
    NotFoundError,
    Secret,
    _job_uid,
    _ResourceNames,
    _sanitize_label_value,
)
from api.plugins.container.schema import (
    CONTAINER_NAME,
    SECURITY_PROXY_CA_PATH_AGENT,
    SECURITY_PROXY_CONTAINER_NAME,
    ProxySpec,
)
from api.plugins.container.security_proxy import CredentialKey, UpstreamCredential
from api.plugins.container.utils import LABEL_EXECUTION_ID, LABEL_PLUGIN


def _names(job_name: str = "jc-test") -> _ResourceNames:
    return _ResourceNames.derive(job_name)


def _stub_proxy_spec(secret_env: dict[str, str] | None = None) -> ProxySpec:
    return ProxySpec(
        config_json='{"execution_id":"e","upstreams":[]}',
        ca_cert_pem="<stub-cert-pem>",
        ca_key_pem="<stub-key-pem>",
        secret_env=secret_env or {},
        image="jeanclode/security-proxy:latest",
    )


def _build(
    backend: KubernetesBackend,
    request: ContainerRequest | None = None,
    *,
    job_name: str = "jc-test",
    public_env: dict[str, str] | None = None,
    proxy: ProxySpec | None = None,
) -> dict:
    """Test helper — invokes _build_job_manifest with sandbox defaults.

    If ``public_env`` is omitted, the request's full env is treated as
    public so the existing assertions about ``envFrom`` continue to work
    without each test having to repeat the credential split.
    """
    req = request or _make_request()
    return backend._build_job_manifest(
        job_name,
        _names(job_name),
        req,
        public_env=public_env if public_env is not None else dict(req.env),
        proxy=proxy or _stub_proxy_spec(),
    )


def _make_config(**overrides: object) -> KubernetesConfig:
    defaults = {
        "namespace": "jeanclode",
        "service_account": "jc-runner",
        "image": "jeanclode/cli:latest",
        "timeout": 600,
    }
    defaults.update(overrides)  # type: ignore[arg-type]
    return KubernetesConfig(**defaults)  # type: ignore[arg-type]


def _make_request(**overrides: object) -> ContainerRequest:
    defaults = {
        "image": "test-image:latest",
        "command": ["echo", "hello"],
        "env": {"FOO": "bar"},
        "timeout_seconds": 60,
        "labels": {
            LABEL_EXECUTION_ID: "exec-123",
            LABEL_PLUGIN: "test",
        },
    }
    defaults.update(overrides)  # type: ignore[arg-type]
    return ContainerRequest(**defaults)  # type: ignore[arg-type]


def _make_backend(config: KubernetesConfig | None = None) -> KubernetesBackend:
    """Create a KubernetesBackend with mocked broker and redis."""
    broker = AsyncMock()
    redis = AsyncMock()
    return KubernetesBackend("test", config or _make_config(), broker, redis)


# ---------------------------------------------------------------------------
# Label sanitization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("simple", "simple"),
        ("has spaces", "has-spaces"),
        ("has/slash", "has-slash"),
        ("---leading", "leading"),
        ("trailing---", "trailing"),
        ("a" * 100, "a" * 63),
        ("", ""),
    ],
)
def test_sanitize_label_value(value: str, expected: str) -> None:
    assert _sanitize_label_value(value) == expected


# ---------------------------------------------------------------------------
# Job manifest building
# ---------------------------------------------------------------------------


def test_build_job_manifest_basic() -> None:
    backend = _make_backend()
    request = _make_request()
    manifest = _build(backend, request, job_name="jc-test-exec-123")

    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["namespace"] == "jeanclode"
    assert manifest["metadata"]["name"] == "jc-test-exec-123"

    spec = manifest["spec"]
    assert spec["backoffLimit"] == 0
    assert spec["activeDeadlineSeconds"] == 60

    container = spec["template"]["spec"]["containers"][0]
    assert container["name"] == CONTAINER_NAME
    assert container["image"] == "test-image:latest"
    assert container["args"] == ["echo", "hello"]
    assert container["imagePullPolicy"] == "IfNotPresent"

    # Env vars come from Secret, not inline. Secret name is derived from job_name.
    assert container["envFrom"] == [{"secretRef": {"name": "jc-test-exec-123-secrets"}}]
    # Sandbox-always: the agent now also gets HTTPS_PROXY etc. inline.
    assert any(e["name"] == "HTTPS_PROXY" for e in container.get("env", []))


def test_build_job_manifest_no_secret() -> None:
    """When the request carries no env, the agent gets no envFrom mount."""
    backend = _make_backend()
    manifest = _build(backend, _make_request(env={}))
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert "envFrom" not in container


def test_build_job_manifest_security_hardening() -> None:
    """Container and pod security contexts are properly set."""
    backend = _make_backend()
    manifest = _build(backend)

    # Container-level security
    container_sc = manifest["spec"]["template"]["spec"]["containers"][0]["securityContext"]
    assert container_sc["readOnlyRootFilesystem"] is True
    assert container_sc["allowPrivilegeEscalation"] is False

    # Pod-level security
    pod_sc = manifest["spec"]["template"]["spec"]["securityContext"]
    assert pod_sc["runAsNonRoot"] is True
    assert pod_sc["runAsUser"] == 1000
    assert pod_sc["runAsGroup"] == 1000


def test_build_job_manifest_tmp_volume() -> None:
    """emptyDir /tmp volume is present for read-only root filesystem, sized per request."""
    backend = _make_backend()
    request = _make_request(tmp_size_limit="3Gi")
    manifest = _build(backend, request)

    pod_spec = manifest["spec"]["template"]["spec"]
    volumes = pod_spec["volumes"]
    assert {"name": "tmp", "emptyDir": {"sizeLimit": "3Gi"}} in volumes

    container = pod_spec["containers"][0]
    assert {"name": "tmp", "mountPath": "/tmp"} in container["volumeMounts"]


def test_build_job_manifest_tmp_volume_falls_back_without_request_size() -> None:
    """A request that doesn't set ``tmp_size_limit`` still gets a bounded emptyDir."""
    backend = _make_backend()
    manifest = _build(backend, _make_request())

    volumes = manifest["spec"]["template"]["spec"]["volumes"]
    assert {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}} in volumes


def test_build_job_manifest_resources() -> None:
    config = _make_config(
        memory_limit="4Gi",
        memory_request="1Gi",
        cpu_limit="4",
        cpu_request="1",
    )
    backend = _make_backend(config)
    manifest = _build(backend)

    resources = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert resources["limits"]["memory"] == "4Gi"
    assert resources["limits"]["cpu"] == "4"
    assert resources["requests"]["memory"] == "1Gi"
    assert resources["requests"]["cpu"] == "1"


def test_build_job_manifest_service_account() -> None:
    backend = _make_backend()
    manifest = _build(backend)
    assert manifest["spec"]["template"]["spec"]["serviceAccountName"] == "jc-runner"


def test_build_job_manifest_image_pull_secrets() -> None:
    config = _make_config(image_pull_secrets=["regcred", "other-secret"])
    backend = _make_backend(config)
    manifest = _build(backend)

    secrets = manifest["spec"]["template"]["spec"]["imagePullSecrets"]
    assert secrets == [{"name": "regcred"}, {"name": "other-secret"}]


def test_build_job_manifest_node_selector() -> None:
    config = _make_config(node_selector={"gpu": "true"})
    backend = _make_backend(config)
    manifest = _build(backend)
    assert manifest["spec"]["template"]["spec"]["nodeSelector"] == {"gpu": "true"}


def test_build_job_manifest_tolerations() -> None:
    toleration = {"key": "dedicated", "operator": "Equal", "value": "jc", "effect": "NoSchedule"}
    config = _make_config(tolerations=[toleration])
    backend = _make_backend(config)
    manifest = _build(backend)
    assert manifest["spec"]["template"]["spec"]["tolerations"] == [toleration]


def test_build_job_manifest_annotations() -> None:
    config = _make_config(annotations={"iam.amazonaws.com/role": "my-role"})
    backend = _make_backend(config)
    manifest = _build(backend)
    assert manifest["spec"]["template"]["metadata"]["annotations"] == {
        "iam.amazonaws.com/role": "my-role"
    }


def test_build_job_manifest_cleanup_ttl() -> None:
    config = _make_config(cleanup_jobs=True)
    backend = _make_backend(config)
    manifest = _build(backend)
    assert "ttlSecondsAfterFinished" in manifest["spec"]


def test_build_job_manifest_no_cleanup_ttl() -> None:
    config = _make_config(cleanup_jobs=False)
    backend = _make_backend(config)
    manifest = _build(backend)
    assert "ttlSecondsAfterFinished" not in manifest["spec"]


def test_build_job_manifest_no_command() -> None:
    backend = _make_backend()
    manifest = _build(backend, _make_request(command=[]))
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert "args" not in container


def test_build_job_manifest_plugin_label_injected() -> None:
    """Plugin label is injected into request.labels before building manifest."""
    backend = _make_backend()
    request = _make_request()
    request.labels[LABEL_PLUGIN] = "test"
    manifest = _build(backend, request)
    sanitized_key = _sanitize_label_value(LABEL_PLUGIN)
    assert sanitized_key in manifest["metadata"]["labels"]


# ---------------------------------------------------------------------------
# Sandbox mode (security-proxy sidecar)
# ---------------------------------------------------------------------------


def _make_proxy_spec(**overrides: object) -> ProxySpec:
    # Real PEM markers are stripped to avoid tripping pre-commit's private-key
    # detector — these are placeholders, never used cryptographically.
    defaults: dict[str, object] = {
        "config_json": '{"execution_id":"e","upstreams":[]}',
        "ca_cert_pem": "<stub-cert-pem>",
        "ca_key_pem": "<stub-key-pem>",
        "secret_env": {"ANTHROPIC_API_KEY": "sk-ant-x", "GH_TOKEN": "ghs-y"},
        "image": "jeanclode/security-proxy:latest",
    }
    defaults.update(overrides)
    return ProxySpec(**defaults)  # type: ignore[arg-type]


def _sandboxed_manifest() -> dict:
    """Sandbox is unconditional, so this is just `_build` with realistic env.

    Public-side env stays on the agent; credential-side env lives on the
    sidecar via the stub ProxySpec.
    """
    backend = _make_backend()
    request = _make_request(env={"LOG_LEVEL": "info"})
    return _build(
        backend,
        request,
        job_name="jc-test-exec-123",
        public_env={"LOG_LEVEL": "info"},
        proxy=_make_proxy_spec(),
    )


def _agent_container(manifest: dict) -> dict:
    for c in manifest["spec"]["template"]["spec"]["containers"]:
        if c["name"] == CONTAINER_NAME:
            return c
    raise AssertionError("agent container missing")


def _sidecar_container(manifest: dict) -> dict:
    """The proxy is a native K8s sidecar — initContainer with restartPolicy=Always."""
    pod = manifest["spec"]["template"]["spec"]
    for c in pod.get("initContainers", []):
        if c["name"] == SECURITY_PROXY_CONTAINER_NAME:
            return c
    raise AssertionError("sidecar container missing")


def test_sandbox_pod_has_agent_and_native_sidecar() -> None:
    manifest = _sandboxed_manifest()
    pod = manifest["spec"]["template"]["spec"]
    container_names = {c["name"] for c in pod["containers"]}
    init_names = {c["name"] for c in pod.get("initContainers", [])}
    assert container_names == {CONTAINER_NAME}
    assert init_names == {SECURITY_PROXY_CONTAINER_NAME}
    sidecar = _sidecar_container(manifest)
    # Native sidecar markers — kubelet keeps it running for the agent's lifetime
    # and only releases the agent once the startupProbe passes.
    assert sidecar["restartPolicy"] == "Always"
    # Exec probe (not tcpSocket): mitmdump binds to 127.0.0.1 only, and
    # kubelet's tcpSocket probes hit the pod IP, which can never reach a
    # loopback listener. Exec runs inside the container.
    assert "exec" in sidecar["startupProbe"]
    assert "127.0.0.1" in " ".join(sidecar["startupProbe"]["exec"]["command"])


def test_sandbox_agent_does_not_reference_the_proxy_secret() -> None:
    """The agent's envFrom must never reference the credential-bearing proxy Secret.

    Sandbox-always means request.secrets is held by the sidecar; if the agent
    ever envFrom-mounted the proxy Secret, the credential isolation would
    be defeated.
    """
    manifest = _sandboxed_manifest()
    agent = _agent_container(manifest)

    secret_refs = {entry["secretRef"]["name"] for entry in agent.get("envFrom", [])}
    assert all(not ref.endswith("-pxs") for ref in secret_refs)


def test_sandbox_sidecar_owns_credential_envs() -> None:
    manifest = _sandboxed_manifest()
    sidecar = _sidecar_container(manifest)
    secret_refs = [entry["secretRef"]["name"] for entry in sidecar["envFrom"]]
    # Exactly one entry, which is the proxy-secrets Secret.
    assert len(secret_refs) == 1
    assert secret_refs[0].endswith("-pxs")


def test_sandbox_agent_routes_through_loopback_proxy() -> None:
    manifest = _sandboxed_manifest()
    agent = _agent_container(manifest)
    env = {e["name"]: e["value"] for e in agent["env"]}
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:8080"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:8080"
    assert env["NODE_EXTRA_CA_CERTS"] == SECURITY_PROXY_CA_PATH_AGENT
    assert env["SSL_CERT_FILE"] == SECURITY_PROXY_CA_PATH_AGENT
    assert env["REQUESTS_CA_BUNDLE"] == SECURITY_PROXY_CA_PATH_AGENT


def test_sandbox_agent_mounts_only_ca_cert_not_key() -> None:
    """Agent gets cert-only via items projection; private key is sidecar-only."""
    manifest = _sandboxed_manifest()
    pod = manifest["spec"]["template"]["spec"]
    agent = _agent_container(manifest)

    agent_ca_volume = next(v for v in pod["volumes"] if v["name"] == "security-proxy-ca")
    assert agent_ca_volume["secret"]["items"] == [{"key": "ca.crt", "path": "ca.crt"}]

    sidecar_priv_volume = next(v for v in pod["volumes"] if v["name"] == "security-proxy-ca-priv")
    assert "items" not in sidecar_priv_volume["secret"]

    agent_mounts = {m["name"] for m in agent["volumeMounts"]}
    assert "security-proxy-ca-priv" not in agent_mounts


def test_sandbox_pod_disables_service_account_token() -> None:
    manifest = _sandboxed_manifest()
    assert manifest["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


def test_real_secret_values_never_appear_on_agent() -> None:
    """End-to-end: passing secrets via ContainerRequest doesn't leak the
    real values onto the agent. The env var *name* is seeded with a
    non-secret placeholder so tools that hard-require it at startup work;
    the proxy strips and replaces the corresponding header on egress, so
    the placeholder is also never sent upstream."""
    backend = _make_backend()
    request = _make_request(
        env={"LOG_LEVEL": "info"},
        secrets={"GH_TOKEN": "ghs-real-secret"},
        upstreams=[
            UpstreamCredential(
                secret_key=CredentialKey.GH_TOKEN,
                host="api.github.com",
                header="Authorization",
                bearer=True,
            )
        ],
    )
    manifest = _build(backend, request, public_env={"LOG_LEVEL": "info"}, proxy=_make_proxy_spec())
    agent = _agent_container(manifest)

    # Real value must be absent from every env entry.
    for entry in agent.get("env", []):
        assert entry.get("value") != "ghs-real-secret"
    # Agent's envFrom must not pull from the proxy/sidecar secret.
    for entry in agent.get("envFrom", []):
        assert not entry["secretRef"]["name"].endswith("-pxs")


def test_sandbox_sidecar_drops_capabilities_and_runs_readonly() -> None:
    manifest = _sandboxed_manifest()
    sc = _sidecar_container(manifest)["securityContext"]
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"] == {"drop": ["ALL"]}


# ---------------------------------------------------------------------------
# start_container (with Secret creation)
# ---------------------------------------------------------------------------


@patch("api.plugins.container.kubernetes.ConfigMap")
@patch("api.plugins.container.kubernetes.Secret")
@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_start_container_creates_all_sandbox_resources(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
    mock_secret_cls: MagicMock,
    mock_configmap_cls: MagicMock,
) -> None:
    """Sandbox-always: every dispatch creates agent secret + proxy secret + CA secret + ConfigMap."""
    mock_api_fn.return_value = AsyncMock()

    mock_job = AsyncMock()
    mock_job.name = "jc-test-exec-123"
    mock_job.metadata = MagicMock()
    mock_job.metadata.uid = "fake-uid"
    mock_job.create = AsyncMock()
    mock_job_cls.return_value = mock_job

    mock_secret = AsyncMock()
    mock_secret.create = AsyncMock()
    mock_secret_cls.return_value = mock_secret
    mock_secret_cls.get = AsyncMock(return_value=AsyncMock(patch=AsyncMock()))

    mock_configmap = AsyncMock()
    mock_configmap.create = AsyncMock()
    mock_configmap_cls.return_value = mock_configmap
    mock_configmap_cls.get = AsyncMock(return_value=AsyncMock(patch=AsyncMock()))

    mock_redis = AsyncMock()
    mock_redis.sadd = AsyncMock(return_value=1)
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(), mock_broker, mock_redis)

    container_id = await backend.start_container("exec-123", _make_request())

    assert container_id == "jc-test-exec-123"
    mock_job.create.assert_awaited_once()
    # 3 Secrets always: agent, proxy, CA. ConfigMap always: proxy config.
    assert mock_secret.create.await_count == 3
    mock_configmap.create.assert_awaited_once()
    mock_redis.sadd.assert_awaited_once()
    mock_broker.publish.assert_awaited_once()


@patch("api.plugins.container.kubernetes.ConfigMap")
@patch("api.plugins.container.kubernetes.Secret")
@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_start_container_skips_agent_secret_when_no_public_env(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
    mock_secret_cls: MagicMock,
    mock_configmap_cls: MagicMock,
) -> None:
    """No agent Secret when the request has no non-secret env. Sandbox resources still created."""
    mock_api_fn.return_value = AsyncMock()

    mock_job = AsyncMock()
    mock_job.name = "jc-test-exec-123"
    mock_job.metadata = MagicMock()
    mock_job.metadata.uid = "uid"
    mock_job_cls.return_value = mock_job

    mock_secret_cls.return_value = AsyncMock()
    mock_secret_cls.get = AsyncMock(return_value=AsyncMock(patch=AsyncMock()))
    mock_configmap_cls.return_value = AsyncMock()
    mock_configmap_cls.get = AsyncMock(return_value=AsyncMock(patch=AsyncMock()))

    backend = _make_backend()
    request = _make_request(env={})

    with patch.object(backend, "_create_secret", new=AsyncMock()) as mock_create_secret:
        await backend.start_container("exec-123", request)
        # 2 Secrets (proxy + CA), no agent secret.
        names_created = [call.args[0] for call in mock_create_secret.call_args_list]
        assert all(not n.endswith("-secrets") for n in names_created)
        assert any(n.endswith("-pxs") for n in names_created)
        assert any(n.endswith("-pxca") for n in names_created)


@patch("api.plugins.container.kubernetes.ConfigMap")
@patch("api.plugins.container.kubernetes.Secret")
@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_start_container_injects_plugin_label(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
    mock_secret_cls: MagicMock,
    mock_configmap_cls: MagicMock,
) -> None:
    mock_api_fn.return_value = AsyncMock()

    mock_job = AsyncMock()
    mock_job.name = "jc-test-exec-123"
    mock_job.metadata = MagicMock()
    mock_job.metadata.uid = "uid"
    mock_job_cls.return_value = mock_job

    mock_secret_cls.return_value = AsyncMock()
    mock_secret_cls.get = AsyncMock(return_value=AsyncMock(patch=AsyncMock()))
    mock_configmap_cls.return_value = AsyncMock()
    mock_configmap_cls.get = AsyncMock(return_value=AsyncMock(patch=AsyncMock()))

    backend = _make_backend()
    request = _make_request()

    await backend.start_container("exec-123", request)

    assert request.labels[LABEL_PLUGIN] == "test"


# ---------------------------------------------------------------------------
# stop_container
# ---------------------------------------------------------------------------


@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_stop_container_deletes_job(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
) -> None:
    mock_api_fn.return_value = AsyncMock()

    mock_job = AsyncMock()
    mock_job.delete = AsyncMock()
    mock_job_cls.get = AsyncMock(return_value=mock_job)

    backend = _make_backend()
    await backend.stop_container("jc-test-exec-123")

    mock_job_cls.get.assert_awaited_once_with(
        "jc-test-exec-123", namespace="jeanclode", api=backend._api
    )
    mock_job.delete.assert_awaited_once_with(propagation_policy="Background")


@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_stop_container_handles_missing_job(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
) -> None:
    mock_api_fn.return_value = AsyncMock()
    mock_job_cls.get = AsyncMock(side_effect=Exception("not found"))

    backend = _make_backend()
    await backend.stop_container("missing-job")


# ---------------------------------------------------------------------------
# Job terminal state detection
# ---------------------------------------------------------------------------


def test_get_job_terminal_state_complete() -> None:
    job_obj = MagicMock()
    job_obj.status = {"conditions": [{"type": "Complete", "status": "True"}]}
    backend = _make_backend()
    assert backend._get_job_terminal_state(job_obj) == 0


def test_get_job_terminal_state_failed() -> None:
    job_obj = MagicMock()
    job_obj.status = {"conditions": [{"type": "Failed", "status": "True"}]}
    backend = _make_backend()
    assert backend._get_job_terminal_state(job_obj) == 1


def test_get_job_terminal_state_running() -> None:
    job_obj = MagicMock()
    job_obj.status = {"active": 1}
    backend = _make_backend()
    assert backend._get_job_terminal_state(job_obj) is None


def test_get_job_terminal_state_condition_not_true() -> None:
    job_obj = MagicMock()
    job_obj.status = {"conditions": [{"type": "Complete", "status": "False"}]}
    backend = _make_backend()
    assert backend._get_job_terminal_state(job_obj) is None


# ---------------------------------------------------------------------------
# Job exit handling
# ---------------------------------------------------------------------------


@patch("api.plugins.container.kubernetes.Pod")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_handle_job_exit_success(
    mock_api_fn: MagicMock,
    mock_pod_cls: MagicMock,
) -> None:
    """Successful job exit publishes completed status."""
    mock_api_fn.return_value = AsyncMock()

    result_json = json.dumps({"pr_url": "https://github.com/org/repo/pull/1"})

    mock_pod = MagicMock()

    async def mock_logs(**kwargs):
        yield f"[JEANCLODE:RESULT] {result_json}"

    mock_pod.logs = mock_logs

    async def mock_pod_list(**kwargs):
        yield mock_pod

    mock_pod_cls.list = mock_pod_list

    mock_redis = AsyncMock()
    mock_redis.srem = AsyncMock(return_value=1)
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(cleanup_jobs=False), mock_broker, mock_redis)

    await backend._handle_job_exit("exec-123", "jc-test-exec-123", exit_code=0)

    mock_redis.srem.assert_awaited_once()
    mock_broker.publish.assert_awaited_once()
    call_kwargs = mock_broker.publish.call_args
    message = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("message")
    assert message["status"] == "completed"
    assert message["execution_id"] == "exec-123"


@patch("api.plugins.container.kubernetes.Pod")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_handle_job_exit_failure(
    mock_api_fn: MagicMock,
    mock_pod_cls: MagicMock,
) -> None:
    """Failed job exit publishes failed status."""
    mock_api_fn.return_value = AsyncMock()

    mock_pod = MagicMock()

    async def mock_logs(**kwargs):
        yield "some output"

    mock_pod.logs = mock_logs

    async def mock_pod_list(**kwargs):
        yield mock_pod

    mock_pod_cls.list = mock_pod_list

    mock_redis = AsyncMock()
    mock_redis.srem = AsyncMock(return_value=1)
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(cleanup_jobs=False), mock_broker, mock_redis)

    await backend._handle_job_exit("exec-456", "jc-test-exec-456", exit_code=1)

    mock_redis.srem.assert_awaited_once()
    mock_broker.publish.assert_awaited_once()
    call_kwargs = mock_broker.publish.call_args
    message = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("message")
    assert message["status"] == "failed"


@patch("api.plugins.container.kubernetes.Pod")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_handle_job_exit_idempotent_skip(
    mock_api_fn: MagicMock,
    mock_pod_cls: MagicMock,
) -> None:
    """Second exit for same execution is skipped via SREM gate."""
    mock_api_fn.return_value = AsyncMock()

    mock_pod = MagicMock()

    async def mock_logs(**kwargs):
        yield '[JEANCLODE:RESULT] {"status": "ok"}'

    mock_pod.logs = mock_logs

    async def mock_pod_list(**kwargs):
        yield mock_pod

    mock_pod_cls.list = mock_pod_list

    mock_redis = AsyncMock()
    mock_redis.srem = AsyncMock(return_value=0)
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(cleanup_jobs=False), mock_broker, mock_redis)

    await backend._handle_job_exit("exec-789", "jc-test-exec-789", exit_code=0)

    mock_redis.srem.assert_awaited_once()
    mock_broker.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# Job DELETED event handling
# ---------------------------------------------------------------------------


async def test_handle_job_deleted_publishes_failed() -> None:
    """DELETED event marks still-tracked execution as failed."""
    mock_redis = AsyncMock()
    mock_redis.srem = AsyncMock(return_value=1)
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(), mock_broker, mock_redis)

    await backend._handle_job_deleted("exec-del", "jc-test-exec-del")

    mock_redis.srem.assert_awaited_once()
    mock_broker.publish.assert_awaited_once()
    message = mock_broker.publish.call_args.args[0]
    assert message["status"] == "failed"
    assert message["error_type"] == "container_error"
    assert "deleted" in message["error_message"].lower()


async def test_handle_job_deleted_idempotent_skip() -> None:
    """DELETED event for already-processed execution is skipped."""
    mock_redis = AsyncMock()
    mock_redis.srem = AsyncMock(return_value=0)
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(), mock_broker, mock_redis)

    await backend._handle_job_deleted("exec-done", "jc-test-exec-done")

    mock_redis.srem.assert_awaited_once()
    mock_broker.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# Reconcile — Phase 1: ALL terminal Jobs (not just active set)
# ---------------------------------------------------------------------------


@patch("api.plugins.container.kubernetes.Pod")
@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_reconcile_phase1_processes_all_terminal_jobs(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
    mock_pod_cls: MagicMock,
) -> None:
    """Phase 1: a terminal Job whose execution can't be resolved as done is
    still force-published, even when it isn't in the active set."""
    mock_api_fn.return_value = AsyncMock()

    mock_job = MagicMock()
    mock_job.name = "jc-test-exec-100"
    mock_job.labels = {LABEL_EXECUTION_ID: "exec-100", LABEL_PLUGIN: "test"}
    mock_job.status = {
        "conditions": [{"type": "Complete", "status": "True"}],
    }

    async def mock_job_list(**kwargs):
        yield mock_job

    mock_job_cls.list = mock_job_list

    mock_pod = MagicMock()

    async def mock_pod_list(**kwargs):
        yield mock_pod

    mock_pod_cls.list = mock_pod_list

    async def mock_logs(**kwargs):
        yield '[JEANCLODE:RESULT] {"pr_url": "https://github.com/test/1"}'

    mock_pod.logs = mock_logs

    mock_redis = AsyncMock()
    # exec-100 is NOT in the active set — but reconcile should still process it
    mock_redis.smembers = AsyncMock(return_value=set())
    mock_redis.srem = AsyncMock()
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(cleanup_jobs=False), mock_broker, mock_redis)

    await backend.reconcile()

    # Should unregister and publish even though not in active set
    mock_redis.srem.assert_awaited()
    assert mock_broker.publish.await_count >= 1


@patch("api.plugins.container.kubernetes.Pod")
@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_reconcile_phase1_skips_already_resolved_executions(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
    mock_pod_cls: MagicMock,
) -> None:
    """Phase 1 skips a terminal Job whose execution is already resolved in the
    DB — no pod-log fetch, no status publish (this is what stops the every-60s
    reconcile from re-chewing a backlog of finished Jobs and draining the pool)."""
    mock_api_fn.return_value = AsyncMock()

    mock_job = MagicMock()
    mock_job.name = "jc-test-exec-done"
    mock_job.labels = {LABEL_EXECUTION_ID: "exec-done", LABEL_PLUGIN: "test"}
    mock_job.status = {"conditions": [{"type": "Complete", "status": "True"}]}

    async def mock_job_list(**kwargs):
        yield mock_job

    mock_job_cls.list = mock_job_list

    pod_logs_called = False

    async def mock_pod_list(**kwargs):
        nonlocal pod_logs_called
        pod_logs_called = True
        return
        yield  # pragma: no cover

    mock_pod_cls.list = mock_pod_list

    mock_redis = AsyncMock()
    mock_redis.smembers = AsyncMock(return_value=set())
    mock_redis.srem = AsyncMock()
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(cleanup_jobs=False), mock_broker, mock_redis)

    # DB says: none of the candidate executions are still pending → all resolved.
    with patch.object(backend, "executions_pending_reconcile", AsyncMock(return_value=set())):
        await backend.reconcile()

    assert not pod_logs_called
    mock_broker.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# Reconcile — Phase 2: orphans
# ---------------------------------------------------------------------------


@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_reconcile_phase2_handles_orphans(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
) -> None:
    """Phase 2: Executions in Redis but no Job → failed."""
    mock_api_fn.return_value = AsyncMock()

    async def mock_job_list(**kwargs):
        return
        yield  # make it an async generator

    mock_job_cls.list = mock_job_list

    mock_redis = AsyncMock()
    mock_redis.smembers = AsyncMock(return_value={b"orphan-exec"})
    mock_redis.srem = AsyncMock()
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(), mock_broker, mock_redis)

    await backend.reconcile()

    mock_redis.srem.assert_awaited_with("jeanclode:test:executions:active", "orphan-exec")
    mock_broker.publish.assert_awaited()
    message = mock_broker.publish.call_args.args[0]
    assert message["status"] == "failed"
    assert message["error_type"] == "container_error"


# ---------------------------------------------------------------------------
# Reconcile — Phase 3: stale DB
# ---------------------------------------------------------------------------


@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_reconcile_phase3_collects_running_ids(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
) -> None:
    """Phase 3: Running job exec IDs are passed to fail_stale_db_executions."""
    mock_api_fn.return_value = AsyncMock()

    running_job = MagicMock()
    running_job.name = "jc-test-running"
    running_job.labels = {LABEL_EXECUTION_ID: "running-exec", LABEL_PLUGIN: "test"}
    running_job.status = {"active": 1}

    async def mock_job_list(**kwargs):
        yield running_job

    mock_job_cls.list = mock_job_list

    mock_redis = AsyncMock()
    mock_redis.smembers = AsyncMock(return_value=set())
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(), mock_broker, mock_redis)
    backend.fail_stale_db_executions = AsyncMock()

    await backend.reconcile()

    backend.fail_stale_db_executions.assert_awaited_once_with({"running-exec"})


# ---------------------------------------------------------------------------
# Redis scoped keys
# ---------------------------------------------------------------------------


def test_scoped_redis_keys() -> None:
    backend = KubernetesBackend("sentry", _make_config(), AsyncMock(), AsyncMock())
    assert backend.active_executions_key == "jeanclode:sentry:executions:active"
    assert backend.reconcile_lock_key == "jeanclode:sentry:reconcile:lock"
    assert backend.status_stream == "jeanclode.sentry.execution.status"


# ---------------------------------------------------------------------------
# Name reuse across retries (a retried execution keeps its id)
# ---------------------------------------------------------------------------


def test_resource_names_children_cover_every_child() -> None:
    names = _names("jc-test-exec-123")
    children = {name: kind for kind, name in names.children()}

    assert set(children) == {
        names.agent_secret,
        names.proxy_secret,
        names.ca_secret,
        names.proxy_config,
    }
    assert children[names.proxy_config] is ConfigMap
    assert children[names.ca_secret] is Secret


@patch("api.plugins.container.kubernetes.ConfigMap")
@patch("api.plugins.container.kubernetes.Secret")
@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_start_container_purges_previous_attempt(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
    mock_secret_cls: MagicMock,
    mock_configmap_cls: MagicMock,
) -> None:
    """A redispatch under a reused id deletes the leftovers before creating."""
    mock_api_fn.return_value = AsyncMock()

    stale_job = AsyncMock()
    stale_job.delete = AsyncMock()
    # Present on the first lookup, gone once the delete has propagated.
    mock_job_cls.get = AsyncMock(side_effect=[stale_job, NotFoundError("gone")])

    stale_children = [AsyncMock() for _ in range(4)]
    mock_secret_cls.get = AsyncMock(side_effect=stale_children[:3])
    mock_configmap_cls.get = AsyncMock(side_effect=stale_children[3:])

    new_job = AsyncMock()
    new_job.name = "jc-test-exec-123"
    new_job.metadata = {"uid": "new-uid"}
    mock_job_cls.return_value = new_job
    mock_secret_cls.return_value = AsyncMock()
    mock_configmap_cls.return_value = AsyncMock()

    backend = _make_backend()
    # _adopt re-fetches the children it just created; the stale-object
    # side_effect lists above are already exhausted by the purge.
    backend._adopt = AsyncMock()

    await backend.start_container("exec-123", _make_request())

    stale_job.delete.assert_awaited_once_with(propagation_policy="Foreground")
    for child in stale_children:
        child.delete.assert_awaited_once()
    new_job.create.assert_awaited_once()


@patch("api.plugins.container.kubernetes.ConfigMap")
@patch("api.plugins.container.kubernetes.Secret")
@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_start_container_purge_is_a_noop_on_a_clean_name(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
    mock_secret_cls: MagicMock,
    mock_configmap_cls: MagicMock,
) -> None:
    """First dispatch of an execution deletes nothing."""
    mock_api_fn.return_value = AsyncMock()

    mock_job_cls.get = AsyncMock(side_effect=NotFoundError("gone"))
    mock_secret_cls.get = AsyncMock(side_effect=NotFoundError("gone"))
    mock_configmap_cls.get = AsyncMock(side_effect=NotFoundError("gone"))

    new_job = AsyncMock()
    new_job.name = "jc-test-exec-123"
    new_job.metadata = {"uid": "uid"}
    mock_job_cls.return_value = new_job
    mock_secret_cls.return_value = AsyncMock()
    mock_configmap_cls.return_value = AsyncMock()

    backend = _make_backend()
    backend._adopt = AsyncMock()

    await backend.start_container("exec-123", _make_request())

    new_job.create.assert_awaited_once()


@patch("api.plugins.container.kubernetes.ConfigMap")
@patch("api.plugins.container.kubernetes.Secret")
@patch("api.plugins.container.kubernetes.Job")
@patch("api.plugins.container.kubernetes.kr8s.asyncio.api")
async def test_start_container_records_the_new_job_uid(
    mock_api_fn: MagicMock,
    mock_job_cls: MagicMock,
    mock_secret_cls: MagicMock,
    mock_configmap_cls: MagicMock,
) -> None:
    mock_api_fn.return_value = AsyncMock()

    mock_job_cls.get = AsyncMock(side_effect=NotFoundError("gone"))
    mock_secret_cls.get = AsyncMock(side_effect=NotFoundError("gone"))
    mock_configmap_cls.get = AsyncMock(side_effect=NotFoundError("gone"))

    new_job = AsyncMock()
    new_job.name = "jc-test-exec-123"
    new_job.metadata = {"uid": "second-generation"}
    mock_job_cls.return_value = new_job
    mock_secret_cls.return_value = AsyncMock()
    mock_configmap_cls.return_value = AsyncMock()

    backend = _make_backend()
    backend._adopt = AsyncMock()

    await backend.start_container("exec-123", _make_request())

    backend._redis.set.assert_awaited_once()
    key, value = backend._redis.set.call_args.args
    assert key == "jeanclode:test:job-uid:exec-123"
    assert value == "second-generation"


async def test_purge_proceeds_when_the_old_job_will_not_die() -> None:
    """A Job that never disappears is logged and stepped over, not waited on."""
    backend = _make_backend()

    stale_job = AsyncMock()
    with (
        patch("api.plugins.container.kubernetes.Job") as mock_job_cls,
        patch("api.plugins.container.kubernetes._PURGE_TIMEOUT_SECONDS", 0.05),
        patch("api.plugins.container.kubernetes._PURGE_POLL_INTERVAL_SECONDS", 0.01),
    ):
        mock_job_cls.get = AsyncMock(return_value=stale_job)
        await backend._delete_job_and_wait("jc-test-exec-123", AsyncMock())

    stale_job.delete.assert_awaited_once_with(propagation_policy="Foreground")


async def test_handle_job_deleted_ignores_a_superseded_generation() -> None:
    """The purge's own DELETED event must not fail the run that replaced it."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=b"new-uid")
    mock_redis.srem = AsyncMock(return_value=1)
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(), mock_broker, mock_redis)

    await backend._handle_job_deleted("exec-del", "jc-test-exec-del", "old-uid")

    mock_redis.srem.assert_not_awaited()
    mock_broker.publish.assert_not_awaited()


async def test_handle_job_deleted_still_fails_the_current_generation() -> None:
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=b"current-uid")
    mock_redis.srem = AsyncMock(return_value=1)
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(), mock_broker, mock_redis)

    await backend._handle_job_deleted("exec-del", "jc-test-exec-del", "current-uid")

    mock_broker.publish.assert_awaited_once()
    assert mock_broker.publish.call_args.args[0]["status"] == "failed"


async def test_handle_job_deleted_fails_when_no_uid_was_recorded() -> None:
    """An unknown generation counts as current, not suppressed."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.srem = AsyncMock(return_value=1)
    mock_broker = AsyncMock()

    backend = KubernetesBackend("test", _make_config(), mock_broker, mock_redis)

    await backend._handle_job_deleted("exec-del", "jc-test-exec-del", "some-uid")

    mock_broker.publish.assert_awaited_once()


def test_job_uid_reads_metadata() -> None:
    job_obj = MagicMock()
    job_obj.metadata = {"uid": "abc-123"}
    assert _job_uid(job_obj) == "abc-123"


def test_job_uid_is_none_without_metadata() -> None:
    job_obj = MagicMock()
    job_obj.metadata = {}
    assert _job_uid(job_obj) is None
