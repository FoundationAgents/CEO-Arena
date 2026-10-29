"""Installed capacity has conditional operating value, not an unconditional profit bonus."""

import math

from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.models import (
    BacklogOrder,
    CompanyDayOutcome,
    CompanyDecision,
    OrderSource,
    ScenarioConfig,
)


def capacity_day(scenario: ScenarioConfig, *, tier: int, backlog: int) -> CompanyDayOutcome:
    world = MarketWorld(scenario)
    state = world.create_initial_state(tuple(f"company-{i}" for i in range(9)))
    focal = state.companies["company-0"]
    focal.capacity_tier = tier
    if backlog:
        focal.backlog.append(
            BacklogOrder(
                order_id="existing-demand",
                source=OrderSource.CONSUMER,
                product_id="A",
                segment_id="value",
                quantity=backlog,
                unit_price_cents=4_000,
                created_day=0,
            )
        )
    result = world.advance_window(
        state, {key: CompanyDecision(company_id=key, world_version=0) for key in state.companies}
    )
    return result.days[0].company_outcomes["company-0"]


def test_capacity_has_positive_operating_value_when_existing_demand_exceeds_capacity() -> None:
    scenario = default_scenario(total_days=1)
    scenario.operations.abandonment_daily_rate = 0
    base = capacity_day(scenario, tier=0, backlog=90)
    expanded = capacity_day(scenario, tier=1, backlog=90)
    daily_gain = expanded.net_cashflow_cents - base.net_cashflow_cents
    assert base.fulfilled_units == 60
    assert expanded.fulfilled_units == 90
    assert daily_gain > 0
    capital = scenario.capacity_tiers[1].expansion_cost_cents
    # A conditional break-even check if this measured load persists, not a 500-day forecast.
    assert math.ceil(capital / daily_gain) + scenario.capacity_tiers[1].expansion_lead_days < 500


def test_idle_capacity_loses_money_without_extra_demand() -> None:
    scenario = default_scenario(total_days=1)
    scenario.segments = tuple(
        segment.model_copy(update={"base_daily_demand": 1e-9}) for segment in scenario.segments
    )
    base = capacity_day(scenario, tier=0, backlog=0)
    expanded = capacity_day(scenario, tier=1, backlog=0)
    assert base.fulfilled_units == expanded.fulfilled_units == 0
    assert expanded.net_cashflow_cents - base.net_cashflow_cents == -13_000
