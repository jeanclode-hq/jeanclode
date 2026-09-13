"""Tests for the security-proxy build helpers."""

import json

import pytest
from cryptography import x509

from api.plugins.container.security_proxy import (
    CredentialKey,
    UpstreamCredential,
    build_proxy_spec,
    mint_ca,
)


def _llm() -> UpstreamCredential:
    return UpstreamCredential(
        secret_key=CredentialKey.CLAUDE_CODE_OAUTH_TOKEN,
        host="api.anthropic.com",
        header="Authorization",
        bearer=True,
    )


def _github() -> UpstreamCredential:
    return UpstreamCredential(
        secret_key=CredentialKey.GH_TOKEN,
        host="api.github.com",
        header="Authorization",
        bearer=True,
    )


def _sentry(host: str) -> UpstreamCredential:
    return UpstreamCredential(
        secret_key=CredentialKey.SENTRY_AUTH_TOKEN,
        host=host,
        header="Authorization",
        bearer=True,
    )


def test_build_proxy_spec_emits_only_provided_upstreams() -> None:
    spec = build_proxy_spec(
        secret_env={"GH_TOKEN": "ghs"},
        upstreams=[_github()],
        image="img",
        execution_id="exec-1",
    )
    config = json.loads(spec.config_json)
    assert config["execution_id"] == "exec-1"
    assert [u["host"] for u in config["upstreams"]] == ["api.github.com"]
    assert config["upstreams"][0]["inject"] == {"Authorization": "Bearer ${GH_TOKEN}"}


def test_build_proxy_spec_resolves_per_org_self_hosted_sentry() -> None:
    """Self-hosted Sentry case — host comes from the org row, not a default."""
    spec = build_proxy_spec(
        secret_env={"SENTRY_AUTH_TOKEN": "tok"},
        upstreams=[_sentry("sentry.acme.example")],
        image="img",
        execution_id="exec-1",
    )
    upstreams = json.loads(spec.config_json)["upstreams"]
    assert upstreams == [
        {"host": "sentry.acme.example", "inject": {"Authorization": "Bearer ${SENTRY_AUTH_TOKEN}"}}
    ]


def test_build_proxy_spec_does_not_inline_real_secret_values() -> None:
    """The on-disk config must reference env vars, never embed the value."""
    spec = build_proxy_spec(
        secret_env={"ANTHROPIC_API_KEY": "sk-ant-real-secret-value"},
        upstreams=[
            UpstreamCredential(
                secret_key="ANTHROPIC_API_KEY",
                host="api.anthropic.com",
                header="x-api-key",
            )
        ],
        image="img",
        execution_id="exec-1",
    )
    assert "sk-ant-real-secret-value" not in spec.config_json
    assert spec.secret_env["ANTHROPIC_API_KEY"] == "sk-ant-real-secret-value"


def test_build_proxy_spec_coalesces_two_creds_on_same_host() -> None:
    """Anthropic with both x-api-key and Authorization → one upstream entry."""
    spec = build_proxy_spec(
        secret_env={"ANTHROPIC_API_KEY": "sk", "CLAUDE_CODE_OAUTH_TOKEN": "oauth"},
        upstreams=[
            UpstreamCredential(
                secret_key="ANTHROPIC_API_KEY",
                host="api.anthropic.com",
                header="x-api-key",
            ),
            _llm(),
        ],
        image="img",
        execution_id="exec-1",
    )
    upstreams = json.loads(spec.config_json)["upstreams"]
    assert len(upstreams) == 1
    assert upstreams[0]["inject"] == {
        "Authorization": "Bearer ${CLAUDE_CODE_OAUTH_TOKEN}",
        "x-api-key": "${ANTHROPIC_API_KEY}",
    }


def test_build_proxy_spec_with_no_upstreams_denies_everything() -> None:
    """An execution with no allowed hosts produces an empty allowlist."""
    spec = build_proxy_spec(
        secret_env={},
        upstreams=[],
        image="img",
        execution_id="exec-1",
    )
    config = json.loads(spec.config_json)
    assert config["upstreams"] == []
    assert spec.secret_env == {}


