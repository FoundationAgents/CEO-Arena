"""Tests for the sole official score and complete Leave-One-Out protocol."""

from __future__ import annotations

import math
from collections.abc import Callable

import pytest

from ceo_arena.evaluation import (
    ArenaAnalyzer,
    ArenaPlan,
    ArenaPlanner,
    ArenaRunResult,
    ModelCandidate,
    arena_lineup_id,
    arena_run_id,
    official_score,
)
from ceo_arena.models import (
    CapacityTierConfig,
    CompanyState,
    InboundShipment,
    InnovationMechanismConfig,
    MarketingMechanismConfig,
    OperationsMechanismConfig,
    ProductConfig,
    ResearchMechanismConfig,
    ScenarioConfig,
    SegmentConfig,
)
from ceo_arena.runtime import RunConfig

_RULE_MODEL_ID = "deterministic-rule-ceo"


def _scenario() -> ScenarioConfig:
    return ScenarioConfig(
        initial_cash_cents=1_000,
        products=(
            ProductConfig(
                product_id="p1",
                base_quality=1,
                unit_cost_cents=50,
                salvage_value_cents=20,
                holding_cost_cents=1,
                procurement_lead_days=1,
                initial_inventory=10,
                initial_price_cents=100,
            ),
        ),
        segments=(
            SegmentConfig(
                segment_id="s1",
                base_daily_demand=1,
                price_reference_cents=100,
                price_weight=1,
                quality_weight=1,
                service_weight=1,
                awareness_weight=1,
                loyalty_weight=1,
                macro_sensitivity=0,
                seasonal_amplitude=0,
                seasonal_period_days=7,
                expected_quality=1,
                service_level_target=1,
            ),
        ),
        capacity_tiers=(
            CapacityTierConfig(
                tier=0,
                units_per_day=1,
                daily_cost_cents=0,
                expansion_cost_cents=0,
                expansion_lead_days=1,
            ),
        ),
        marketing=MarketingMechanismConfig(
            attention_power=1,
            outside_attention=1,
            goodwill_decay=0.1,
            awareness_gain=1,
            spend_reference_cents=1,
        ),
        innovation=InnovationMechanismConfig(
            daily_depreciation=0,
            development_delay_days=1,
            quality_scale=1,
            capital_reference_cents=1,
            project_cost_cents={1: 1, 2: 2, 3: 3},
            project_median_days={1: 1, 2: 2, 3: 3},
            project_capital_gain_cents={1: 1, 2: 2, 3: 3},
        ),
        operations=OperationsMechanismConfig(
            base_failure_probability=0,
            failure_probability_floor=0,
            reliability_spend_scale_cents=1,
            support_agent_daily_cost_cents=1,
            tickets_per_agent_day=1,
            base_ticket_rate=0,
            failure_ticket_rate=0,
            satisfaction_alpha=1,
            negative_experience_weight=1,
            abandonment_daily_rate=0,
        ),
        research=ResearchMechanismConfig(
            level_cost_cents={1: 1, 2: 2, 3: 3, 4: 4, 5: 5},
            level_delay_days={1: 1, 2: 2, 3: 3, 4: 4, 5: 5},
            level_signal_std={1: 1, 2: 0.8, 3: 0.6, 4: 0.4, 5: 0.2},
            prior_std=1,
            weekly_process_std=0,
        ),
    )


def _company() -> CompanyState:
    return CompanyState(
        company_id="c1",
        cash_cents=1_300,
        initial_cash_cents=1_000,
        initial_enterprise_value_cents=1_000,
        prices_cents={"p1": 100},
        promotions_cents={"p1": 0},
        reorder_targets={"p1": 0},
        inventory={"p1": 4},
        reserved_inventory={"p1": 0},
        development_budget_cents={"p1": 0},
        knowledge_capital_cents={"p1": 0},
        cumulative_revenue_cents=500,
        cumulative_cost_cents=200,
    )


