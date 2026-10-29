"""No-op means no intervention, not zero inventory or disabled commerce."""

import pytest

from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.harness import NoopCeoHarness
from ceo_arena.models import AgentUsage, CompanyDecision, DecisionWindowRequest
from ceo_arena.runtime.context import _issue_company_context


@pytest.mark.asyncio
async def test_noop_preserves_initial_policies_and_matches_empty_decision_economy() -> None:
    world = MarketWorld(default_scenario(seed=11, total_days=7))
    state = world.create_initial_state(("company-1", "company-2"))
    observations = world.project_observations(state)
    decisions = {}
    harness = NoopCeoHarness()
    for company, observation in observations.items():
        original = observation.detached_copy()
        request = DecisionWindowRequest(
            run_id="noop-contract",
            company_id=company,
            world_version=observation.world_version,
            observation=observation,
        )
        with _issue_company_context(observation) as context:
            result = await harness.decide(request, context)
        assert result.status == "sealed"
        assert result.usage == AgentUsage()
        assert result.decision == CompanyDecision(company_id=company, world_version=state.version)
        assert observation == original
        decisions[company] = result.decision
    actual = world.advance_window(state, decisions)
    expected = world.advance_window(
        state,
        {
            company: CompanyDecision(company_id=company, world_version=state.version)
            for company in observations
        },
    )
    assert actual == expected
    for company in actual.state.companies.values():
        assert company.reorder_targets == state.companies[company.company_id].reorder_targets
        assert company.prices_cents == state.companies[company.company_id].prices_cents
        assert company.cumulative_revenue_cents > 0


@pytest.mark.asyncio
async def test_noop_rejects_wrong_company_scope() -> None:
    world = MarketWorld(default_scenario())
    observations = world.project_observations(world.create_initial_state(("a", "b")))
    request = DecisionWindowRequest(
        run_id="scope", company_id="a", world_version=0, observation=observations["a"]
    )
    with _issue_company_context(observations["b"]) as context:
        result = await NoopCeoHarness().decide(request, context)
    assert result.status == "protocol_error"
    assert result.decision is None
