"""Pydantic schemas exposed by the agents layer."""

from typing import Any, Self

from pydantic import BaseModel, Field


class AgentUsage(BaseModel):
    """Token usage from a Claude Agent SDK invocation."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0

    @classmethod
    def from_result(cls, usage: dict[str, Any] | None, **kwargs: Any) -> Self:
        """Build from ResultMessage fields."""
        u = usage or {}
        return cls(
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            cache_read_tokens=u.get("cache_read_input_tokens", 0),
            cache_write_tokens=u.get("cache_creation_input_tokens", 0),
            **kwargs,
        )

    def __add__(self, other: AgentUsage) -> AgentUsage:
        return AgentUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            num_turns=self.num_turns + other.num_turns,
            duration_ms=self.duration_ms + other.duration_ms,
        )


class AgentResult(BaseModel):
    """Result of one BaseAgent.invoke call."""

    text: str = ""
    structured: dict[str, object] | None = None
    usage: AgentUsage = Field(default_factory=AgentUsage)
