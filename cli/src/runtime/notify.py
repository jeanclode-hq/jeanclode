"""The notify list — who to @-mention once a bot-opened PR/MR is ready.

The backend resolves the tenant's configured provider identities to
handles and hands them over as the ``JEANCLODE_NOTIFY_USERS`` JSON
payload (see ``add_notify_to_inputs``). Its presence is the on/off
signal: no var, no ping.

The marker lives here rather than with the posting activity because both
ends of the review loop can emit the notice (code review on LGTM, respond
on a resolve-only turn), so both have to recognise the other's comment
and stand down.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

NOTIFY_USERS_ENV_VAR = "JEANCLODE_NOTIFY_USERS"

# Invisible in rendered markdown on both providers, and greppable in the
# raw comment body — this is what makes the notice post-once.
READY_MARKER = "<!-- jeanclode:ready -->"

_HANDLE_MAX_LEN = 64


def _clean(handle: object) -> str | None:
    """A usable bare handle, or None.

    Handles are @-mentioned into a comment body, so anything carrying
    whitespace or markdown is dropped rather than rendered — the backend
    resolves these from the org's own members, but this is the last point
    before they become live text in someone's repo.
    """
    if not isinstance(handle, str):
        return None
    text = handle.strip().lstrip("@").strip()
    if not text or len(text) > _HANDLE_MAX_LEN:
        return None
    if any(c.isspace() for c in text):
        return None
    if not all(c.isalnum() or c in "-_." for c in text):
        return None
    return text


def load_notify_users_from_env(env: dict[str, str] | None = None) -> list[str]:
    """Read ``JEANCLODE_NOTIFY_USERS`` (a JSON array of handles).

    Order is preserved and duplicates collapse, so the rendered mention
    line matches what the tenant picked in the dashboard.
    """
    raw = (env or os.environ).get(NOTIFY_USERS_ENV_VAR, "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON, ignoring", NOTIFY_USERS_ENV_VAR)
        return []
    if not isinstance(entries, list):
        logger.warning("%s is not a JSON array, ignoring", NOTIFY_USERS_ENV_VAR)
        return []

    handles: list[str] = []
    for entry in entries:
        handle = _clean(entry)
        if handle is None:
            logger.warning("skipping malformed notify handle: %r", entry)
        elif handle not in handles:
            handles.append(handle)
    return handles


def ready_notice_body(handles: list[str], *, findings: int = 0) -> str:
    """The comment body for a ready-for-review ping.

    ``findings`` is the count of review comments left on the MR. Zero
    reads as converged; anything else says so plainly rather than
    implying the bot signed off on work a human still has to judge.
    """
    mentions = " ".join(f"@{h}" for h in handles)
    if findings > 0:
        state = (
            f"Jeanclode is done here — {findings} review "
            f"{'comment' if findings == 1 else 'comments'} left for you to weigh in on."
        )
    else:
        state = "Jeanclode is done here and the review came back clean — ready for you."
    return f"{READY_MARKER}\n{mentions} {state}"