def test_build_proxy_spec_carries_unrelated_secret_env_through() -> None:
    """A secret with no upstream entry stays on the sidecar but isn't auto-injected."""
    spec = build_proxy_spec(
        secret_env={"GH_TOKEN": "ghs", "SOMETHING_ELSE": "x"},
        upstreams=[_github()],
        image="img",
        execution_id="exec-1",
    )
    assert spec.secret_env == {"GH_TOKEN": "ghs", "SOMETHING_ELSE": "x"}
    upstreams = {u["host"] for u in json.loads(spec.config_json)["upstreams"]}
    assert upstreams == {"api.github.com"}


def test_path_prefix_entries_on_same_host_are_kept_separate() -> None:
    """Two GitLab orgs on the same host with different prefixes → two upstream objects."""
    spec = build_proxy_spec(
        secret_env={"GITLAB_TOKEN_0": "tok0", "GITLAB_TOKEN_1": "tok1"},
        upstreams=[
            UpstreamCredential(
                secret_key="GITLAB_TOKEN_0",
                host="gitlab.com",
                header="PRIVATE-TOKEN",
                path_prefix="/groupA/",
            ),
            UpstreamCredential(
                secret_key="GITLAB_TOKEN_1",
                host="gitlab.com",
                header="PRIVATE-TOKEN",
                path_prefix="/groupB/",
            ),
        ],
        image="img",
        execution_id="exec-1",
    )
    upstreams = json.loads(spec.config_json)["upstreams"]
    assert len(upstreams) == 2
    prefixes = {u["path_prefix"] for u in upstreams}
    assert prefixes == {"/groupA/", "/groupB/"}


def test_path_prefix_same_host_same_prefix_coalesces_headers() -> None:
    """PRIVATE-TOKEN and Authorization on the same (host, prefix) → one entry."""
    spec = build_proxy_spec(
        secret_env={"GITLAB_TOKEN_0": "tok", "GITLAB_GIT_AUTH_0": "basic"},
        upstreams=[
            UpstreamCredential(
                secret_key="GITLAB_TOKEN_0",
                host="gitlab.com",
                header="PRIVATE-TOKEN",
                path_prefix="/groupA/",
            ),
            UpstreamCredential(
                secret_key="GITLAB_GIT_AUTH_0",
                host="gitlab.com",
                header="Authorization",
                path_prefix="/groupA/",
            ),
        ],
        image="img",
        execution_id="exec-1",
    )
    upstreams = json.loads(spec.config_json)["upstreams"]
    assert len(upstreams) == 1
    assert upstreams[0]["path_prefix"] == "/groupA/"
    assert set(upstreams[0]["inject"].keys()) == {"PRIVATE-TOKEN", "Authorization"}


def test_path_prefix_absent_for_no_prefix_entries() -> None:
    """GitHub and LLM entries without path_prefix emit no path_prefix field."""
    spec = build_proxy_spec(
        secret_env={"GH_TOKEN": "ghs"},
        upstreams=[_github()],
        image="img",
        execution_id="exec-1",
    )
    upstreams = json.loads(spec.config_json)["upstreams"]
    assert len(upstreams) == 1
    assert "path_prefix" not in upstreams[0]


@pytest.mark.parametrize("execution_id", ["exec-short", "x" * 80])
def test_mint_ca_produces_valid_self_signed_ca(execution_id: str) -> None:
    cert_pem, key_pem = mint_ca(execution_id=execution_id)
    assert "BEGIN CERTIFICATE" in cert_pem
    assert (
        "BEGIN " + "PRIVATE KEY" in key_pem
    )  # split avoids detect-private-key pre-commit false positive

    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    basic_constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic_constraints.value.ca is True

    key_usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert key_usage.value.key_cert_sign is True


def test_mint_ca_yields_unique_certs_per_call() -> None:
    a, _ = mint_ca(execution_id="exec-a")
    b, _ = mint_ca(execution_id="exec-b")
    assert a != b
