"""BaseAgent — LLM-backed activity wrapping the Claude Agent SDK.

Subclasses set ClassVars (`name`, `prompt_file`, `allowed_tools`,
`output_schema`) and call `await self.invoke(input, ctx)`. The base class
handles prompt rendering, SDK option building, message streaming, and
emission of typed events on the context's bus.
"""

import logging
import sys
import time
from pathlib import Path
from typing import Any, ClassVar

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from claude_agent_sdk.types import HookEvent
from jinja2 import Template
from pydantic import BaseModel

from src.agents.memory_tool import MEMORY_SERVER_NAME, MEMORY_TOOL_NAMES, memory_mcp_server
from src.agents.schemas import AgentResult, AgentUsage
from src.agents.utils import (
    block_text,
    strip_frontmatter,
    tool_summary,
    try_parse_json_object,
    usage_dict,
)
from src.runtime.context import RunContext
from src.runtime.events import AgentEnd, AgentStart, ToolCall, ToolResult
from src.runtime.llm_options import fixer_llm_block
from src.skills.prompt import (
    continuity_protocol_block,
    discovery_block,
    memory_protocol_block,
    related_repos_block,
)

logger = logging.getLogger(__name__)


class BaseAgent:
    """Base class for LLM-backed activities."""

    name: ClassVar[str] = ""
    prompt_file: ClassVar[str] = ""
    # Rendered with the same input and sent as the system prompt. Agents sharing one, with the
    # same tools and output_schema, reuse each other's prompt cache once the first one's
    # response has begun.
    system_prompt_file: ClassVar[str] = ""
    allowed_tools: ClassVar[list[str]] = []
    # Built-in Claude Code tools the session loads at all; None keeps the default set.
    # bypassPermissions makes allowed_tools no restriction, so this is the real limit.
    builtin_tools: ClassVar[list[str] | None] = None
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = None
    # Per-agent opt-in. When True the agent gets:
    #   - the Skill tool authorized
    #   - user plugin folders loaded via ClaudeAgentOptions.plugins
    #   - a discovery block (name + description) injected into the prompt
    #   - the attribution + guardrail reminder in that block
    # The agent's prompt_file still owns the guardrail declaring which
    # fields skills may influence.
    use_third_party_skills: ClassVar[bool] = False
    # Per-agent opt-in for org-registered MCP servers (see issue #191).
    # Jeanclode is a pure MCP client — each entry becomes an
    # McpHttpServerConfig the SDK connects out to, never a spawned process.
    use_mcp_connectors: ClassVar[bool] = False
    # Per-agent opt-in for the memory tool (requires ctx.memory_enabled too).
    use_memory: ClassVar[bool] = False
    # Per-agent opt-in for agents whose prompt includes issue/PR comments,
    # notes, or discussion history — warns that some of that history may
    # be prior turns from other jeanclode agents/workflows on the same
    # thread, so settled points there shouldn't be re-derived from scratch.
    use_continuity: ClassVar[bool] = False
    prefer_small_model: ClassVar[bool] = False
    # Triage that picks the fixer's LLM when the run offers a choice (#43).
    choose_fixer_llm: ClassVar[bool] = False

    def _prompts_dir(self) -> Path:
        """Resolve the directory where `prompt_file` lives.

        Defaults to `<package-of-this-subclass>/prompts/`. Subclasses can
        override by setting an absolute `prompt_file` instead.
        """
        module = sys.modules.get(self.__class__.__module__)
        module_file = getattr(module, "__file__", None) if module else None
        if module_file is None:
            return Path.cwd() / "prompts"
        return Path(module_file).resolve().parent / "prompts"

    def _render_file(self, prompt_file: str, agent_input: BaseModel) -> str:
        path = Path(prompt_file)
        if not path.is_absolute():
            path = self._prompts_dir() / prompt_file
        text = strip_frontmatter(path.read_text())
        return Template(text).render(**agent_input.model_dump())

    def _render(self, agent_input: BaseModel) -> str:
        return self._render_file(self.prompt_file, agent_input) if self.prompt_file else ""

    async def invoke(
        self,
        agent_input: BaseModel,
        ctx: RunContext,
        *,
        extra_hooks: dict[HookEvent, list[HookMatcher]] | None = None,
    ) -> AgentResult:
        prompt = self._render(agent_input)
        if self.use_third_party_skills and ctx.skills:
            prompt = f"{prompt}\n\n{discovery_block(ctx.skills)}"
        if self.choose_fixer_llm and ctx.llm_options:
            prompt = f"{prompt}\n\n{fixer_llm_block(ctx.llm_options)}"
        if ctx.related_repos:
            prompt = f"{prompt}\n\n{related_repos_block(ctx.related_repos)}"
        if self.use_memory and ctx.memory_enabled:
            prompt = f"{prompt}\n\n{memory_protocol_block()}"
        if self.use_continuity:
            prompt = f"{prompt}\n\n{continuity_protocol_block()}"
        options = self._build_options(ctx, extra_hooks=extra_hooks)
        if self.system_prompt_file:
            options.system_prompt = self._render_file(self.system_prompt_file, agent_input)

        agent_name = self.name or self.__class__.__name__
        ctx.emit(AgentStart(name=agent_name))

        text = ""
        structured: dict[str, object] | None = None
        result_message: ResultMessage | None = None
        usage = AgentUsage()
        ok = True
        started = time.monotonic()
        # tool_use_id -> (tool_name, input, parent_tool_use_id)
        pending: dict[str, tuple[str, dict[str, object], str | None]] = {}

        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    parent_id = message.parent_tool_use_id
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            tool_input: dict[str, object] = dict(block.input)
                            pending[block.id] = (block.name, tool_input, parent_id)
                            ctx.emit(
                                ToolCall(
                                    agent=agent_name,
                                    name=block.name,
                                    summary=tool_summary(block, cwd=ctx.workspace),
                                    input=tool_input,
                                    parent_tool_use_id=parent_id,
                                    tool_use_id=block.id,
                                )
                            )

                elif isinstance(message, UserMessage):
                    content = message.content if isinstance(message.content, list) else []
                    for block in content:
                        if isinstance(block, ToolResultBlock) and block.tool_use_id in pending:
                            tool_name, _input, parent_id = pending.pop(block.tool_use_id)
                            ctx.emit(
                                ToolResult(
                                    agent=agent_name,
                                    name=tool_name,
                                    output=block_text(block),
                                    parent_tool_use_id=parent_id,
                                )
                            )

                elif isinstance(message, SystemMessage) and message.subtype == "init":
                    _log_session_init(agent_name, message.data)

                elif isinstance(message, ResultMessage):
                    text = message.result or ""
                    usage = AgentUsage.from_result(
                        message.usage,
                        num_turns=message.num_turns,
                        duration_ms=message.duration_ms,
                    )
                    result_message = message
                    raw_structured = getattr(message, "structured_output", None)
                    if isinstance(raw_structured, dict):
                        structured = dict(raw_structured)
                    elif isinstance(raw_structured, str):
                        structured = try_parse_json_object(raw_structured)

            structured = self._reconcile_structured(structured, text)
            if self.output_schema is not None and structured is None:
                self._log_missing_structured(result_message, text)
        except BaseException as exc:
            ok = False
            # A 429 must stale the credential this session ran on, which
            # for a retargeted fixer isn't the run's default (#43).
            credential_id = ctx.env.get("JEANCLODE_LLM_CREDENTIAL_ID")
            if credential_id and isinstance(exc, Exception):
                exc.llm_credential_id = credential_id  # type: ignore[attr-defined]
            raise
        finally:
            ctx.emit(
                AgentEnd(
                    name=agent_name,
                    ok=ok,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    usage=usage_dict(usage),
                )
            )

        return AgentResult(text=text, structured=structured, usage=usage)

    # Keywords the bundled CLI's strict-schema derivation accepts. Anything
    # else (notably pydantic's `default`) makes it bail to a permissive Ajv
    # check where *any* JSON object validates — see _strict_schema.
    _STRICT_KEYWORDS = frozenset(
        {
            "$schema",
            "type",
            "description",
            "title",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "enum",
            "const",
            "anyOf",
        }
    )

    # Keywords whose removal would change what a schema *means*, rather
    # than just dropping an annotation the CLI ignores. A schema using any
    # of them (pydantic emits $ref/$defs for nested models) is handed over
    # untouched — better a permissive check than a rewritten contract.
    _UNSTRICTIFIABLE = frozenset({"$ref", "$defs", "allOf", "oneOf", "not", "patternProperties"})

    @classmethod
    def _can_strictify(cls, node: Any) -> bool:
        if isinstance(node, list):
            return all(cls._can_strictify(n) for n in node)
        if not isinstance(node, dict):
            return True
        if cls._UNSTRICTIFIABLE & set(node):
            return False
        return all(cls._can_strictify(v) for v in node.values())

    @classmethod
    def _schema_for_cli(cls, model: type[BaseModel]) -> dict[str, Any]:
        schema = model.model_json_schema()
        if not cls._can_strictify(schema):
            logger.debug("%s: schema uses $ref/$defs; sending it unmodified", model.__name__)
            return schema
        return cls._strict_schema(schema)  # type: ignore[no-any-return]

    @classmethod
    def _strict_schema(cls, node: Any) -> Any:
        """Rewrite a pydantic JSON schema into the CLI's strict dialect.

        The CLI compiles our schema with Ajv and only derives a *strict*
        schema (the one that actually constrains generation) when every
        keyword is in its allowed set and each object declares
        `required` plus `additionalProperties: false`. Pydantic emits
        `default` for defaulted fields, which fails that check — so the
        CLI fell back to plain Ajv, where an object with none of our
        fields still validates. That is how a triage verdict arrived
        wrapped as `{"input": {...}}`, passed validation, and was
        reported as a success while carrying nothing we could read
        (jc-sentry-1887793).

        Every property becomes required, which is what strict structured
        output means: the model fills each field, using empty
        string/list/null rather than omitting it. Schemas we can't
        express this way (e.g. `$ref`/`$defs` from nested models) are
        left for the CLI to reject on its own and handle as before.
        """
        if isinstance(node, list):
            return [cls._strict_schema(n) for n in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key not in cls._STRICT_KEYWORDS:
                continue
            if key == "properties" and isinstance(value, dict):
                # Keys here are field names, not schema keywords — only
                # their values are schemas to rewrite.
                out[key] = {name: cls._strict_schema(sub) for name, sub in value.items()}
            elif key in {"enum", "const", "required"}:
                out[key] = value
            else:
                out[key] = cls._strict_schema(value)
        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    @staticmethod
    def _unwrap_envelope(
        structured: dict[str, object] | None, fields: set[str]
    ) -> dict[str, object] | None:
        """Return the real payload when it arrived inside a single-key
        wrapper, e.g. ``{"input": {...the schema's fields...}}``.

        Seen in prod from the deferred-tool call path: the whole verdict
        nested under ``input``, which a permissive schema happily
        validates (jc-sentry-1887793). Only unwraps when the inner object
        actually carries the schema's fields, so a legitimate one-field
        payload is never mistaken for an envelope.
        """
        if not structured or len(structured) != 1:
            return None
        inner = next(iter(structured.values()))
        if isinstance(inner, dict) and fields & {str(k) for k in inner}:
            return dict(inner)
        return None

    def _log_missing_structured(self, message: ResultMessage | None, text: str) -> None:
        """Record the CLI's own account of why a schema-bound turn produced
        no structured output.

        Without this the failure is mute and every cause looks identical
        (jc-sentry-1887773): four prod runs reported an all-defaults verdict
        with nothing in the logs to separate them. ``subtype`` is the field
        that names it — ``error_max_structured_output_retries`` means the
        agent did call StructuredOutput but no attempt survived (schema
        validation, or retraction by a model fallback), while ``success``
        with no payload means the tool result never reached the turn stream.
        """
        if message is None:
            logger.warning(
                "%s: schema-bound turn ended without a ResultMessage",
                self.name or type(self).__name__,
            )
            return
        logger.warning(
            "%s: no structured output — subtype=%s stop_reason=%s terminal_reason=%s "
            "is_error=%s deferred_tool_use=%s text_len=%d text_head=%r",
            self.name or type(self).__name__,
            message.subtype,
            getattr(message, "stop_reason", None),
            getattr(message, "terminal_reason", None),
            message.is_error,
            getattr(getattr(message, "deferred_tool_use", None), "name", None),
            len(text),
            text[:200],
        )

    def _reconcile_structured(
        self, structured: dict[str, object] | None, text: str
    ) -> dict[str, object] | None:
        """Fall back to the agent's own text when the structured payload is
        missing or unrecognisable.

        A schema-bound agent ends its turn by calling ``StructuredOutput``
        and *also* prints that same JSON as its result text. The schema we
        hand the CLI (``--json-schema``) declares no required properties
        and doesn't forbid extras, so any object passes validation there —
        which means a payload whose keys don't match the model reaches us
        looking successful and then validates to an all-defaults instance,
        losing an entire investigation without an error anywhere
        (jc-sentry-1887773). Prefer whichever of the two actually carries
        the schema's fields.
        """
        if self.output_schema is None:
            return structured
        fields = set(self.output_schema.model_fields)
        if structured is not None and fields & {str(k) for k in structured}:
            return structured
        unwrapped = self._unwrap_envelope(structured, fields)
        if unwrapped is not None:
            logger.warning(
                "%s: structured output arrived wrapped in a single %r key; unwrapped it",
                self.name or type(self).__name__,
                next(iter(structured or {}), ""),
            )
            return unwrapped
        if not text:
            return structured
        parsed = try_parse_json_object(text)
        if parsed is None or not fields & {str(k) for k in parsed}:
            return structured
        if structured is not None:
            logger.warning(
                "%s: structured output carried none of the schema's fields (keys: %s); "
                "recovered the verdict from the result text instead",
                self.name or type(self).__name__,
                ", ".join(sorted(str(k) for k in structured)) or "none",
            )
        return parsed

    def _build_options(
        self,
        ctx: RunContext,
        *,
        extra_hooks: dict[HookEvent, list[HookMatcher]] | None = None,
    ) -> ClaudeAgentOptions:
        kwargs: dict[str, Any] = {
            "permission_mode": "bypassPermissions",
            "max_turns": self.max_turns,
            "cwd": str(ctx.cwd),
        }
        if extra_hooks:
            kwargs["hooks"] = extra_hooks
        if self.builtin_tools is not None:
            kwargs["tools"] = list(self.builtin_tools)

        tools = list(self.allowed_tools)
        mcp_servers: dict[str, Any] = {}
        if self.use_third_party_skills and ctx.skills:
            if "Skill" not in tools:
                tools.append("Skill")
            plugin_paths = {s.plugin_path for s in ctx.skills}
            kwargs["plugins"] = [{"type": "local", "path": str(p)} for p in sorted(plugin_paths)]
        if self.use_memory and ctx.memory_enabled:
            tools.extend(t for t in MEMORY_TOOL_NAMES if t not in tools)
            mcp_servers[MEMORY_SERVER_NAME] = memory_mcp_server()
        if self.use_mcp_connectors and ctx.mcp_servers:
            for server in ctx.mcp_servers:
                if server.name in mcp_servers:
                    # Backend rejects "memory" on create/update, but a row
                    # from before that guard existed could still collide
                    # with the in-process memory server registered above.
                    logger.warning(
                        "skipping org MCP server %r — name collides with an "
                        "already-registered server",
                        server.name,
                    )
                    continue
                mcp_servers[server.name] = server.to_sdk_config()
                wildcard = f"mcp__{server.name}__*"
                if wildcard not in tools:
                    tools.append(wildcard)
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers
        if tools:
            kwargs["allowed_tools"] = tools

        if ctx.env:
            kwargs["env"] = dict(ctx.env)
        if self.prefer_small_model:
            kwargs["model"] = ctx.small_model or "haiku"
        elif ctx.model:
            kwargs["model"] = ctx.model
        if self.output_schema is not None:
            kwargs["output_format"] = {
                "type": "json_schema",
                "schema": self._schema_for_cli(self.output_schema),
            }
        return ClaudeAgentOptions(**kwargs)


def _log_session_init(agent_name: str, data: dict[str, Any]) -> None:
    """Log what the CLI actually loaded — the only proof a plugin skill or MCP
    server made it into the session, as opposed to merely being configured.
    """

    def names(key: str) -> str:
        items = data.get(key) or []
        return (
            ", ".join(str(i.get("name", i)) if isinstance(i, dict) else str(i) for i in items)
            or "none"
        )

    servers = (
        ", ".join(f"{s.get('name')}={s.get('status')}" for s in data.get("mcp_servers") or [])
        or "none"
    )
    logger.info(
        "%s: session init — model=%s plugins=[%s] skills=[%s] mcp=[%s] tools=%d",
        agent_name,
        data.get("model"),
        names("plugins"),
        names("skills"),
        servers,
        len(data.get("tools") or []),
    )
