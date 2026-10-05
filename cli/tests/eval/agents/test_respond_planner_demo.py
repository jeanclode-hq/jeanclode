"""Evals for the respond planner's ``demo`` action.

The bar: on a PR/MR, an ask for a demo (alone or with a change) sets
``demo_plan``, and so does a UI push to a PR/MR that already has one. On an
issue the planner never demos: no PR/MR yet → it asks whether to resolve; an
open one → it delegates with a mention there; "do it and add a demo" →
resolve label.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.activities.demo import DEMO_END, DEMO_START
from src.agents.respond.planner import PlannerAgent
from src.agents.respond.schemas import PlannerInput, PlannerOutput
from tests.eval.conftest import assert_cli_called

pytestmark = pytest.mark.eval

REPO = "org/shop"
PLATFORM = "gitlab"

_BADGE_DIFF = """\
diff --git a/src/components/OrderList.vue b/src/components/OrderList.vue
--- a/src/components/OrderList.vue
+++ b/src/components/OrderList.vue
@@ -5,6 +5,7 @@
       <li v-for="order in orders" :key="order.id">
         #{{ order.id }} — {{ order.total }} €
+        <span v-if="order.status === 'shipped'" class="badge badge-green">Shipped</span>
         <button @click="cancel(order.id)">{{ $t("orders.cancel") }}</button>
       </li>
"""

_BACKEND_DIFF = """\
diff --git a/server/totals.py b/server/totals.py
--- a/server/totals.py
+++ b/server/totals.py
@@ -2,4 +2,4 @@ from decimal import Decimal

 def order_total(lines):
-    return sum(Decimal(str(line["price"])) for line in lines)
+    return sum(Decimal(str(line["price"])) * line["quantity"] for line in lines)
"""

_ORDER_LIST = """\
<template>
  <ul>
    <li v-for="order in orders" :key="order.id">
      #{{ order.id }} — {{ order.total }} €
      <span v-if="order.status === 'shipped'" class="badge badge-green">Shipped</span>
      <button @click="cancel(order.id)">{{ $t("orders.cancel") }}</button>
    </li>
  </ul>
