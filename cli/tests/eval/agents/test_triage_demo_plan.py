"""Evals for issue triage's ``demo_plan`` decision.

The bar: a fix that changes what a user sees or clicks in the web UI gets a
plan; copy tweaks, config, refactors and backend-only fixes get ``None``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.agents.issue.schemas import TriageInput, TriageOutput
from src.agents.issue.triage import TriageAgent

pytestmark = pytest.mark.eval

REPO = "org/shop"
PROVIDER = "gitlab"

_PACKAGE_JSON = {
    "name": "shop",
    "private": True,
    "packageManager": "pnpm@9.15.0",
    "scripts": {"dev": "vite", "build": "vite build", "test": "vitest"},
    "dependencies": {"vue": "^3.5.0", "vue-i18n": "^10.0.0"},
    "devDependencies": {"vite": "^6.0.0", "@vitejs/plugin-vue": "^5.0.0", "vitest": "^2.0.0"},
}

_ORDER_LIST = """\
<script setup lang="ts">
import { ref, onMounted } from "vue";
import { fetchOrders, cancelOrder } from "../api/orders";

const orders = ref([]);

onMounted(async () => {
  orders.value = await fetchOrders();
});

async function cancel(id: number) {
  await cancelOrder(id);
  orders.value = await fetchOrders();
}
</script>

<template>
  <section>
    <h1>{{ $t("orders.title") }}</h1>
    <ul>
      <li v-for="order in orders" :key="order.id">
        #{{ order.id }} — {{ order.total }} €
        <button @click="cancel(order.id)">{{ $t("orders.cancel") }}</button>
      </li>
    </ul>
  </section>
</template>
"""

_ORDERS_API = """\
export async function fetchOrders() {
  const res = await fetch("/api/orders");
  return res.json();
}

export async function cancelOrder(id: number) {
  await fetch(`/api/orders/${id}/cancel`, { method: "POST" });
}
"""

_EN = {"orders": {"title": "Your orders", "cancel": "Cancel"}}

_VITE_CONFIG = """\
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: { proxy: { "/api": "http://localhost:8000" } },
});
"""

_BACKEND = """\
from decimal import Decimal


def order_total(lines):
    return sum(Decimal(str(line["price"])) for line in lines)
"""


def _seed_repo(root: Path) -> None:
    (root / "package.json").write_text(json.dumps(_PACKAGE_JSON, indent=2))
    (root / ".nvmrc").write_text("22\n")
    (root / "vite.config.ts").write_text(_VITE_CONFIG)
    src = root / "src"
    (src / "components").mkdir(parents=True)
    (src / "api").mkdir()
    (src / "locales").mkdir()
    (src / "components" / "OrderList.vue").write_text(_ORDER_LIST)
    (src / "api" / "orders.ts").write_text(_ORDERS_API)
    (src / "locales" / "en.json").write_text(json.dumps(_EN, indent=2))
    (root / "server").mkdir()
    (root / "server" / "totals.py").write_text(_BACKEND)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


async def _triage(run_agent, tmp_path: Path, title: str, body: str) -> TriageOutput:
    _seed_repo(tmp_path)
    raw = await run_agent(
        TriageAgent,
        TriageInput(
            repo=REPO,
            provider=PROVIDER,
            repo_name=tmp_path.name,
            issue_number="63",
            issue_title=title,
            issue_body=body,
        ),
    )
    return TriageOutput.model_validate(raw or {})


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_ui_behaviour_change_gets_a_demo_plan(run_agent, tmp_path, fake_cli_env) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        "Ask for confirmation before cancelling an order",
        "Clicking Cancel in the orders list (`src/components/OrderList.vue`) cancels the "
        "order immediately; people cancel by accident. Show a confirmation dialog "
        '("Cancel order #123?") with Confirm / Keep buttons, and only cancel on Confirm.',
    )
    assert output.kind == "proceed"
    assert output.code_change is True
    assert output.demo_plan
    assert "cancel" in output.demo_plan.lower()


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_empty_state_gets_a_demo_plan(run_agent, tmp_path, fake_cli_env) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        "Orders page is blank when there are no orders",
        "A customer with no orders sees just the title and an empty list. Show "
        '"You have no orders yet" with a link to the shop instead.',
    )
    assert output.kind == "proceed"
    assert output.demo_plan


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_translation_tweak_gets_no_demo(run_agent, tmp_path, fake_cli_env) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        "Rename 'Cancel' to 'Cancel order'",
        "In `src/locales/en.json`, `orders.cancel` should read 'Cancel order'.",
    )
    assert output.kind == "proceed"
    assert output.demo_plan is None


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_backend_fix_gets_no_demo(run_agent, tmp_path, fake_cli_env) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        "order_total ignores quantities",
        "`order_total` in `server/totals.py` sums each line's price once, ignoring "
        "`line['quantity']`. It should multiply price by quantity.",
    )
    assert output.kind == "proceed"
    assert output.demo_plan is None


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_config_change_gets_no_demo(run_agent, tmp_path, fake_cli_env) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        "Dev server API proxy should point at port 8080",
        "The backend moved to port 8080 locally. Update the `/api` proxy in "
        "`vite.config.ts` from 8000 to 8080.",
    )
    assert output.kind == "proceed"
    assert output.demo_plan is None
