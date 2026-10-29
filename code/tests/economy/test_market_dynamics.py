"""Shared, discoverable market transitions without identity or future-information leakage."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from ceo_arena.economy import MarketWorld
from ceo_arena.economy.market import MarketDynamics
from ceo_arena.models import (
    CompanyDecision,
    CompanyStatus,
    MarketDynamicsConfig,
    MarketPhaseConfig,
    MarketResearchRequest,
    ScenarioConfig,
    WorldState,
)

from .conftest import build_scenario


def alternating_market() -> ScenarioConfig:
    scenario = build_scenario()
    return scenario.model_copy(
        update={
            "market_dynamics": MarketDynamicsConfig(
                phases=(
                    MarketPhaseConfig(phase_id="base"),
                    MarketPhaseConfig(
                        phase_id="changed",
                        demand_multiplier=2.0,
                        price_weight_multiplier=1.5,
                        quality_weight_multiplier=1.2,
                        service_weight_multiplier=1.3,
                        quality_expectation_shift=0.1,
                    ),
                ),
                transitions=((0.0, 1.0), (1.0, 0.0)),
                publication_delay_days=1,
                daily_quality_drift=0.01,
                maximum_quality_drift=0.02,
            )
        }
    )


def empty_decisions(state: WorldState) -> dict[str, CompanyDecision]:
    return {
        key: CompanyDecision(company_id=key, world_version=state.version)
        for key, company in state.companies.items()
        if company.status is CompanyStatus.ACTIVE
    }


def test_transition_changes_actual_conditions_but_observations_are_delayed() -> None:
    scenario = alternating_market()
    world = MarketWorld(scenario)
    state = world.create_initial_state(("a", "b"))
    state = world.advance_window(state, empty_decisions(state)).state
    dynamics = MarketDynamics(scenario)
    segment = dynamics.segment(state, "mainstream")

    assert state.market.phase_index == 1
    assert segment.base_daily_demand == 80
    assert segment.price_weight == 3
    assert segment.quality_weight == pytest.approx(1.8)
    assert segment.expected_quality == pytest.approx(0.91)
    observation = world.project_observations(state)["a"]
    assert observation.published_market_signal.day == 0
    assert observation.published_market_signal.demand_index == 1
    assert "market" not in observation.model_dump()
    assert "changed" not in observation.model_dump_json()

    state = world.advance_window(state, empty_decisions(state)).state
    assert world.project_observations(state)["a"].published_market_signal.demand_index == 2


def test_phase_path_and_market_size_do_not_depend_on_company_identity_or_exit() -> None:
    scenario = alternating_market()
    first_world, second_world = MarketWorld(scenario), MarketWorld(scenario)
    first = first_world.create_initial_state(("a", "b"))
    second = second_world.create_initial_state(("unrelated-1", "unrelated-2"))
    second.companies["unrelated-2"].status = CompanyStatus.BANKRUPT
    for _ in range(5):
        first = first_world.advance_window(first, empty_decisions(first)).state
        second = second_world.advance_window(second, empty_decisions(second)).state
        assert first.market == second.market
        assert first.market_signals == second.market_signals
        assert first.initial_company_count == second.initial_company_count == 2


def test_research_samples_request_day_not_delivery_day_and_hides_unreleased_truth() -> None:
    scenario = alternating_market()
    scenario.research.level_delay_days[5] = 1
    scenario.research.level_signal_std[5] = 1e-8
    world = MarketWorld(scenario)
    initial = world.create_initial_state(("a",))
    decision = CompanyDecision(
        company_id="a",
        world_version=0,
        market_research=(MarketResearchRequest(segment_id="mainstream", level=5),),
    )
    result = world.advance_window(initial, {"a": decision})
    company = result.state.companies["a"]
    job, report = company.research_jobs[0], company.research_reports[0]
    assert job.snapshot["base_daily_demand"] == 40
    assert job.snapshot["price_weight"] == 2
    assert report.snapshot_day == 0
    assert report.completed_day == 1
    assert report.posterior["base_daily_demand"].mean == pytest.approx(40, rel=1e-5)
    assert report.posterior["price_weight"].mean == pytest.approx(2, rel=1e-5)
    assert MarketDynamics(scenario).segment(result.state, "mainstream").base_daily_demand == 80
    assert result.observations["a"].company.research_jobs[0].snapshot == {}


def test_segment_composition_is_normalized_and_quality_drift_is_bounded() -> None:
    base = build_scenario()
    first = base.segments[0]
    scenario = base.model_copy(
        update={
            "segments": (first, first.model_copy(update={"segment_id": "other"})),
            "market_dynamics": MarketDynamicsConfig(
                phases=(
                    MarketPhaseConfig(
                        phase_id="mixed",
                        demand_multiplier=1.5,
                        segment_weights={"mainstream": 3, "other": 1},
                    ),
                ),
                transitions=((1.0,),),
                daily_quality_drift=0.1,
                maximum_quality_drift=0.2,
            ),
        }
    )
    world = MarketWorld(scenario)
    state = world.create_initial_state(("a",))
    dynamics = MarketDynamics(scenario)
    for day in range(1, 10):
        dynamics.advance(state, day)
    left, right = (dynamics.segment(state, key) for key in ("mainstream", "other"))
    assert left.base_daily_demand == 90
    assert right.base_daily_demand == 30
    assert left.expected_quality == pytest.approx(first.expected_quality + 0.2)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_market_parameters_reject_nonfinite_values(value: float) -> None:
    with pytest.raises(ValidationError):
        MarketPhaseConfig(phase_id="invalid", segment_weights={"s": value})
    with pytest.raises(ValidationError):
        MarketDynamicsConfig(phases=(MarketPhaseConfig(phase_id="p"),), transitions=((value,),))


def test_invalid_transition_matrix_and_unknown_customer_group_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MarketDynamicsConfig(phases=(MarketPhaseConfig(phase_id="p"),), transitions=((0.9,),))
    raw = build_scenario().model_dump()
    raw["market_dynamics"] = MarketDynamicsConfig(
        phases=(MarketPhaseConfig(phase_id="p", segment_weights={"missing": 1}),),
        transitions=((1.0,),),
    )
    with pytest.raises(ValidationError, match="unknown customer group"):
        ScenarioConfig.model_validate(raw)