def _model(model_id: str) -> ModelCandidate:
    return ModelCandidate(
        model_id=model_id,
        harness_id="harness-v1",
    )


def _results(
    plan: ArenaPlan,
    score: Callable[[str, tuple[str, ...], int], int],
) -> tuple[ArenaRunResult, ...]:
    results: list[ArenaRunResult] = []
    for lineup in plan.lineups:
        for seed in plan.seeds:
            config = RunConfig(
                run_id=arena_run_id(lineup.lineup_id, seed),
                scenario=plan.scenario_template.model_copy(update={"seed": seed}),
                assignments=lineup.assignments,
                budget=plan.budget,
                provider_endpoint_sha256=plan.provider_endpoint_sha256,
                artifact_fingerprints=plan.artifact_fingerprints,
            )
            results.append(
                ArenaRunResult(
                    run_id=config.run_id,
                    lineup_id=lineup.lineup_id,
                    seed=seed,
                    assignments=lineup.assignments,
                    official_score_by_company_cents={
                        assignment.company_id: score(
                            assignment.model_id,
                            lineup.model_ids,
                            seed,
                        )
                        for assignment in lineup.assignments
                    },
                    decision_status_by_company={
                        assignment.company_id: {"sealed": 1} for assignment in lineup.assignments
                    },
                    config_hash=config.config_hash,
                    artifact_fingerprints=plan.artifact_fingerprints,
                )
            )
    return tuple(results)


def test_official_score_uses_declared_initial_value() -> None:
    score = official_score(_company(), _scenario())
    assert score.official_score_cents == 1_300 + 4 * 20 - 1_000
    assert score.diagnostics.cumulative_revenue_cents == 500
    assert score.diagnostics.cumulative_cost_cents == 200


def test_official_score_subtracts_explicit_initial_enterprise_value() -> None:
    company = _company().model_copy(update={"initial_enterprise_value_cents": 1_050})

    score = official_score(company, _scenario())

    assert score.official_score_cents == 1_300 + 4 * 20 - 1_050


def test_official_score_values_paid_inventory_in_transit() -> None:
    company = _company().model_copy(
        update={
            "inbound_shipments": [
                InboundShipment(
                    shipment_id="shipment-1",
                    product_id="p1",
                    quantity=3,
                    arrival_day=5,
                )
            ]
        }
    )

    score = official_score(company, _scenario())

    assert score.official_score_cents == 1_300 + (4 + 3) * 20 - 1_000


def test_planner_generates_complete_fixed_seat_leave_one_out_design() -> None:
    planner = ArenaPlanner(
        models=(_model("model-a"), _model("model-b"), _model("model-c")),
        rule_baseline=_model(_RULE_MODEL_ID),
        company_ids=("company-1", "company-2", "company-3"),
    )
    lineups = planner.lineups()

    assert planner.expected_lineup_count == 4
    assert tuple(lineup.model_ids for lineup in lineups) == (
        ("model-a", "model-b", "model-c"),
        (_RULE_MODEL_ID, "model-b", "model-c"),
        ("model-a", _RULE_MODEL_ID, "model-c"),
        ("model-a", "model-b", _RULE_MODEL_ID),
    )
    assert all(
        tuple(item.company_id for item in lineup.assignments)
        == ("company-1", "company-2", "company-3")
        for lineup in lineups
    )

    reversed_planner = ArenaPlanner(
        models=(_model("model-c"), _model("model-a"), _model("model-b")),
        rule_baseline=_model(_RULE_MODEL_ID),
        company_ids=("company-1", "company-2", "company-3"),
    )
    assert reversed_planner.lineups()[0].model_ids == ("model-c", "model-a", "model-b")


