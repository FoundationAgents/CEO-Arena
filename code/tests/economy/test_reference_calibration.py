"""Stylized-fact guardrails for the public retail calibration."""

from __future__ import annotations

import math

import pytest

from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.evaluation import score_world
from ceo_arena.models import (
    CompanyDecision,
    MarketingAllocation,
    ProductDecision,
    ScenarioConfig,
    WorldState,
)


def _company_share(world: MarketWorld, state: WorldState, segment_id: str) -> float:
    shares = world.expected_choice_shares(state, segment_id)
    return sum(
        share for choice, share in shares.product.items() if choice.company_id == "company-1"
    )


def _price_elasticity(world: MarketWorld, state: WorldState, segment_id: str) -> float:
    baseline = _company_share(world, state, segment_id)
    expensive = state.model_copy(deep=True)
    company = expensive.companies["company-1"]
    company.prices_cents = {
        product_id: round(price * 1.01) for product_id, price in company.prices_cents.items()
    }
    changed = _company_share(world, expensive, segment_id)
    return math.log(changed / baseline) / math.log(1.01)


def _product_policy(
    scenario: ScenarioConfig,
    *,
    price_multiplier: float,
    reorder_multiplier: float,
    development_budget_cents: int,
) -> tuple[ProductDecision, ...]:
    return tuple(
        ProductDecision(
            product_id=product.product_id,
            price_cents=round(product.initial_price_cents * price_multiplier),
            promotion_cents=0,
            reorder_target=round(product.initial_inventory * reorder_multiplier),
            development_budget_cents=development_budget_cents,
        )
        for product in scenario.products
    )


def _strategy_decision(
    scenario: ScenarioConfig,
    state: WorldState,
    company_id: str,
) -> CompanyDecision:
    common = {"company_id": company_id, "world_version": state.version}
    if company_id == "balanced":
        return CompanyDecision(
            **common,
            products=_product_policy(
                scenario,
                price_multiplier=1.0,
                reorder_multiplier=1.0,
                development_budget_cents=4_000,
            ),
            marketing=tuple(
                MarketingAllocation(
                    segment_id=segment.segment_id,
                    channel="search",
                    daily_budget_cents=4_000,
                )
                for segment in scenario.segments
            ),
            reliability_budget_cents=8_000,
            support_budget_cents=8_000,
        )
    if company_id == "premium":
        return CompanyDecision(
            **common,
            products=_product_policy(
                scenario,
                price_multiplier=1.10,
                reorder_multiplier=1.0,
                development_budget_cents=18_000,
            ),
            marketing=(
                MarketingAllocation(
                    segment_id="premium",
                    channel="display",
                    daily_budget_cents=20_000,
                ),
            ),
            reliability_budget_cents=20_000,
            support_budget_cents=20_000,
            research_project_tier=2 if state.day == 0 else None,
        )
    if company_id == "reckless":
        return CompanyDecision(
            **common,
            marketing=(
                MarketingAllocation(
                    segment_id="mainstream",
                    channel="display",
                    daily_budget_cents=20_000_000,
                ),
            ),
            reliability_budget_cents=20_000_000,
            support_budget_cents=20_000_000,
            capacity_target_tier=3 if state.day == 0 else None,
        )
    return CompanyDecision(
        **common,
        products=_product_policy(
            scenario,
            price_multiplier=1.05,
            reorder_multiplier=0.6,
            development_budget_cents=0,
        ),
        reliability_budget_cents=0,
        support_budget_cents=0,
    )


def test_reference_scenario_matches_empirical_moment_targets() -> None:
    scenario = default_scenario(total_days=1)
    world = MarketWorld(scenario)
    state = world.create_initial_state([f"company-{index}" for index in range(1, 10)])

    elasticities = {
        segment.segment_id: _price_elasticity(world, state, segment.segment_id)
        for segment in scenario.segments
    }
    expected_purchases = sum(
        segment.base_daily_demand
        * (1.0 - world.expected_choice_shares(state, segment.segment_id).outside)
        for segment in scenario.segments
    )
    installed_capacity = len(state.companies) * scenario.capacity_tiers[0].units_per_day
    gross_margins = tuple(
        (product.initial_price_cents - product.unit_cost_cents) / product.initial_price_cents
        for product in scenario.products
    )
    annual_carrying_rates = tuple(
        365 * product.holding_cost_cents / product.unit_cost_cents for product in scenario.products
    )

    assert -5.3 < elasticities["value"] < -4.7
    assert -2.7 < elasticities["mainstream"] < -2.2
    assert -1.4 < elasticities["premium"] < -0.9
    assert 0.50 < expected_purchases / installed_capacity < 0.75
    assert gross_margins == pytest.approx((0.35, 0.36, 0.37037), abs=1e-4)
    assert all(0.20 < rate < 0.30 for rate in annual_carrying_rates)


def test_reference_scenario_separates_strategies_and_bankrupts_reckless_policy() -> None:
    scenario = default_scenario(total_days=180)
    world = MarketWorld(scenario)
    state = world.create_initial_state(("austere", "balanced", "premium", "reckless"))

    while state.day < scenario.total_days:
        active = {
            company_id: _strategy_decision(scenario, state, company_id)
            for company_id, company in state.companies.items()
            if company.status.value == "active"
        }
        if not active:
            break
        state = world.advance_window(state, active).state

    scores = score_world(state, scenario)
    initial_value = state.companies["balanced"].initial_enterprise_value_cents
    assert initial_value is not None
    active_scores = [
        scores[company_id].official_score_cents for company_id in ("austere", "balanced", "premium")
    ]

    assert state.companies["reckless"].status.value == "bankrupt"
    assert all(
        state.companies[company_id].status.value == "active"
        for company_id in ("austere", "balanced", "premium")
    )
    assert max(active_scores) - min(active_scores) > initial_value * 0.10


def test_reference_scenario_keeps_cash_negative_bankruptcy_rule() -> None:
    scenario = default_scenario(total_days=1)
    world = MarketWorld(scenario)
    state = world.create_initial_state(("company-1",))
    company = state.companies["company-1"]
    company.cash_cents = 0
    company.reserved_inventory = dict(company.inventory)

    result = world.advance_window(
        state,
        {"company-1": CompanyDecision(company_id="company-1", world_version=state.version)},
    )

    bankrupt = result.state.companies["company-1"]
    assert bankrupt.cash_cents < 0
    assert sum(bankrupt.inventory.values()) > 0
    assert bankrupt.status.value == "bankrupt"
