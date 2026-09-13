"""GitLab webhook → workflow dispatch evaluator.

Mirrors :mod:`api.plugins.github.dispatch` — same trigger semantics, same
hardcoded label convention. The webhook handler normalizes GitLab's
action vocabulary (``open`` / ``update``) to GitHub's
(``opened`` / ``synchronize`` / ``labeled``) before calling
:func:`evaluate_trigger`, so the underlying rule lives in one place.
"""

from __future__ import annotations

# Re-export the evaluator + label constants so callers can import either
# `api.plugins.github.dispatch` or `api.plugins.gitlab.dispatch` and not
# care which provider they're on.
from api.plugins.github.dispatch import (
    LABEL_RESOLVE,
    LABEL_REVIEW,
    LABEL_SUMMARY,
    evaluate_trigger,
)

__all__ = ["LABEL_RESOLVE", "LABEL_REVIEW", "LABEL_SUMMARY", "evaluate_trigger"]