def test_arena_plan_freezes_the_seed_independent_scenario() -> None:
    planner = ArenaPlanner(
        models=(_model("model-a"), _model("model-b")),
        rule_baseline=_model(_RULE_MODEL_ID),
        company_ids=("company-1", "company-2"),
    )
    plan = planner.plan((3, 7), scenario=_scenario(), provider_endpoint_sha256="0" * 64)

    assert plan.scenario_template.seed == 0
    assert plan.scenario_template.total_days == _scenario().total_days
    payload = plan.model_dump(mode="json")
    payload["scenario_template"]["total_days"] = 1
    with pytest.raises(ValueError, match="template hash is invalid"):
        ArenaPlan.model_validate(payload)


def test_arena_plan_rejects_model_route_drift() -> None:
    planner = ArenaPlanner(
        models=(_model("model-a"), _model("model-b")),
        rule_baseline=_model(_RULE_MODEL_ID),
        company_ids=("company-1", "company-2"),
    )
    plan = planner.plan((7,), scenario=_scenario(), provider_endpoint_sha256="0" * 64)
    payload = plan.model_dump(mode="json")
    for lineup in payload["lineups"]:
        for assignment in lineup["assignments"]:
            if assignment["model_id"] == "model-a":
                assignment["harness_id"] = "harness-v2"
                with pytest.raises(ValueError, match="changes Harness or API route"):
                    ArenaPlan.model_validate(payload)
                return
    raise AssertionError("test plan unexpectedly omitted model-a")


def test_arena_plan_rejects_seat_drift() -> None:
    planner = ArenaPlanner(
        models=(_model("model-a"), _model("model-b")),
        rule_baseline=_model(_RULE_MODEL_ID),
        company_ids=("company-1", "company-2"),
    )
    plan = planner.plan((7,), scenario=_scenario(), provider_endpoint_sha256="0" * 64)
    payload = plan.model_dump(mode="json")
    assignments = payload["lineups"][0]["assignments"]
    assignments[0]["model_id"], assignments[1]["model_id"] = (
        assignments[1]["model_id"],
        assignments[0]["model_id"],
    )
    payload["lineups"][0]["lineup_id"] = arena_lineup_id(
        tuple(item["model_id"] for item in assignments)
    )

    with pytest.raises(ValueError, match="fixed-seat Leave-One-Out"):
        ArenaPlan.model_validate(payload)


def test_tracks_follow_leave_one_out_mean_stability_and_replacement_effects() -> None:
    planner = ArenaPlanner(
        models=(_model("model-a"), _model("model-b"), _model("model-c")),
        rule_baseline=_model(_RULE_MODEL_ID),
        company_ids=("company-1", "company-2", "company-3"),
    )
    score_by_model = {
        "model-a": 100,
        "model-b": 20,
        "model-c": 30,
        _RULE_MODEL_ID: 40,
    }
    plan = planner.plan((7,), scenario=_scenario(), provider_endpoint_sha256="0" * 64)
    results = _results(
        plan,
        lambda model_id, _lineup, _seed: score_by_model[model_id],
    )
    analyzer = ArenaAnalyzer(results, plan)

    analysis = analyzer.analyze()
    track_a = analysis.track_a["model-a"]
    track_b = analysis.track_b["model-a"]
    track_c = analysis.track_c["model-a"]

    assert track_a.run_count == 3
    assert track_a.mean_official_score_cents == 100
    assert track_b.scores_cents == (100, 100, 100)
    assert set(analysis.track_a) == {*score_by_model}
    assert set(analysis.track_b) == {*score_by_model}
    assert set(analysis.track_c) == {"model-a", "model-b", "model-c"}
    assert set(analysis.health) == {*score_by_model}
    assert analysis.health["model-a"].failure_rate == 0
    assert track_c.pair_count == 1
    comparison = track_c.seed_comparisons[0]
    assert comparison.background_model_ids == ("model-b", "model-c")
    assert comparison.private_difference_cents == 60
    assert comparison.directed_effects_cents == {"model-b": 0, "model-c": 0}
    assert comparison.others_difference_cents == 0
    assert comparison.system_difference_cents == 60

    with pytest.raises(ValueError, match="incomplete"):
        ArenaAnalyzer(results[:-1], plan)
    incomplete_pair = tuple(
        result
        for result in results
        if frozenset(item.model_id for item in result.assignments)
        != frozenset(("model-a", "model-b", _RULE_MODEL_ID))
    )
    with pytest.raises(ValueError, match="incomplete Track C"):
        ArenaAnalyzer(incomplete_pair).track_c("model-c", _RULE_MODEL_ID)

    drifted = results[0].model_copy(update={"config_hash": "0" * 64})
    with pytest.raises(ValueError, match="configuration drifted"):
        ArenaAnalyzer((drifted, *results[1:]), plan)


