"""Container backend schema definitions and enums."""

from enum import StrEnum

from pydantic import BaseModel, Field

# Kubernetes container name used in Job pod specs.
CONTAINER_NAME = "worker"

# Sidecar container name used when sandbox mode (security-proxy) is enabled.
SECURITY_PROXY_CONTAINER_NAME = "security-proxy"

# In-pod loopback the agent must dial for any outbound HTTPS.
SECURITY_PROXY_LOOPBACK = "http://127.0.0.1:8080"

# Mount points inside the sidecar / agent containers. Config and CA live in
# separate directories so K8s can mount them independently — putting both
# under /etc/security-proxy/ causes a subPath/directory mount collision.
SECURITY_PROXY_CONFIG_DIR = "/etc/security-proxy-config"
SECURITY_PROXY_CONFIG_MOUNT = "/etc/security-proxy-config/config.json"
SECURITY_PROXY_CA_DIR_SIDECAR = "/etc/security-proxy"  # cert + key
SECURITY_PROXY_CA_PATH_AGENT = "/etc/security-proxy/ca.crt"  # cert only

# Non-secret value placed on the agent for every sidecar-owned credential
# env var. Tools that gate on "is auth configured?" via env see something
# truthy; the proxy strips and replaces the real Authorization / x-api-key
# headers on egress, so this string never crosses the wire. The CLI also
# recognises it (``PLACEHOLDER_TOKENS``) and treats it as "no token".
SANDBOX_PLACEHOLDER = "sandbox-proxy-injected"


class DockerEventAction(StrEnum):
    """Docker container event actions."""

    START = "start"
    DIE = "die"
    STOP = "stop"
    KILL = "kill"
    CREATE = "create"


class ContainerDockerStatus(StrEnum):
    """Docker container status values."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    RESTARTING = "restarting"
    EXITED = "exited"
    DEAD = "dead"


class WatchEventType(StrEnum):
    """Kubernetes Watch API event types."""

    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"


class JobConditionType(StrEnum):
    """Kubernetes Job condition types."""

    COMPLETE = "Complete"
    FAILED = "Failed"


class ConditionStatus(StrEnum):
    """Kubernetes condition status values."""

    TRUE = "True"
    FALSE = "False"
    UNKNOWN = "Unknown"


class ProxySpec(BaseModel):
    """Sandbox configuration for an execution.

    When set on a ``ContainerRequest``, the K8s backend launches the agent
    alongside a security-proxy sidecar that holds all credentials and is the
    only process with internet egress. The agent container receives no
    credential env vars and is pointed at the sidecar via ``HTTPS_PROXY``.

    Attributes:
        config_json: JSON string of the proxy config (with ``${VAR}``
            placeholders that the sidecar resolves against its own env).
            Mounted into the sidecar as a ConfigMap volume.
        ca_cert_pem: PEM-encoded CA certificate. Mounted into both
            containers; the agent's HTTP clients trust this CA.
        ca_key_pem: PEM-encoded CA private key. Mounted into the sidecar
            only; used to sign leaf certs on the fly.
        secret_env: Credential env vars destined for the sidecar — never
            placed on the agent container.
        image: Container image for the sidecar binary.
    """

    config_json: str
    ca_cert_pem: str
    ca_key_pem: str
    secret_env: dict[str, str] = Field(default_factory=dict)
    image: str
