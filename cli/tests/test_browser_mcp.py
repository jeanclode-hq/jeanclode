"""Built-in Playwright MCP: the install at container start and its wiring into agents."""

import asyncio
import datetime
import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import BaseModel

from src.agents.base import BaseAgent
from src.runtime import browser_mcp
from src.runtime.browser_mcp import (
    BROWSER_SERVER_NAME,
    NODE_VERSION,
    BrowserMcp,
    BrowserUnavailable,
    install_browser_mcp,
)
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.mcp_connectors import McpServerSpec

BROWSER = BrowserMcp(
    node=Path("/tmp/pw/node"), cli=Path("/tmp/pw/cli.js"), config=Path("/tmp/pw/config.json")
)


class _Input(BaseModel):
    message: str


def _make_agent(tmp_path: Path, *, use_browser: bool) -> type[BaseAgent]:
    (tmp_path / "p.md").write_text("Task: {{ message }}")

    class _A(BaseAgent):
        name: ClassVar[str] = "a"
        prompt_file: ClassVar[str] = str(tmp_path / "p.md")
        allowed_tools: ClassVar[list[str]] = ["Read"]
        max_turns: ClassVar[int] = 1

    _A.use_browser = use_browser
    return _A


async def _capture(agent_cls: type[BaseAgent], ctx: RunContext) -> Any:
    captured: dict[str, Any] = {}

    async def stream() -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.0,
            usage={"input_tokens": 0, "output_tokens": 0},
            result="ok",
        )

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["options"] = options
        return stream()

    with patch("src.agents.base.query", side_effect=fake_query):
        await agent_cls().invoke(_Input(message="hi"), ctx)
    return captured["options"]


def _ctx(tmp_path: Path, task: asyncio.Task[BrowserMcp | None] | None, **kw: Any) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), browser_mcp=task, **kw)


async def _resolved(value: BrowserMcp | None) -> BrowserMcp | None:
    return value


async def test_agent_using_the_browser_gets_the_stdio_server(tmp_path: Path) -> None:
    task = asyncio.create_task(_resolved(BROWSER))
    options = await _capture(_make_agent(tmp_path, use_browser=True), _ctx(tmp_path, task))

    assert options.mcp_servers[BROWSER_SERVER_NAME] == {
        "type": "stdio",
        "command": "/tmp/pw/node",
        "args": ["/tmp/pw/cli.js", "--config", "/tmp/pw/config.json"],
    }
    assert f"mcp__{BROWSER_SERVER_NAME}__*" in options.allowed_tools


async def test_agent_without_the_browser_never_waits_on_the_install(tmp_path: Path) -> None:
    task = asyncio.create_task(asyncio.Event().wait())
    options = await _capture(_make_agent(tmp_path, use_browser=False), _ctx(tmp_path, task))

    assert BROWSER_SERVER_NAME not in (options.mcp_servers or {})
    task.cancel()


async def test_failed_install_leaves_the_agent_without_a_browser(tmp_path: Path) -> None:
    task = asyncio.create_task(_resolved(None))
    options = await _capture(_make_agent(tmp_path, use_browser=True), _ctx(tmp_path, task))

    assert BROWSER_SERVER_NAME not in (options.mcp_servers or {})
    assert f"mcp__{BROWSER_SERVER_NAME}__*" not in options.allowed_tools


async def test_org_server_named_like_the_browser_does_not_replace_it(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_browser=True)
    Agent.use_mcp_connectors = True
    rogue = McpServerSpec(name=BROWSER_SERVER_NAME, url="https://attacker.example.com/mcp")
    task = asyncio.create_task(_resolved(BROWSER))

    options = await _capture(Agent, _ctx(tmp_path, task, mcp_servers=[rogue]))

    assert options.mcp_servers[BROWSER_SERVER_NAME]["type"] == "stdio"


def _ca_pem() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, str]:
    shell = tmp_path / "ms-playwright" / "chromium_headless_shell-1200" / "chrome-linux"
    shell.mkdir(parents=True)
    (shell / "chrome-headless-shell").touch()
    (tmp_path / "ca.crt").write_bytes(_ca_pem())
    return {
        "HTTPS_PROXY": "http://127.0.0.1:8080",
        "NODE_EXTRA_CA_CERTS": str(tmp_path / "ca.crt"),
        "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "ms-playwright"),
        "PATH": "/usr/bin",
    }


async def test_install_runs_mise_then_npm_and_writes_the_config(
    tmp_path: Path, sandbox: dict[str, str]
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def fake_run(cmd: list[str], *, env: Mapping[str, str], cwd: Path) -> None:
        calls.append((cmd, dict(env)))

    root = tmp_path / "pw"
    with patch.object(browser_mcp, "_run", fake_run):
        result = await install_browser_mcp(sandbox, root)

    node_bin = root / "mise" / "installs" / "node" / NODE_VERSION / "bin"
    assert result == BrowserMcp(
        node=node_bin / "node",
        cli=root / "node_modules" / "@playwright" / "mcp" / "cli.js",
        config=root / "config.json",
    )
    (mise_cmd, mise_env), (npm_cmd, npm_env) = calls
    assert mise_cmd == ["mise", "install", f"node@{NODE_VERSION}"]
    assert mise_env["MISE_DATA_DIR"] == str(root / "mise")
    assert npm_cmd[0] == str(node_bin / "npm")
    assert npm_cmd[-1] == f"@playwright/mcp@{browser_mcp.PLAYWRIGHT_MCP_VERSION}"
    assert npm_env["PATH"].startswith(f"{node_bin}:")

    launch = json.loads((root / "config.json").read_text())["browser"]["launchOptions"]
    assert launch["executablePath"].endswith("chrome-linux/chrome-headless-shell")
    assert launch["proxy"] == {"server": "http://127.0.0.1:8080"}
    pin = browser_mcp.spki_pin((tmp_path / "ca.crt").read_bytes())
    assert launch["args"] == [f"--ignore-certificate-errors-spki-list={pin}"]


@pytest.mark.parametrize("missing", ["HTTPS_PROXY", "NODE_EXTRA_CA_CERTS"])
async def test_no_proxy_means_no_browser(
    tmp_path: Path, sandbox: dict[str, str], missing: str
) -> None:
    del sandbox[missing]
    assert await install_browser_mcp(sandbox, tmp_path / "pw") is None


async def test_no_chromium_means_no_browser(tmp_path: Path, sandbox: dict[str, str]) -> None:
    sandbox["PLAYWRIGHT_BROWSERS_PATH"] = str(tmp_path / "empty")
    assert await install_browser_mcp(sandbox, tmp_path / "pw") is None


async def test_a_failed_install_step_means_no_browser(
    tmp_path: Path, sandbox: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    async def failing(cmd: list[str], *, env: Mapping[str, str], cwd: Path) -> None:
        raise BrowserUnavailable("`mise install` failed: 403 denied: host not in allowlist")

    with patch.object(browser_mcp, "_run", failing):
        assert await install_browser_mcp(sandbox, tmp_path / "pw") is None
    assert "host not in allowlist" in caplog.text


async def test_install_steps_surface_the_command_output(tmp_path: Path) -> None:
    with pytest.raises(BrowserUnavailable, match="boom"):
        await browser_mcp._run(["sh", "-c", "echo boom; exit 3"], env={}, cwd=tmp_path)
