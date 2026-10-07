from __future__ import annotations

from src.runtime.review_strategy import (
    REVIEW_PRIMARY_SKILL_ENV_VAR,
    REVIEW_STRATEGY_ENV_VAR,
    resolve_review_strategy,
)


def test_defaults_to_builtin() -> None:
    resolved = resolve_review_strategy({})
    assert resolved.strategy == "builtin"
    assert not resolved.uses_skill_primary


def test_skill_requires_primary_name() -> None:
    resolved = resolve_review_strategy({REVIEW_STRATEGY_ENV_VAR: "skill"})
    assert resolved.strategy == "builtin"
    assert not resolved.uses_skill_primary


def test_skill_mode_when_configured() -> None:
    resolved = resolve_review_strategy(
        {
            REVIEW_STRATEGY_ENV_VAR: "skill",
            REVIEW_PRIMARY_SKILL_ENV_VAR: "vue3-review",
        }
    )
    assert resolved.uses_skill_primary
    assert resolved.primary_skill == "vue3-review"
