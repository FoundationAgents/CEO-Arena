"""Behavior contracts for the shared economic runtime."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld, ProductChoice
from ceo_arena.models import (
    BacklogOrder,
    CompanyDecision,
    EnterpriseBid,
    MarketingAllocation,
    MarketResearchRequest,
    OrderSource,
    ProductDecision,
    ScenarioConfig,
    WorldState,
)

from .conftest import build_scenario


def decisions(state: WorldState, **overrides: CompanyDecision) -> dict[str, CompanyDecision]:
    """Create one current-version decision per active company."""
    return {
        company_id: overrides.get(
            company_id,
            CompanyDecision(company_id=company_id, world_version=state.version),
        )
        for company_id in state.companies
        if state.companies[company_id].status.value == "active"
    }


def outcome_signature(result, company_ids: Iterable[str]) -> dict[str, tuple[int, int, int, int]]:
    """Extract stable company outcomes without depending on mapping insertion order."""
    return {
        company_id: (
            result.state.companies[company_id].cash_cents,
            result.state.companies[company_id].inventory["standard"],
            result.state.companies[company_id].cumulative_consumer_orders,
            result.state.companies[company_id].support_backlog,
        )
        for company_id in company_ids
    }


def test_initial_state_is_symmetric_and_observations_hide_bids(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    state = world.create_initial_state(["beta", "alpha"])
    tender = state.tenders[0]
    tender.bids["alpha"] = 900

    observations = world.project_observations(state)

    assert list(state.companies) == ["alpha", "beta"]
    assert (
        state.companies["alpha"].model_copy(update={"company_id": "beta"})
        == state.companies["beta"]
    )
    assert observations["alpha"].open_tenders[0].bids == {}
    assert observations["alpha"].company is not state.companies["alpha"]
    assert observations["alpha"].tender_history[0].own_bid_cents == 900
    assert observations["alpha"].tender_history[0].status == "submitted"
    assert observations["beta"].tender_history[0].own_bid_cents is None


def test_price_monotonicity_and_outside_option(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha", "beta"])
    baseline = world.expected_choice_shares(state, "mainstream")
    expensive = state.model_copy(deep=True)
    expensive.companies["alpha"].prices_cents["standard"] = 3_000
    changed = world.expected_choice_shares(expensive, "mainstream")

    alpha = ProductChoice("alpha", "standard")
    beta = ProductChoice("beta", "standard")
    assert changed.product[alpha] < baseline.product[alpha]
    assert changed.product[beta] > baseline.product[beta]
    assert changed.outside > baseline.outside
    assert baseline.total == pytest.approx(1.0)


def test_demand_is_finite_and_inventory_backlog_stay_nonnegative() -> None:
    scenario = build_scenario(demand=250.0, inventory=3, capacity=2, abandonment_rate=0.25)
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha", "beta"])

    result = world.advance_window(state, decisions(state))
    day = result.days[0]

    assert sum(item.consumer_orders for item in day.company_outcomes.values()) <= sum(
        day.demand_by_segment.values()
    )
    for company in result.state.companies.values():
        assert all(quantity >= 0 for quantity in company.inventory.values())
        assert all(order.quantity > 0 for order in company.backlog)
        assert company.support_backlog >= 0
    assert state.day == 0, "advance_window must not mutate its input snapshot"


def test_stockout_substitution_is_not_counted_as_abandonment() -> None:
    scenario = build_scenario(
        demand=300.0,
        inventory=2,
        capacity=500,
        abandonment_rate=0.0,
        stockout_switch_probability=1.0,
    )
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha", "beta"])

    result = world.advance_window(state, decisions(state))

    assert sum(item.abandoned_units for item in result.days[0].company_outcomes.values()) == 0


def test_enterprise_reserved_inventory_is_not_sold_to_consumers() -> None:
    scenario = build_scenario(demand=200.0, inventory=10, capacity=500)
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha"])
    state.companies["alpha"].reserved_inventory["standard"] = 10

    result = world.advance_window(state, decisions(state))

    assert result.days[0].company_outcomes["alpha"].consumer_orders == 0


def test_mapping_order_does_not_change_world_state(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    first = world.create_initial_state(["alpha", "beta"])
    second = world.create_initial_state(["beta", "alpha"])
    alpha = CompanyDecision(
        company_id="alpha",
        world_version=0,
        products=(ProductDecision(product_id="standard", price_cents=900),),
    )
    beta = CompanyDecision(
        company_id="beta",
        world_version=0,
        products=(ProductDecision(product_id="standard", price_cents=1_100),),
    )

    left = world.advance_window(first, {"alpha": alpha, "beta": beta})
    right = world.advance_window(second, {"beta": beta, "alpha": alpha})

    assert world.state_hash(left.state) == world.state_hash(right.state)
    assert outcome_signature(left, ["alpha", "beta"]) == outcome_signature(right, ["alpha", "beta"])


def test_macro_observation_is_published_with_delay(scenario: ScenarioConfig) -> None:
    scenario = scenario.model_copy(
        update={"macro_volatility": 0.2, "macro_publication_delay_days": 1}
    )
    world = MarketWorld(scenario)
    initial = world.create_initial_state(["alpha"])

    result = world.advance_window(initial, decisions(initial))
    observation = result.observations["alpha"]

    assert result.state.macro_index != initial.macro_index
    assert observation.published_macro_index == initial.macro_index


def test_innovation_quality_has_diminishing_marginal_returns(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha", "beta"])
    capital = scenario.innovation.capital_reference_cents

    shares: list[float] = []
    for multiplier in (0, 1, 2):
        candidate = state.model_copy(deep=True)
        candidate.companies["alpha"].knowledge_capital_cents["standard"] = multiplier * capital
        shares.append(
            world.expected_choice_shares(candidate, "mainstream").product[
                ProductChoice("alpha", "standard")
            ]
        )

    assert shares[1] > shares[0]
    assert shares[2] > shares[1]
    assert shares[1] - shares[0] > shares[2] - shares[1]


def test_research_project_timing_and_gain_are_seeded_positive_draws(
    scenario: ScenarioConfig,
) -> None:
    first_world = MarketWorld(scenario)
    second_world = MarketWorld(scenario)
    first = first_world.create_initial_state(["alpha"])
    second = second_world.create_initial_state(["alpha"])
    decision = CompanyDecision(
        company_id="alpha",
        world_version=0,
        research_project_tier=2,
    )

    first_result = first_world.advance_window(first, {"alpha": decision})
    second_result = second_world.advance_window(second, {"alpha": decision})
    first_project = first_result.state.companies["alpha"].innovation_projects[0]
    second_project = second_result.state.companies["alpha"].innovation_projects[0]

    assert first_project == second_project
    assert first_project.completion_day > 0
    assert first_project.capital_gain_cents > 0


def test_pending_random_research_outcomes_remain_hidden(scenario: ScenarioConfig) -> None:
    innovation = scenario.innovation.model_copy(
        update={"project_median_days": {1: 20, 2: 30, 3: 40}}
    )
    scenario = scenario.model_copy(update={"innovation": innovation})
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha"])
    decision = CompanyDecision(company_id="alpha", world_version=0, research_project_tier=2)

    result = world.advance_window(state, {"alpha": decision})
    internal = result.state.companies["alpha"].innovation_projects[0]
    observation = result.observations["alpha"]
    visible = observation.innovation_projects[0]

    assert internal.completion_day > result.state.day
    assert observation.company.innovation_projects == []
    assert visible.completed_day is None
    assert visible.realized_capital_gain_cents is None
    assert visible.median_completion_day == internal.started_day + 30


def test_marketing_builds_goodwill_then_decays(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha", "beta"])
    alpha = CompanyDecision(
        company_id="alpha",
        world_version=0,
        marketing=(
            MarketingAllocation(
                segment_id="mainstream", channel="search", daily_budget_cents=4_000
            ),
        ),
    )
    marketed = world.advance_window(state, decisions(state, alpha=alpha))
    peak = marketed.state.companies["alpha"].goodwill["mainstream"]
    quiet_state = marketed.state
    stop_marketing = CompanyDecision(
        company_id="alpha",
        world_version=quiet_state.version,
        marketing=(
            MarketingAllocation(segment_id="mainstream", channel="search", daily_budget_cents=0),
        ),
    )
    quiet = world.advance_window(quiet_state, decisions(quiet_state, alpha=stop_marketing))

    assert peak > state.companies["alpha"].goodwill["mainstream"]
    assert quiet.state.companies["alpha"].goodwill["mainstream"] < peak
    assert (
        quiet.state.companies["alpha"].awareness["mainstream"]
        < marketed.state.companies["alpha"].awareness["mainstream"]
    )


def test_empty_decision_preserves_every_recurring_policy(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha"])
    company = state.companies["alpha"]
    company.prices_cents["standard"] = 1_234
    company.promotions_cents["standard"] = 100
    company.reorder_targets["standard"] = 321
    company.development_budget_cents["standard"] = 456
    company.marketing_budget_cents["mainstream"]["search"] = 123
    company.reliability_budget_cents = 789
    company.support_budget_cents = 987

    result = world.advance_window(state, decisions(state))
    preserved = result.state.companies["alpha"]

    assert preserved.prices_cents["standard"] == 1_234
    assert preserved.promotions_cents["standard"] == 100
    assert preserved.reorder_targets["standard"] == 321
    assert preserved.development_budget_cents["standard"] == 456
    assert preserved.marketing_budget_cents["mainstream"]["search"] == 123
    assert preserved.reliability_budget_cents == 789
    assert preserved.support_budget_cents == 987


def test_empty_decision_does_not_repeat_one_time_actions(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha"])
    one_time = CompanyDecision(
        company_id="alpha",
        world_version=state.version,
        research_project_tier=1,
        market_research=(MarketResearchRequest(segment_id="mainstream", level=1),),
    )

    started = world.advance_window(state, {"alpha": one_time})
    continued = world.advance_window(started.state, decisions(started.state))

    company = continued.state.companies["alpha"]
    assert len(company.innovation_projects) == 1
    assert len(company.research_jobs) == 1


def test_marketing_channels_have_segment_specific_effectiveness(
    scenario: ScenarioConfig,
) -> None:
    segment = scenario.segments[0].model_copy(
        update={"channel_effectiveness": {"search": 2.0, "display": 0.5}}
    )
    scenario = scenario.model_copy(update={"segments": (segment,)})
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha", "beta"])
    alpha = CompanyDecision(
        company_id="alpha",
        world_version=0,
        marketing=(
            MarketingAllocation(
                segment_id="mainstream", channel="search", daily_budget_cents=1_000
            ),
        ),
    )
    beta = CompanyDecision(
        company_id="beta",
        world_version=0,
        marketing=(
            MarketingAllocation(
                segment_id="mainstream", channel="display", daily_budget_cents=1_000
            ),
        ),
    )

    result = world.advance_window(state, {"alpha": alpha, "beta": beta})

    assert (
        result.state.companies["alpha"].goodwill["mainstream"]
        > result.state.companies["beta"].goodwill["mainstream"]
    )


def test_capacity_expansion_is_delayed(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha"])
    expand = CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=1)

    day_one = world.advance_window(state, {"alpha": expand})
    day_two = world.advance_window(day_one.state, decisions(day_one.state))

    assert day_one.state.companies["alpha"].capacity_tier == 0
    assert day_two.state.companies["alpha"].capacity_tier == 1


def test_lowest_qualified_tender_bid_has_one_winner(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha", "beta", "gamma"])
    tender = state.tenders[0]
    tender.minimum_quality = 0.0
    bids = {
        company_id: CompanyDecision(
            company_id=company_id,
            world_version=0,
            enterprise_bids=(EnterpriseBid(tender_id=tender.tender_id, unit_price_cents=price),),
        )
        for company_id, price in {"alpha": 900, "beta": 850, "gamma": 950}.items()
    }

    result = world.advance_window(state, bids)
    settled = result.state.tenders[0]

    assert settled.settled
    assert settled.winner_company_id == "beta"
    assert settled.winning_unit_price_cents == 850
    assert result.observations["beta"].tender_history[0].status == "won"
    assert result.observations["alpha"].tender_history[0].status == "lost"
    assert result.observations["beta"].tender_history[0].winning_unit_price_cents == 850
    assert (
        sum(
            order.source.value == "enterprise"
            for company in result.state.companies.values()
            for order in company.backlog
        )
        == 1
    )


def test_tender_quantity_uses_versioned_reference_scale() -> None:
    current = default_scenario(total_days=30)
    legacy = current.model_copy(update={"scenario_id": "ceos-retail-v1"})
    typical_demand = sum(segment.base_daily_demand for segment in current.segments)

    current_tender = MarketWorld(current).create_initial_state(["alpha"]).tenders[0]
    legacy_tender = MarketWorld(legacy).create_initial_state(["alpha"]).tenders[0]

    assert round(typical_demand * 0.15) <= current_tender.quantity <= round(typical_demand * 0.45)
    assert round(typical_demand * 0.5) <= legacy_tender.quantity <= round(typical_demand * 1.5)


def test_tender_rejects_cheapest_company_without_inventory(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha", "beta"])
    tender = state.tenders[0]
    tender.quantity = 1
    tender.minimum_quality = 0.0
    state.companies["beta"].inventory[tender.product_id] = 0
    bids = {
        "alpha": CompanyDecision(
            company_id="alpha",
            world_version=0,
            enterprise_bids=(EnterpriseBid(tender_id=tender.tender_id, unit_price_cents=900),),
        ),
        "beta": CompanyDecision(
            company_id="beta",
            world_version=0,
            enterprise_bids=(EnterpriseBid(tender_id=tender.tender_id, unit_price_cents=800),),
        ),
    }

    result = world.advance_window(state, bids)

    assert result.state.tenders[0].winner_company_id == "alpha"


def test_higher_level_research_has_lower_posterior_variance(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    low_state = world.create_initial_state(["alpha"])
    high_state = world.create_initial_state(["alpha"])
    low = CompanyDecision(
        company_id="alpha",
        world_version=0,
        market_research=(MarketResearchRequest(segment_id="mainstream", level=1),),
    )
    high = CompanyDecision(
        company_id="alpha",
        world_version=0,
        market_research=(MarketResearchRequest(segment_id="mainstream", level=5),),
    )

    low_result = world.advance_window(low_state, {"alpha": low})
    high_result = world.advance_window(high_state, {"alpha": high})
    low_report = low_result.state.companies["alpha"].research_reports[0]
    high_report = high_result.state.companies["alpha"].research_reports[0]

    assert (
        high_report.posterior["base_daily_demand"].variance
        < low_report.posterior["base_daily_demand"].variance
    )
    assert (
        high_report.posterior["price_weight"].variance
        < low_report.posterior["price_weight"].variance
    )
    assert all(belief.mean > 0 for belief in low_report.posterior.values())


def test_initial_research_beliefs_do_not_reveal_segment_truth() -> None:
    scenario = default_scenario(total_days=1)
    world = MarketWorld(scenario)
    company = world.create_initial_state(["alpha"]).companies["alpha"]
    truth = {segment.segment_id: segment.base_daily_demand for segment in scenario.segments}

    assert any(
        company.beliefs[segment_id]["base_daily_demand"].mean != demand
        for segment_id, demand in truth.items()
    )


def test_waiting_backlog_reduces_customer_satisfaction(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    clear = world.create_initial_state(["alpha"])
    congested = clear.model_copy(deep=True)
    congested.companies["alpha"].backlog.append(
        BacklogOrder(
            order_id="old-order",
            source=OrderSource.CONSUMER,
            product_id="standard",
            segment_id="mainstream",
            quantity=500,
            unit_price_cents=1_000,
            created_day=0,
        )
    )

    clear_result = world.advance_window(clear, decisions(clear))
    congested_result = world.advance_window(congested, decisions(congested))

    assert (
        congested_result.state.companies["alpha"].satisfaction["mainstream"]
        < clear_result.state.companies["alpha"].satisfaction["mainstream"]
    )


def test_higher_price_raises_quality_expectation_and_reduces_satisfaction(
    scenario: ScenarioConfig,
) -> None:
    scenario = scenario.model_copy(
        update={
            "operations": scenario.operations.model_copy(
                update={
                    "base_failure_probability": 0.0,
                    "failure_probability_floor": 0.0,
                    "base_ticket_rate": 0.0,
                    "failure_ticket_rate": 0.0,
                }
            )
        }
    )
    world = MarketWorld(scenario)
    regular = world.create_initial_state(["alpha"])
    expensive = world.create_initial_state(["alpha"])
    expensive_decision = CompanyDecision(
        company_id="alpha",
        world_version=0,
        products=(ProductDecision(product_id="standard", price_cents=1_200),),
    )

    regular_result = world.advance_window(regular, decisions(regular))
    expensive_result = world.advance_window(expensive, {"alpha": expensive_decision})

    assert (
        expensive_result.state.companies["alpha"].satisfaction["mainstream"]
        < regular_result.state.companies["alpha"].satisfaction["mainstream"]
    )


def test_base_stock_orders_only_the_inventory_position_gap(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha"])
    company = state.companies["alpha"]
    company.inventory["standard"] = 10
    target = 35
    reorder = CompanyDecision(
        company_id="alpha",
        world_version=0,
        products=(ProductDecision(product_id="standard", reorder_target=target),),
    )

    result = world.advance_window(state, {"alpha": reorder})
    shipment = result.state.companies["alpha"].inbound_shipments[0]

    assert shipment.quantity <= target
    assert shipment.quantity == max(
        0,
        target - result.state.companies["alpha"].inventory["standard"],
    )


def test_stale_or_incomplete_decision_set_is_rejected(scenario: ScenarioConfig) -> None:
    world = MarketWorld(scenario)
    state = world.create_initial_state(["alpha", "beta"])

    with pytest.raises(ValueError, match="missing"):
        world.advance_window(
            state,
            {"alpha": CompanyDecision(company_id="alpha", world_version=state.version)},
        )
    with pytest.raises(ValueError, match="stale"):
        world.advance_window(
            state,
            {
                "alpha": CompanyDecision(company_id="alpha", world_version=1),
                "beta": CompanyDecision(company_id="beta", world_version=1),
            },
        )
