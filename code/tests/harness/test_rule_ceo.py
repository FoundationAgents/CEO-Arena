"""Behavioral contracts for the fixed, single-market Rule policy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.harness import AgentHarness, RuleCEOConfig, RuleCeoHarness
from ceo_arena.models import AgentUsage, CompanyDecision, CompanyObservation, DecisionWindowRequest
from ceo_arena.runtime.context import _issue_company_context


def _observation() -> CompanyObservation:
    world = MarketWorld(default_scenario(total_days=500))
    return world.project_observations(world.create_initial_state(["own", "rival"]))["own"]


async def _decide(observation: CompanyObservation, config: RuleCEOConfig) -> CompanyDecision:
    request = DecisionWindowRequest(
        run_id="fixed-rule-test",
        company_id=observation.company.company_id,
        world_version=observation.world_version,
        observation=observation,
    )
    with _issue_company_context(observation) as context:
        result = await RuleCeoHarness(config).decide(request, context)
    assert result.status == "sealed", result.error
    assert result.usage == AgentUsage()
    assert result.decision is not None
    return result.decision


@pytest.mark.asyncio
@pytest.mark.parametrize("focus,core", [("value", "A"), ("mainstream", "B"), ("premium", "C")])
async def test_fixed_focus_prices_stock_and_budget(focus: str, core: str) -> None:
    observation = _observation()
    initial = observation.detached_copy()
    config = RuleCEOConfig(focus=focus, price_multiplier_bps=9_000)
    decision = await _decide(observation, config)
    assert isinstance(RuleCeoHarness(), AgentHarness)
    assert observation == initial
    assert tuple(item.price_cents for item in decision.products) == (3_600, 6_750, 12_150)
    assert tuple(item.reorder_target for item in decision.products) == (150, 100, 50)
    assert all(item.promotion_cents == 0 for item in decision.products)
    assert {item.product_id: item.development_budget_cents for item in decision.products} == {
        name: (5_000 if name == core else 0) for name in ("A", "B", "C")
    }
    focused = [item.daily_budget_cents for item in decision.marketing if item.segment_id == focus]
    assert sum(focused) == 5_000 and max(focused) - min(focused) <= 1
    assert all(
        item.daily_budget_cents == 0 for item in decision.marketing if item.segment_id != focus
    )
    assert (decision.reliability_budget_cents, decision.support_budget_cents) == (2_500, 5_000)
    assert not decision.enterprise_bids and not decision.market_research
    assert decision.research_project_tier is None
    assert decision.capacity_target_tier is None and decision.capacity_downsize_tier is None
    assert decision.cancel_capacity_project_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cash,enabled", [(5_000_001, True), (5_000_000, False), (4_999_999, False), (0, False)]
)
async def test_cash_threshold_controls_only_optional_spending(cash: int, enabled: bool) -> None:
    observation = _observation()
    observation.company.cash_cents = cash
    decision = await _decide(observation, RuleCEOConfig())
    assert sum(item.development_budget_cents for item in decision.products) == (
        5_000 if enabled else 0
    )
    assert sum(item.daily_budget_cents for item in decision.marketing) == (5_000 if enabled else 0)
    assert (decision.reliability_budget_cents, decision.support_budget_cents) == (2_500, 5_000)
    assert tuple(item.reorder_target for item in decision.products) == (150, 100, 50)


@pytest.mark.asyncio
async def test_no_online_reaction_to_prices_inventory_tickets_or_time() -> None:
    observation = _observation()
    config = RuleCEOConfig()
    expected = await _decide(observation, config)
    observation.days_remaining = 1
    observation.company.support_backlog = 100_000
    observation.company.inventory = {"A": 0, "B": 9_999, "C": 0}
    observation.company.prices_cents = {"A": 1, "B": 1, "C": 1}
    observation.company.promotions_cents = {"A": 1, "B": 1, "C": 1}
    observation.public_effective_prices_cents["rival"] = {"A": 1, "B": 1, "C": 1}
    assert await _decide(observation, config) == expected
    observation.company.cash_cents = config.cash_floor_cents
    await _decide(observation, config)
    observation.company.cash_cents = config.cash_floor_cents + 1
    assert await _decide(observation, config) == expected


def test_fixed_config_is_frozen_and_parameters_change_identity() -> None:
    config = RuleCEOConfig()
    with pytest.raises(ValidationError, match="frozen"):
        config.focus = "premium"
    assert config.harness_id != RuleCEOConfig(focus="premium").harness_id
    assert config.harness_id != RuleCEOConfig(price_multiplier_bps=9_000).harness_id


@pytest.mark.asyncio
async def test_wrong_context_and_negative_cash_are_rejected() -> None:
    observation = _observation()
    request = DecisionWindowRequest(
        run_id="scope",
        company_id="other",
        world_version=observation.world_version,
        observation=observation,
    )
    with _issue_company_context(observation) as context:
        result = await RuleCeoHarness().decide(request, context)
    assert result.status == "protocol_error"
    observation.company.cash_cents = -1
    request = request.model_copy(update={"company_id": "own", "observation": observation})
    with _issue_company_context(observation) as context:
        result = await RuleCeoHarness().decide(request, context)
    assert result.status == "protocol_error"