</template>
"""

_DEMO_BLOCK = f"{DEMO_START}\n#### Demo\n\n![demo](/uploads/abc/demo.webm)\n{DEMO_END}"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _commit_count(repo: Path) -> int:
    return len(_git(repo, "log", "--oneline").splitlines())


@pytest.fixture
def shop_repo(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    # The demo records the pushed branch, so a push that fails rightly skips it.
    origin = tmp_path_factory.mktemp("origin")
    _git(origin, "init", "-q", "--bare")
    _git(tmp_path, "init", "-q", "-b", "feat/shipped-badge")
    _git(tmp_path, "config", "user.email", "test@test.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "src" / "components").mkdir(parents=True)
    (tmp_path / "src" / "components" / "OrderList.vue").write_text(_ORDER_LIST)
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "totals.py").write_text(
        "from decimal import Decimal\n\n\ndef order_total(lines):\n"
        '    return sum(Decimal(str(line["price"])) * line["quantity"] for line in lines)\n'
    )
    (tmp_path / "server" / "test_totals.py").write_text("def test_total():\n    pass\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "shipped badge")
    _git(tmp_path, "remote", "add", "origin", str(origin))
    _git(tmp_path, "push", "-q", "-u", "origin", "feat/shipped-badge")
    # fake_cli_env's scaffolding lives in tmp_path too.
    (tmp_path / ".git" / "info" / "exclude").write_text("/bin/\n/cli_fixtures/\n/cli_calls.log\n")
    return tmp_path


def _parse(output: dict | None) -> PlannerOutput:
    return PlannerOutput.model_validate(output or {})


def _mr_input(
    mention: str, *, diff: str = _BADGE_DIFF, description: str = "", **kw
) -> PlannerInput:
    return PlannerInput(
        platform=PLATFORM,
        repo=REPO,
        surface="pr_top_level",
        pr="9",
        thread_id="disc-1",
        mention_body=mention,
        mention_author="reviewer",
        pr_description=description or "Show a green Shipped badge on shipped orders.",
        diff=diff,
        **kw,
    )


def _issue_input(mention: str) -> PlannerInput:
    return PlannerInput(
        platform=PLATFORM,
        repo=REPO,
        surface="issue",
        issue="5",
        thread_id="disc-2",
        mention_body=mention,
        mention_author="pm",
    )


def _calls(log: Path) -> str:
    return log.read_text() if log.exists() else ""


# --- PR/MR surface ---


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_demo_request_on_an_mr_plans_a_demo(run_agent, shop_repo, fake_cli_env) -> None:
    before = _commit_count(shop_repo)
    output = _parse(await run_agent(PlannerAgent, _mr_input("can you add a demo of this?")))

    assert "demo" in output.actions_taken
    assert output.demo_plan
    assert "shipped" in output.demo_plan.lower()
    # Only a demo was asked for: nothing to change.
    assert _commit_count(shop_repo) == before


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_change_and_demo_in_one_mention(run_agent, shop_repo, fake_cli_env) -> None:
    before = _commit_count(shop_repo)
    output = _parse(
        await run_agent(
            PlannerAgent,
            _mr_input(
                "make the Shipped badge blue instead of green (class `badge-blue`) and add a demo"
            ),
        )
    )

    assert "handle" in output.actions_taken
    assert "demo" in output.actions_taken
    assert output.demo_plan
    assert _commit_count(shop_repo) > before


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_backend_only_mr_gets_no_demo(run_agent, shop_repo, fake_cli_env) -> None:
    output = _parse(
        await run_agent(
            PlannerAgent,
            _mr_input(
                "can you add a demo of this?",
                diff=_BACKEND_DIFF,
                description="order_total now multiplies by quantity.",
            ),
        )
    )

    assert "demo" not in output.actions_taken
    assert not output.demo_plan
    assert "disc-1" in _calls(fake_cli_env["log"])


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_ui_change_on_an_mr_with_a_demo_re_records_it(
    run_agent, shop_repo, fake_cli_env
) -> None:
    output = _parse(
        await run_agent(
            PlannerAgent,
            _mr_input(
                "the badge should say 'Delivered' instead of 'Shipped', please change it",
                description=f"{_DEMO_BLOCK}\n\nShow a green Shipped badge on shipped orders.",
                has_demo=True,
            ),
        )
    )

    assert "handle" in output.actions_taken
    assert "demo" in output.actions_taken
    assert output.demo_plan


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_test_only_change_on_an_mr_with_a_demo_leaves_it(
    run_agent, shop_repo, fake_cli_env
) -> None:
    output = _parse(
        await run_agent(
            PlannerAgent,
            _mr_input(
                "can you rename `test_total` in server/test_totals.py to `test_order_total`?",
                description=f"{_DEMO_BLOCK}\n\nShow a green Shipped badge on shipped orders.",
                has_demo=True,
            ),
        )
    )

    assert "handle" in output.actions_taken
    assert "demo" not in output.actions_taken
    assert not output.demo_plan


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_no_demo_when_the_org_turned_demos_off(run_agent, shop_repo, fake_cli_env) -> None:
    output = _parse(
        await run_agent(PlannerAgent, _mr_input("can you add a demo of this?", demo_enabled=False))
    )

    assert "demo" not in output.actions_taken
    assert not output.demo_plan


# --- Issue surface ---


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_demo_on_an_issue_without_an_mr_asks_before_resolving(
    run_agent, fake_cli_env
) -> None:
    output = _parse(await run_agent(PlannerAgent, _issue_input("can you add a demo of this?")))

    assert output.actions_taken == ["handle"]
    assert not output.demo_plan
    calls = _calls(fake_cli_env["log"])
    assert "jeanclode:resolve" not in calls
    assert_cli_called(fake_cli_env["log"], "issues/5/discussions/disc-2/notes")


@pytest.mark.parametrize(
    "fake_cli_env",
    [
        {
            "glab": {
                "related_merge_requests.json": [
                    {
                        "iid": 9,
                        "state": "opened",
                        "title": "Shipped badge on orders",
                        "web_url": "https://gitlab.example.com/org/shop/-/merge_requests/9",
                    }
                ],
                "mr_list_org_shop_5.json": [
                    {
                        "iid": 9,
                        "state": "opened",
                        "title": "Shipped badge on orders",
                        "web_url": "https://gitlab.example.com/org/shop/-/merge_requests/9",
                    }
                ],
            }
        }
    ],
    indirect=True,
)
async def test_demo_on_an_issue_with_an_mr_delegates_to_it(run_agent, fake_cli_env) -> None:
    output = _parse(await run_agent(PlannerAgent, _issue_input("can you add a demo of this?")))

    assert "demo" not in output.actions_taken
    assert not output.demo_plan
    calls = _calls(fake_cli_env["log"]).splitlines()
    on_mr = [c for c in calls if ("merge_requests/9" in c or "mr note 9" in c)]
    assert any("@jeanclode-bot" in c for c in on_mr), "\n".join(calls)
    assert not any("jeanclode:resolve" in c for c in calls)


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_do_it_and_add_a_demo_on_an_issue_routes_to_resolve(run_agent, fake_cli_env) -> None:
    output = _parse(
        await run_agent(PlannerAgent, _issue_input("can you do this issue and add a demo?"))
    )

    assert "route" in output.actions_taken
    assert "demo" not in output.actions_taken
    assert_cli_called(fake_cli_env["log"], "issue update", "5", "-R org/shop", "jeanclode:resolve")