def test_track_b_separates_composition_variation_from_seed_noise() -> None:
    planner = ArenaPlanner(
        models=(_model("model-a"), _model("model-b"), _model("model-c")),
        rule_baseline=_model(_RULE_MODEL_ID),
        company_ids=("company-1", "company-2", "company-3"),
    )
    plan = planner.plan((1, 2), scenario=_scenario(), provider_endpoint_sha256="0" * 64)
    pool = {"model-a", "model-b", "model-c", _RULE_MODEL_ID}
    background_mean = {"model-b": 100, "model-c": 200, _RULE_MODEL_ID: 300}

    def score(model_id: str, lineup: tuple[str, ...], seed: int) -> int:
        if model_id != "model-a":
            return 0
        omitted = (pool - set(lineup)).pop()
        return background_mean[omitted] + (-10 if seed == 1 else 10)

    track = ArenaAnalyzer(_results(plan, score), plan).track_b("model-a")

    assert track.background_count == 3
    assert len(track.backgrounds) == 3
    assert all(len(background.seed_scores_cents) == 2 for background in track.backgrounds)
    assert track.between_background_std_cents == pytest.approx(math.sqrt(20_000 / 3))
    assert track.within_background_std_cents == 10


def test_formal_design_exposes_nine_background_means_and_three_track_c_comparisons() -> None:
    models = tuple(_model(f"model-{index}") for index in range(9))
    planner = ArenaPlanner(
        models=models,
        rule_baseline=_model(_RULE_MODEL_ID),
        company_ids=tuple(f"company-{index}" for index in range(1, 10)),
    )
    plan = planner.plan(
        (11, 29, 47),
        scenario=_scenario(),
        provider_endpoint_sha256="0" * 64,
    )

    results = _results(
        plan,
        lambda model_id, lineup, seed: 100 * lineup.index(model_id) + seed,
    )
    analysis = ArenaAnalyzer(results, plan).analyze()

    assert len(plan.lineups) == 10
    assert len(results) == 30
    assert set(analysis.track_a) == {model.model_id for model in models} | {_RULE_MODEL_ID}
    assert set(analysis.track_b) == set(analysis.track_a)
    assert set(analysis.track_c) == {model.model_id for model in models}
    assert set(analysis.health) == set(analysis.track_a)
    for distribution in analysis.track_b.values():
        assert distribution.run_count == 27
        assert distribution.background_count == 9
        assert len(distribution.backgrounds) == 9
        assert all(
            len(background.seed_scores_cents) == 3 for background in distribution.backgrounds
        )
    assert all(comparison.pair_count == 3 for comparison in analysis.track_c.values())


def test_analysis_rejects_track_c_baseline_outside_candidate_pool() -> None:
    models = tuple(_model(f"model-{index}") for index in range(3))
    plan = ArenaPlanner(
        models=models,
        rule_baseline=_model(_RULE_MODEL_ID),
        company_ids=("company-1", "company-2", "company-3"),
    ).plan((11,), scenario=_scenario(), provider_endpoint_sha256="0" * 64)
    analysis = ArenaAnalyzer(
        _results(plan, lambda model_id, lineup, seed: len(model_id) + len(lineup) + seed),
        plan,
    ).analyze()
    payload = analysis.model_dump()
    for comparison in payload["track_c"].values():
        comparison["baseline_model_id"] = "ghost-rule"

    with pytest.raises(ValueError, match="baseline must be an evaluated candidate"):
        type(analysis).model_validate(payload)


