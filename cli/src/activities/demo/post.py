"""Write the demo gate's outcome into the PR/MR(s) it ran for."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from src.activities.demo.publish import demo_media, link_demo, post_demo_note, publish_demo
from src.activities.demo.schemas import DemoGateState, DemoRound
from src.activities.git.schemas import PRRef
from src.runtime.context import RunContext

logger = logging.getLogger(__name__)


def demo_note(state: DemoGateState) -> str:
    """Why a PR/MR carries no demo, for its comment thread."""
    if state.bypass_reason:
        return f"No demo for this change: the fixer stood the demo gate down. {state.bypass_reason}"
    if state.dirty:
        return (
            f"No demo for this change: after {state.rounds} round(s) the fixer's checkout "
            f"still had uncommitted changes, so the demo couldn't run: {', '.join(state.dirty)}."
        )
    if state.error:
        return (
            f"No demo for this change: after {state.rounds} round(s) the demo agent "
            f"failed.\n\n{state.error}"
        )
    if state.last is not None:
        return (
            f"No demo for this change: after {state.rounds} round(s) the demo agent's last "
            f"verdict was `{state.last.verdict}`.\n\n{state.last.evidence.strip()}"
        )
    return ""


def _app_prs(prs: dict[str, PRRef], app_repo: str) -> list[str]:
    """The PRs that get the demo: the app's own, or all of them when it has none."""
    return [app_repo] if app_repo in prs else list(prs)


def post_demos(
    prs: dict[str, PRRef], state: DemoGateState, *, ctx: RunContext, cwds: dict[str, Path]
) -> dict[str, str]:
    """Post the last ``ok`` round on the app's PR, a link to it on the others, or
    a note saying why there's no demo.

    Returns each PR's outcome for the workflow result. Never raises: a demo that
    fails to post must not hold back the PRs' labels.
    """
    demo = state.ok
    outcomes: dict[str, str] = {}
    if demo is not None and demo_media(demo):
        demo_on = _app_prs(prs, demo.app_repo)
        for name in demo_on:
            outcomes[name] = _attempt(
                prs[name], lambda pr, c: publish_demo(pr, demo, ctx=c), "posted", ctx, cwds[name]
            )
        posted = [n for n in demo_on if outcomes[n] == "posted"]
        app_pr = prs[posted[0]] if posted else None
        for name in (n for n in prs if n not in demo_on):
            if app_pr is None:
                outcomes[name] = "none"
                continue
            outcomes[name] = _attempt(
                prs[name],
                _linker(app_pr),
                "linked",
                ctx,
                cwds[name],
            )
        return outcomes

    note = demo_note(state)
    if demo is not None:
        note = "The demo agent saw the change working but saved no screenshot or video."
    last_app = (demo or state.last or DemoRound(verdict="ok")).app_repo
    note_on = _app_prs(prs, last_app)
    for name in prs:
        if note and name in note_on:
            failed = _attempt(
                prs[name], lambda pr, c: post_demo_note(pr, note, ctx=c), "", ctx, cwds[name]
            )
            if failed:
                outcomes[name] = failed
                continue
        outcomes[name] = "bypassed" if state.bypass_reason else "none"
    return outcomes


def _linker(app_pr: PRRef) -> Callable[[PRRef, RunContext], None]:
    def _link(pr: PRRef, ctx: RunContext) -> None:
        link_demo(pr, app_pr, ctx=ctx)

    return _link


def _attempt(
    pr: PRRef,
    action: Callable[[PRRef, RunContext], object],
    done: str,
    ctx: RunContext,
    cwd: Path,
) -> str:
    try:
        action(pr, ctx.with_cwd(cwd))
    except Exception:
        logger.warning("posting the demo on %s failed", pr.url, exc_info=True)
        return "post_failed"
    return done
