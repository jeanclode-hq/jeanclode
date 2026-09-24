import json
import os
import re
from pathlib import Path

from claude_agent_sdk import ToolResultBlock, ToolUseBlock

from src.agents.schemas import AgentUsage

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_FENCED_OPEN_RE = re.compile(r"^```[a-zA-Z]*\s*\n?")
_FENCED_CLOSE_RE = re.compile(r"\n?```\s*$")


def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def block_text(block: ToolResultBlock) -> str:
    content = block.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    return ""


def _strip_workspace(path: str, cwd: Path | None) -> str:
    if not cwd:
        return path
    cwd_str = str(cwd)
    for prefix in (cwd_str, os.path.realpath(cwd_str)):
        if path.startswith(prefix):
            return path[len(prefix) :].lstrip("/")
    return path


def tool_summary(block: ToolUseBlock, cwd: Path | None = None) -> str:
    inp = block.input
    match block.name:
        case "Read" | "Write" | "Edit":
            return _strip_workspace(str(inp.get("file_path", "")), cwd)
        case "Glob":
            return str(inp.get("pattern", ""))
        case "Grep":
            path = _strip_workspace(str(inp.get("path", ".")), cwd)
            return f"{inp.get('pattern', '')} in {path}"
        case "Bash":
            cmd = str(inp.get("command", ""))
            return cmd if len(cmd) <= 120 else cmd[:120] + "..."
        case "WebFetch":
            return str(inp.get("url", ""))
        case "WebSearch":
            return str(inp.get("query", ""))
        case "Agent":
            return str(inp.get("description", ""))
        case "ToolSearch":
            return str(inp.get("query", ""))
        case "Skill":
            return str(inp.get("skill", ""))
        case "StructuredOutput":
            # Keys only — the payload carries whole fix plans. Enough to
            # tell "the agent handed over nothing" from "it handed over
            # something the schema didn't recognise", which is otherwise
            # invisible: an unmatched payload validates to all-defaults
            # without raising.
            keys = ", ".join(sorted(str(k) for k in inp))
            kind = inp.get("kind")
            return f"kind={kind} fields: {keys}" if kind else f"fields: {keys}"
        case _:
            return ""


def try_parse_json_object(text: str) -> dict[str, object] | None:
    text = text.strip()
    if text.startswith("```"):
        text = _FENCED_OPEN_RE.sub("", text)
        text = _FENCED_CLOSE_RE.sub("", text)
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def usage_dict(usage: AgentUsage) -> dict[str, int]:
    dump = usage.model_dump()
    return {k: int(v) for k, v in dump.items() if isinstance(v, int)}
