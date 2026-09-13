"""``login_is_bot`` — is this PR/MR author a bot/token account?

Extracted so the two gates that need it agree: code review's post-review
self-mention sweep and respond's ready notice. Patterns mirror the
backend ``_is_bot_login`` helpers (github/mentions.py, gitlab/notes.py).
"""

from __future__ import annotations

from typing import Literal


def login_is_bot(login: str, platform: Literal["github", "gitlab"]) -> bool:
    """True when ``login`` belongs to a bot/token account, not a human.

    Deliberately not an exact identity match: a run holds one token per
    namespace (ADR-003), so a cross-namespace MR is authored by a
    *different* token's bot user than the one this container would
    resolve to.
    """
    login = (login or "").lower()
    if not login:
        return False
    if login.endswith("[bot]"):
        return True
    if platform == "gitlab":
        # group/project access tokens: group_<id>_bot_<hash>, project_<id>_bot_<hash>
        return login == "jeanclode-bot" or login.endswith("_bot") or "_bot_" in login
    return False
