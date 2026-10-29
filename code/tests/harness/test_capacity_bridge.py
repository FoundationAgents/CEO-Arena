"""Exercise both language gateways using genuine environment-generated quotes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from factories import observation

from ceo_arena.config import default_scenario
from ceo_arena.economy.capacity import CapacityManager
from ceo_arena.harness.bun import project_bun_path
from ceo_arena.harness.draft import AgentToolGateway
from ceo_arena.harness.wire import WorkerDecisionRequest
from ceo_arena.models import (
    AgentBudget,
    CompanyDecision,
    DecisionWindowRequest,
)


@pytest.mark.parametrize(
    "action", ["expand_capacity", "downsize_capacity", "cancel_capacity_project"]
)
def test_both_gateways_accept_real_quotes_with_audit_metadata(action: str) -> None:
    root = Path(__file__).resolve().parents[2]
    bun = project_bun_path(root)
    if not bun.exists() or not (root / "OpenCode/packages/llm/node_modules").exists():
        pytest.skip("pinned offline worker dependencies are not installed")
    scenario = default_scenario()
    visible = observation()
    manager = CapacityManager(scenario)
    if action == "downsize_capacity":
        visible.company.capacity_tier = 1
    if action == "cancel_capacity_project":
        manager.apply(
            visible.company,
            CompanyDecision(
                company_id=visible.company.company_id, world_version=7, capacity_target_tier=1
            ),
            visible.day - 1,
        )
    quotes = manager.quotes(visible.company, visible.day)
    quote = next(item for item in quotes if item.action == action and item.project_id is not None)
    assert quote.project_id is not None
    assert quote.target_tier is not None
    visible = visible.model_copy(
        update={
            "capacity_action_quotes": (quote,),
            "capacity_catalog": scenario.capacity_tiers,
        }
    )
    gateway = AgentToolGateway(visible)
    if action == "expand_capacity":
        gateway.expand_capacity(quote.target_tier)
    elif action == "downsize_capacity":
        gateway.downsize_capacity(quote.target_tier)
    else:
        gateway.cancel_capacity_project(quote.project_id)
    expected = gateway.seal()
    message = WorkerDecisionRequest(
        request_id="offline-capacity-test",
        request=DecisionWindowRequest(
            run_id="offline-capacity-test",
            company_id=visible.company.company_id,
            world_version=visible.world_version,
            observation=visible,
            budget=AgentBudget(),
        ),
    )
    script = """
import { AgentToolGateway } from './agent_worker/src/draft';
import { decodeWorkerInput } from './agent_worker/src/protocol';
const message = decodeWorkerInput(await Bun.stdin.text());
if (message.type !== 'decide') throw new Error('wrong message');
const gateway = new AgentToolGateway(message.request);
const quote = message.request.observation.capacity_action_quotes[0];
if (quote.action === 'expand_capacity') gateway.expandCapacity(quote.target_tier);
else if (quote.action === 'downsize_capacity') gateway.downsizeCapacity(quote.target_tier);
else gateway.cancelCapacityProject(quote.project_id);
process.stdout.write(JSON.stringify({ decision: gateway.seal(), preview: gateway.preview() }));
"""
    completed = subprocess.run(
        (str(bun), "-e", script),
        cwd=root,
        input=message.model_dump_json(),
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )
    actual = json.loads(completed.stdout)
    assert CompanyDecision.model_validate(actual["decision"]) == expected
    for field in (
        "upfront_charges_cents",
        "upfront_refunds_cents",
        "cash_after_upfront_cents",
        "daily_policy_cost_cents",
    ):
        assert actual["preview"][field] == getattr(gateway.preview(), field)
