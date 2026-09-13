"""Configuration for the container plugin."""

from typing import Literal

from pydantic import BaseModel, Field


class WatcherConfig(BaseModel):
    """Configuration for the container watcher."""

    enabled: bool = Field(default=True, description="Run watcher in this process")
    reconcile_interval: int = Field(default=60, description="Seconds between reconciliation scans")


class ScheduledDispatchConfig(BaseModel):
    """Configuration for the SCHEDULED-execution redispatch poller (ADR-010)."""

    enabled: bool = Field(default=True, description="Run the redispatch poller in this process")
    interval_seconds: int = Field(default=30, description="Seconds between redispatch scans")
    batch_size: int = Field(default=20, description="Max SCHEDULED executions claimed per tick")


class DockerConfig(BaseModel):
    """Docker-specific configuration."""

    socket: str = Field(default="/var/run/docker.sock", description="Path to Docker socket")
    image: str = Field(default="jeanclode/cli:latest", description="Docker image for workers")
    network: str | None = Field(default=None, description="Docker network to attach containers to")
    memory_limit: str = Field(default="2g", description="Memory limit for containers")
    cpu_limit: float = Field(default=2.0, description="CPU limit for containers")
    timeout: int = Field(default=600, description="Maximum execution time in seconds")
    cleanup_containers: bool = Field(default=True, description="Delete containers after completion")
    security_proxy_image: str = Field(
        default="jeanclode/security-proxy:latest",
        description="Container image for the security-proxy sidecar.",
    )


class KubernetesConfig(BaseModel):
    """Kubernetes-specific configuration."""

    namespace: str = Field(description="Kubernetes namespace for jobs")
    image: str = Field(default="jeanclode/cli:latest", description="Container image for workers")
    service_account: str = Field(description="Service account for pods")
    timeout: int = Field(default=600, description="Maximum execution time in seconds")
    memory_limit: str = Field(default="2Gi", description="Memory limit for pods")
    memory_request: str = Field(default="512Mi", description="Memory request for pods")
    cpu_limit: str = Field(default="2", description="CPU limit for pods")
    cpu_request: str = Field(default="500m", description="CPU request for pods")
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = Field(default="IfNotPresent")
    image_pull_secrets: list[str] = Field(default_factory=list)
    node_selector: dict[str, str] = Field(default_factory=dict)
    tolerations: list[dict[str, str]] = Field(default_factory=list)
    annotations: dict[str, str] = Field(default_factory=dict)
    run_as_user: int = Field(default=1000, description="UID to run the pod as")
    run_as_group: int = Field(default=1000, description="GID to run the pod as")
    cleanup_jobs: bool = Field(default=True, description="Delete Jobs after completion")
    security_proxy_image: str = Field(
        default="jeanclode/security-proxy:latest",
        description="Container image for the security-proxy sidecar.",
    )


class ContainerPluginConfig(BaseModel):
    """Configuration for the container plugin."""

    enabled: bool = Field(default=False, description="Enable the container plugin")
    backend: Literal["docker", "kubernetes"] = Field(default="docker")
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    docker: DockerConfig | None = Field(default_factory=DockerConfig)
    kubernetes: KubernetesConfig | None = Field(default=None)
    scheduled_dispatch: ScheduledDispatchConfig = Field(default_factory=ScheduledDispatchConfig)
