"""Resolve how a code-review run executes (builtin pipeline vs primary skill)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

REVIEW_STRATEGY_ENV_VAR = "JEANCLODE_REVIEW_STRATEGY"
REVIEW_PRIMARY_SKILL_ENV_VAR = "JEANCLODE_REVIEW_PRIMARY_SKILL"


@dataclass(frozen=True)
class ResolvedReviewStrategy:
    strategy: Literal["builtin", "skill"]
    primary_skill: str | None = None

    @property
    def uses_skill_primary(self) -> bool:
        return self.strategy == "skill" and bool(self.primary_skill)


def resolve_review_strategy(env: dict[str, str] | None = None) -> ResolvedReviewStrategy:
    """Read dispatch env. Skill mode requires both strategy=skill and a skill name."""
    raw_env = env if env is not None else os.environ
    raw_strategy = raw_env.get(REVIEW_STRATEGY_ENV_VAR, "builtin").strip().lower()
    primary = raw_env.get(REVIEW_PRIMARY_SKILL_ENV_VAR, "").strip() or None
    if raw_strategy == "skill" and primary:
        return ResolvedReviewStrategy(strategy="skill", primary_skill=primary)
    return ResolvedReviewStrategy(strategy="builtin", primary_skill=None)
