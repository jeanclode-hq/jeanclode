"""Built-in Playwright MCP, so agents can drive a browser through the security proxy.

Installed at container start, every run, under its own mise dirs and off ``PATH``
so a repo's pinned Node is unaffected. It drives the Chromium headless shell the
image already ships, through the proxy, trusting only the proxy's CA.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from claude_agent_sdk.types import McpStdioServerConfig
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel

logger = logging.getLogger(__name__)

BROWSER_SERVER_NAME = "playwright"
# Its own Playwright is an alpha whose browser revision never matches the image's;
# `executablePath` below is what keeps it from downloading one.
PLAYWRIGHT_MCP_VERSION = "0.0.83"
NODE_VERSION = "24.21.0"
BROWSER_MCP_DIR = Path("/tmp/playwright-mcp")
INSTALL_TIMEOUT_S = 180
_CHROMIUM_GLOB = "chromium_headless_shell-*/chrome-*/chrome-headless-shell"


class BrowserMcp(BaseModel):
    node: Path
    cli: Path
    config: Path

    def to_sdk_config(self) -> McpStdioServerConfig:
        return {
            "type": "stdio",
            "command": str(self.node),
            "args": [str(self.cli), "--config", str(self.config)],
        }


class BrowserUnavailable(Exception):
    pass


def spki_pin(ca_pem: bytes) -> str:
    """Chromium's ``--ignore-certificate-errors-spki-list`` form of a certificate's key."""
    key = x509.load_pem_x509_certificate(ca_pem).public_key()
    der = key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


def find_chromium(browsers_path: Path) -> Path:
    found = sorted(browsers_path.glob(_CHROMIUM_GLOB))
    if not found:
        raise BrowserUnavailable(f"no Chromium headless shell under {browsers_path}")
    return found[-1]


def mcp_config(*, chromium: Path, proxy: str, ca_pem: bytes, output_dir: Path) -> dict[str, object]:
    return {
        "browser": {
            "browserName": "chromium",
            "isolated": True,
            "launchOptions": {
                "executablePath": str(chromium),
                "headless": True,
                "proxy": {"server": proxy},
                # Chromium ignores SSL_CERT_FILE; this trusts the proxy's CA and nothing else.
                "args": [f"--ignore-certificate-errors-spki-list={spki_pin(ca_pem)}"],
            },
        },
        "outputDir": str(output_dir),
        # Snapshots land in files; an agent may run in another cwd than this server.
        "filePaths": "absolute",
    }


async def _run(cmd: list[str], *, env: Mapping[str, str], cwd: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=dict(env),
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        raise
    if proc.returncode != 0:
        tail = out.decode(errors="replace").strip().splitlines()[-5:]
        raise BrowserUnavailable(f"`{' '.join(cmd[:2])}` failed: {' | '.join(tail)}")


def _write_config(env: Mapping[str, str], root: Path) -> Path:
    proxy = env.get("HTTPS_PROXY")
    ca_path = env.get("NODE_EXTRA_CA_CERTS")
    if not proxy or not ca_path:
        raise BrowserUnavailable("no security proxy (HTTPS_PROXY / NODE_EXTRA_CA_CERTS unset)")
    chromium = find_chromium(Path(env.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright")))
    ca_pem = Path(ca_path).read_bytes()
    root.mkdir(parents=True, exist_ok=True)
    config = root / "config.json"
    config.write_text(
        json.dumps(
            mcp_config(chromium=chromium, proxy=proxy, ca_pem=ca_pem, output_dir=root / "output")
        )
    )
    return config


async def _install(env: Mapping[str, str], root: Path) -> BrowserMcp:
    config = _write_config(env, root)
    mise_dir = root / "mise"
    install_env = {
        **env,
        "MISE_DATA_DIR": str(mise_dir),
        "MISE_CONFIG_DIR": str(mise_dir / "config"),
        "MISE_CACHE_DIR": str(mise_dir / "cache"),
        "MISE_STATE_DIR": str(mise_dir / "state"),
        "npm_config_cache": str(root / "npm-cache"),
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
    }
    await _run(["mise", "install", f"node@{NODE_VERSION}"], env=install_env, cwd=root)
    node_bin = mise_dir / "installs" / "node" / NODE_VERSION / "bin"
    # npm's shebang is `env node`, and this Node is deliberately not on PATH.
    install_env["PATH"] = f"{node_bin}{os.pathsep}{env.get('PATH', '')}"
    await _run(
        [
            str(node_bin / "npm"),
            "install",
            "--prefix",
            str(root),
            "--no-audit",
            "--no-fund",
            f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}",
        ],
        env=install_env,
        cwd=root,
    )
    return BrowserMcp(
        node=node_bin / "node",
        cli=root / "node_modules" / "@playwright" / "mcp" / "cli.js",
        config=config,
    )


async def install_browser_mcp(
    env: Mapping[str, str] | None = None, root: Path = BROWSER_MCP_DIR
) -> BrowserMcp | None:
    """Install the Playwright MCP; ``None`` (with the reason logged) leaves the run without a browser."""
    try:
        async with asyncio.timeout(INSTALL_TIMEOUT_S):
            browser = await _install(os.environ if env is None else env, root)
    except Exception as exc:
        reason = "timed out" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__
        logger.warning("browser MCP unavailable, continuing without it: %s", reason)
        return None
    logger.info(
        "browser MCP ready: @playwright/mcp@%s on Node %s", PLAYWRIGHT_MCP_VERSION, NODE_VERSION
    )
    return browser
