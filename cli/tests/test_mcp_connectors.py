"""JEANCLODE_MCP_SERVERS env parsing + McpServerSpec → SDK config (#191)."""

import json

from src.runtime.mcp_connectors import McpServerSpec, load_mcp_servers_from_env


def test_load_mcp_servers_from_env_empty_returns_empty() -> None:
    assert load_mcp_servers_from_env({}) == []


def test_load_mcp_servers_from_env_parses_entries() -> None:
    payload = json.dumps(
        [
            {
                "name": "outline",
                "url": "https://mcp.outline.com/sse",
                "header": "Authorization",
                "auth_scheme": "bearer",
            },
            {"name": "public-tool", "url": "https://mcp.public.example.com/sse"},
        ]
    )
    specs = load_mcp_servers_from_env({"JEANCLODE_MCP_SERVERS": payload})

    assert len(specs) == 2
    assert specs[0].name == "outline"
    assert specs[0].auth_scheme == "bearer"
    assert specs[1].header is None


def test_load_mcp_servers_from_env_malformed_json_returns_empty() -> None:
    assert load_mcp_servers_from_env({"JEANCLODE_MCP_SERVERS": "not json"}) == []


def test_load_mcp_servers_from_env_skips_malformed_entry_keeps_others() -> None:
    payload = json.dumps(
        [
            {"name": "ok", "url": "https://a.example.com"},
            {"url": "https://b.example.com"},  # missing required "name"
        ]
    )
    specs = load_mcp_servers_from_env({"JEANCLODE_MCP_SERVERS": payload})

    assert len(specs) == 1
    assert specs[0].name == "ok"


def test_to_sdk_config_bearer_scheme() -> None:
    spec = McpServerSpec(
        name="outline",
        url="https://mcp.outline.com/sse",
        header="Authorization",
        auth_scheme="bearer",
    )
    config = spec.to_sdk_config()
    assert config == {
        "type": "http",
        "url": "https://mcp.outline.com/sse",
        "headers": {"Authorization": "Bearer sandbox-proxy-injected"},
    }


def test_to_sdk_config_basic_scheme() -> None:
    spec = McpServerSpec(
        name="legacy",
        url="https://mcp.legacy.example.com",
        header="Authorization",
        auth_scheme="basic",
    )
    config = spec.to_sdk_config()
    assert config["headers"]["Authorization"] == "Basic sandbox-proxy-injected"


def test_to_sdk_config_raw_scheme() -> None:
    spec = McpServerSpec(
        name="raw-key", url="https://mcp.raw.example.com", header="X-Api-Key", auth_scheme="raw"
    )
    config = spec.to_sdk_config()
    assert config["headers"]["X-Api-Key"] == "sandbox-proxy-injected"


def test_to_sdk_config_unauthenticated_has_no_headers() -> None:
    spec = McpServerSpec(name="public", url="https://mcp.public.example.com")
    config = spec.to_sdk_config()
    assert config == {"type": "http", "url": "https://mcp.public.example.com"}