def test_track_c_decomposes_private_system_and_directed_effects() -> None:
    planner = ArenaPlanner(
        models=(_model("model-a"), _model("model-b"), _model("model-c")),
        rule_baseline=_model(_RULE_MODEL_ID),
        company_ids=("company-1", "company-2", "company-3"),
    )
    plan = planner.plan((1, 2), scenario=_scenario(), provider_endpoint_sha256="0" * 64)
    scores = {
        (1, "without-rule"): {"model-a": 100, "model-b": 50, "model-c": 30},
        (1, "without-a"): {_RULE_MODEL_ID: 40, "model-b": 45, "model-c": 35},
        (2, "without-rule"): {"model-a": 80, "model-b": 60, "model-c": 40},
        (2, "without-a"): {_RULE_MODEL_ID: 50, "model-b": 55, "model-c": 30},
    }

    def score(model_id: str, lineup: tuple[str, ...], seed: int) -> int:
        if _RULE_MODEL_ID not in lineup:
            return scores[(seed, "without-rule")][model_id]
        if "model-a" not in lineup:
            return scores[(seed, "without-a")][model_id]
        return 0

    comparison = ArenaAnalyzer(_results(plan, score), plan).track_c("model-a")

    assert comparison.pair_count == 2
    assert comparison.mean_private_difference_cents == 45
    assert comparison.mean_others_difference_cents == 7.5
    assert comparison.mean_system_difference_cents == 52.5
    assert comparison.mean_directed_effects_cents == {"model-b": 5, "model-c": 2.5}
    assert comparison.positive_system_seed_count == 2
    assert [item.private_difference_cents for item in comparison.seed_comparisons] == [60, 30]
    assert [item.others_difference_cents for item in comparison.seed_comparisons] == [0, 15]
    assert [item.system_difference_cents for item in comparison.seed_comparisons] == [60, 45]


def test_track_c_rejects_target_and_rule_in_different_seats() -> None:
    planner = ArenaPlanner(
        models=(_model("model-a"), _model("model-b")),
        rule_baseline=_model(_RULE_MODEL_ID),
        company_ids=("company-1", "company-2"),
    )
    plan = planner.plan((7,), scenario=_scenario(), provider_endpoint_sha256="0" * 64)
    results = list(_results(plan, lambda _model_id, _lineup, _seed: 0))
    baseline_index = next(
        index for index, result in enumerate(results) if not result.contains("model-a")
    )
    baseline = results[baseline_index]
    assignments = tuple(
        assignment.model_copy(
            update={
                "company_id": "company-2" if assignment.company_id == "company-1" else "company-1"
            }
        )
        for assignment in baseline.assignments
    )
    results[baseline_index] = ArenaRunResult(
        run_id=baseline.run_id,
        lineup_id=baseline.lineup_id,
        seed=baseline.seed,
        assignments=assignments,
        official_score_by_company_cents=baseline.official_score_by_company_cents,
        decision_status_by_company=baseline.decision_status_by_company,
    )

    with pytest.raises(ValueError, match="same company seat"):
        ArenaAnalyzer(tuple(results)).track_c("model-a")


@pytest.mark.parametrize("missing_value", (True, False))
def test_company_state_requires_initial_enterprise_value(missing_value: bool) -> None:
    payload = _company().model_dump()
    if missing_value:
        payload.pop("initial_enterprise_value_cents")
    else:
        payload["initial_enterprise_value_cents"] = None
    with pytest.raises(ValueError, match="initial_enterprise_value_cents"):
        CompanyState.model_validate(payload)
