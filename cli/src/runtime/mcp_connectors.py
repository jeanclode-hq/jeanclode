"""Org-registered MCP servers — pure client, never hosted or spawned.

The backend resolves each org's MCP servers + credentials at dispatch time
and hands the CLI a ``JEANCLODE_MCP_SERVERS`` JSON payload (see
``add_connectors_to_inputs`` in ``backend/api/plugins/container/dispatch_inputs.py``,
https://github.com/jeanclode-hq/jeanclode/issues/14). The CLI's only job is
to turn each entry into an ``McpHttpServerConfig`` for the SDK — same
mental model as adding a remote server in Claude Code itself: paste a URL
+ auth, connect out, never host anything.
"""

from __future__ import annotations

import json
import logging
import os

from claude_agent_sdk.types import McpHttpServerConfig
from pydantic import BaseModel

logger = logging.getLogger(__name__)

MCP_SERVERS_ENV_VAR = "JEANCLODE_MCP_SERVERS"

# Mirrors backend/api/plugins/container/kubernetes.py:SANDBOX_PLACEHOLDER.
# The literal value sent here is irrelevant — the security-proxy sidecar
# strips and replaces Authorization for any matched upstream regardless of
# what the agent sends, so this only has to look like a plausible header.
_SANDBOX_PLACEHOLDER = "sandbox-proxy-injected"


class McpServerSpec(BaseModel):
    """One registered MCP server, as resolved by the backend."""

    name: str
    url: str
    header: str | None = None
    auth_scheme: str | None = None  # "bearer" | "basic" | "raw" | None (unauthenticated)

    def to_sdk_config(self) -> McpHttpServerConfig:
        config: McpHttpServerConfig = {"type": "http", "url": self.url}
        if self.header and self.auth_scheme:
            if self.auth_scheme == "bearer":
                value = f"Bearer {_SANDBOX_PLACEHOLDER}"
            elif self.auth_scheme == "basic":
                value = f"Basic {_SANDBOX_PLACEHOLDER}"
            else:
                value = _SANDBOX_PLACEHOLDER
            config["headers"] = {self.header: value}
        return config


def load_mcp_servers_from_env(env: dict[str, str] | None = None) -> list[McpServerSpec]:
    """Read ``JEANCLODE_MCP_SERVERS`` (a JSON array) into specs.

    Malformed entries are skipped with a warning rather than failing the
    whole run — one bad connector shouldn't take down the agent.
    """
    raw = (env or os.environ).get(MCP_SERVERS_ENV_VAR, "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON, ignoring", MCP_SERVERS_ENV_VAR)
        return []

    specs: list[McpServerSpec] = []
    for entry in entries:
        try:
            specs.append(McpServerSpec.model_validate(entry))
        except Exception:
            logger.warning("skipping malformed MCP server entry: %r", entry, exc_info=True)
    return specs
