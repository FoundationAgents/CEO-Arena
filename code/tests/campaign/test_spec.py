"""Tests for strict multi-key specs and credential-free campaign compilation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ceo_arena.campaign import (
    RULE_MODEL_ID,
    CampaignCompiler,
    CampaignExecutionSpec,
    ExperimentSpec,
    FrozenCampaignPlan,
    load_experiment_spec,
)
from ceo_arena.campaign.models import MAX_CONCURRENT_RUNS
from ceo_arena.config import default_scenario

_KEYS = ("temporary-key-one", "temporary-key-two", "temporary-key-three")


def test_default_campaign_caps_only_runs_and_never_models_or_retries() -> None:
    execution = CampaignExecutionSpec()
    assert execution.max_concurrent_runs == MAX_CONCURRENT_RUNS == 30
    assert execution.max_run_attempts is None
    assert execution.max_inflight_decisions is None
    assert execution.max_idle_workers is None


@pytest.mark.parametrize("limit", (0, 31))
def test_campaign_rejects_out_of_range_concurrency(limit: int) -> None:
    with pytest.raises(ValidationError):
        CampaignExecutionSpec(max_concurrent_runs=limit)


@pytest.mark.parametrize("limit", (1, 20, 30))
def test_campaign_accepts_configurable_concurrency_up_to_thirty(limit: int) -> None:
    assert CampaignExecutionSpec(max_concurrent_runs=limit).max_concurrent_runs == limit


def _toml(*, keys: tuple[str, str, str] = _KEYS) -> str:
    return f'''\
schema_version = 1
campaign_id = "retail-formal"
base_url = "https://newapi.example/v1/"
scenario = "scenario.json"
company_count = 4
days = 500
seeds = [11, 29]

[[api]]
api_key = "{keys[0]}"
models = ["gpt-5.6-sol"]

[[api]]
api_key = "{keys[1]}"
models = ["gpt-5.6-terra", "gpt-5.6-luna"]

[[api]]
api_key = "{keys[2]}"
models = ["qwen/qwen3-235b-a22b"]

[execution]
max_concurrent_runs = 5
max_inflight_decisions = 32
max_run_attempts = 3
schedule = "seed_wave"
'''


def _load(tmp_path: Path, *, keys: tuple[str, str, str] = _KEYS) -> ExperimentSpec:
    tmp_path.mkdir(parents=True, exist_ok=True)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(default_scenario().model_dump_json(), encoding="utf-8")
    spec_path = tmp_path / "formal.local.toml"
    spec_path.write_text(_toml(keys=keys), encoding="utf-8")
    return load_experiment_spec(spec_path)


def test_toml_compiles_complete_fixed_seat_leave_one_out_design(tmp_path: Path) -> None:
    spec = _load(tmp_path)
    plan = CampaignCompiler().compile(spec, simulation_fingerprints={"runtime": "v1"})

    assert spec.base_url == "https://newapi.example/v1"
    assert spec.scenario == (tmp_path / "scenario.json").resolve()
    assert spec.model_ids == (
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "qwen/qwen3-235b-a22b",
    )
    assert len(plan.routes) == 4
    assert {route.model_id: route.api_index for route in plan.routes} == {
        "gpt-5.6-luna": 1,
        "gpt-5.6-sol": 0,
        "gpt-5.6-terra": 1,
        "qwen/qwen3-235b-a22b": 2,
    }
    assert tuple(lineup.model_ids for lineup in plan.lineups) == (
        (
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "qwen/qwen3-235b-a22b",
        ),
        (
            RULE_MODEL_ID,
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "qwen/qwen3-235b-a22b",
        ),
        (
            "gpt-5.6-sol",
            RULE_MODEL_ID,
            "gpt-5.6-luna",
            "qwen/qwen3-235b-a22b",
        ),
        (
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            RULE_MODEL_ID,
            "qwen/qwen3-235b-a22b",
        ),
        ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", RULE_MODEL_ID),
    )
    assert len(plan.runs) == 10
    assert sum(RULE_MODEL_ID in lineup.model_ids for lineup in plan.lineups) == 4
    assert all(len(lineup.model_ids) == 4 for lineup in plan.lineups)
    assert all(
        run.company_ids == ("company-1", "company-2", "company-3", "company-4") for run in plan.runs
    )
    assert all(
        api_index is None
        for run in plan.runs
        for model_id, api_index in zip(run.model_ids, run.api_indices, strict=True)
        if model_id == RULE_MODEL_ID
    )
    assert plan.scenario_template.seed == 0
    assert plan.scenario_template.total_days == 500
    assert plan.simulation_fingerprints == {"runtime": "v1"}


def test_estimate_matches_complete_k4_n4_matrix(tmp_path: Path) -> None:
    plan = CampaignCompiler().compile(_load(tmp_path))
    estimate = plan.estimate

    assert estimate.candidate_model_count == 4
    assert estimate.company_count == 4
    assert estimate.seed_count == 2
    assert estimate.lineup_count == 5
    assert estimate.run_count == 10
    assert estimate.decision_window_days == 7
    assert estimate.decision_windows_per_trajectory == 72
    assert estimate.total_company_trajectories == 40
    assert estimate.model_company_trajectories == 32
    assert estimate.rule_company_trajectories == 8
    assert estimate.maximum_model_decisions == 2_304
    assert estimate.maximum_rule_decisions == 576


def test_formal_k9_design_has_ten_lineups_and_twenty_seven_scores_per_identity() -> None:
    models = tuple(f"model-{index}" for index in range(1, 10))
    plan = CampaignCompiler().compile(
        ExperimentSpec(
            campaign_id="formal-k9",
            base_url="https://newapi.example/v1",
            company_count=9,
            days=500,
            seeds=(11, 29, 47),
            api=({"api_key": "temporary", "models": models},),
        )
    )

    appearances = {
        model_id: sum(model_id in run.model_ids for run in plan.runs)
        for model_id in (*models, RULE_MODEL_ID)
    }
    assert len(plan.lineups) == 10
    assert len(plan.runs) == 30
    assert plan.estimate.total_company_trajectories == 270
    assert set(appearances.values()) == {27}


def test_compiler_requires_explicit_opt_in_before_large_materialization() -> None:
    models = tuple(f"model-{i}" for i in range(500))
    spec = ExperimentSpec(
        campaign_id="large",
        base_url="https://newapi.example/v1",
        company_count=len(models),
        days=7,
        seeds=(1,),
        api=({"api_key": "temporary", "models": models},),
    )

    with pytest.raises(ValueError, match="allow_large_campaign"):
        CampaignCompiler().compile(spec)


def test_explicit_model_order_is_independent_of_api_grouping() -> None:
    spec = ExperimentSpec(
        campaign_id="ordered",
        base_url="https://newapi.example/v1",
        company_count=3,
        days=7,
        seeds=(1,),
        api=(
            {"api_key": "key-a", "models": ("model-a", "model-c")},
            {"api_key": "key-b", "models": ("model-b",)},
        ),
        model_order=("model-a", "model-b", "model-c"),
    )

    plan = CampaignCompiler().compile(spec)

    assert plan.model_order == ("model-a", "model-b", "model-c")
    assert plan.lineups[0].model_ids == ("model-a", "model-b", "model-c")


def test_estimate_uses_the_frozen_scenario_window_size(tmp_path: Path) -> None:
    scenario = default_scenario().model_copy(update={"days_per_decision_window": 9})
    plan = CampaignCompiler().compile(_load(tmp_path), scenario=scenario)

    assert plan.estimate.decision_window_days == 9
    assert plan.estimate.decision_windows_per_trajectory == 56


def test_keys_never_enter_plan_serialization_repr_or_hash(tmp_path: Path) -> None:
    first_spec = _load(tmp_path)
    second_spec = _load(
        tmp_path,
        keys=("replacement-one", "replacement-two", "replacement-three"),
    )

    first = CampaignCompiler().compile(first_spec)
    second = CampaignCompiler().compile(second_spec)
    serialized = first.model_dump_json()
    rendered = repr(first)

    assert first == second
    assert first_spec.spec_hash == second_spec.spec_hash
    assert first.plan_hash == second.plan_hash
    assert "api_key" not in serialized
    assert "competitor_id" not in serialized
    assert all(secret not in serialized and secret not in rendered for secret in _KEYS)
    assert all(secret not in repr(first_spec) for secret in _KEYS)


def test_frozen_task_is_sufficient_to_rebuild_fixed_assignments(tmp_path: Path) -> None:
    plan = CampaignCompiler().compile(_load(tmp_path))
    task = next(run for run in plan.runs if RULE_MODEL_ID in run.model_ids)

    assignments = tuple(zip(task.company_ids, task.model_ids, task.api_indices, strict=True))
    assert len(assignments) == plan.company_count
    assert len({company_id for company_id, _, _ in assignments}) == plan.company_count
    assert next(index for _, model_id, index in assignments if model_id == RULE_MODEL_ID) is None
    assert task.task_key.startswith("task-")
    assert task.run_id.startswith("run-")
    assert task.total_days == 500


def test_run_ids_are_scoped_to_the_campaign(tmp_path: Path) -> None:
    first_spec = _load(tmp_path / "first")
    second_spec = _load(tmp_path / "second").model_copy(update={"campaign_id": "another-campaign"})

    first = CampaignCompiler().compile(first_spec)
    second = CampaignCompiler().compile(second_spec)

    assert first.lineups == second.lineups
    assert {run.run_id for run in first.runs}.isdisjoint(run.run_id for run in second.runs)
    assert {run.task_key for run in first.runs}.isdisjoint(run.task_key for run in second.runs)


def test_spec_hash_uses_scenario_content_via_plan_not_the_local_path(tmp_path: Path) -> None:
    first_spec = _load(tmp_path / "first")
    second_spec = _load(tmp_path / "second")

    assert first_spec.spec_hash == second_spec.spec_hash
    assert (
        CampaignCompiler().compile(first_spec).plan_hash
        == CampaignCompiler().compile(second_spec).plan_hash
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "schema_version": 1,
                "campaign_id": "duplicate",
                "base_url": "https://newapi.example/v1",
                "company_count": 1,
                "days": 7,
                "seeds": [1],
                "api": [
                    {"api_key": "key-1", "models": ["same-model"]},
                    {"api_key": "key-2", "models": ["same-model"]},
                ],
            },
            "globally unique",
        ),
        (
            {
                "schema_version": 1,
                "campaign_id": "duplicate-key",
                "base_url": "https://newapi.example/v1",
                "company_count": 1,
                "days": 7,
                "seeds": [1],
                "api": [
                    {"api_key": "same-key", "models": ["m1"]},
                    {"api_key": "same-key", "models": ["m2"]},
                ],
            },
            "distinct keys",
        ),
        (
            {
                "schema_version": 1,
                "campaign_id": "reserved",
                "base_url": "https://newapi.example/v1",
                "company_count": 1,
                "days": 7,
                "seeds": [1],
                "api": [{"api_key": "key", "models": [RULE_MODEL_ID]}],
            },
            "added automatically",
        ),
        (
            {
                "schema_version": 1,
                "campaign_id": "no-substitute",
                "base_url": "https://newapi.example/v1",
                "company_count": 2,
                "days": 7,
                "seeds": [1],
                "api": [{"api_key": "key", "models": ["only-model"]}],
            },
            "must equal the number",
        ),
        (
            {
                "schema_version": 1,
                "campaign_id": "alias-is-forbidden",
                "base_url": "https://newapi.example/v1",
                "company_count": 1,
                "days": 7,
                "seeds": [1],
                "api": [{"api_key": "key", "models": ["m1"]}],
                "competitor_id": "A",
            },
            "competitor_id",
        ),
    ],
)
def test_spec_rejects_ambiguous_or_legacy_identity_fields(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ExperimentSpec.model_validate(payload)


def test_plan_rejects_route_or_task_tampering(tmp_path: Path) -> None:
    plan = CampaignCompiler().compile(_load(tmp_path))
    route_payload = plan.model_dump(mode="json")
    route_payload["routes"][0]["api_index"] = 2
    with pytest.raises(ValidationError, match="routing differs"):
        FrozenCampaignPlan.model_validate(route_payload)

    task_payload = plan.model_dump(mode="json")
    task_payload["runs"][0]["model_ids"] = list(reversed(task_payload["runs"][0]["model_ids"]))
    with pytest.raises(ValidationError, match="assignments differ"):
        FrozenCampaignPlan.model_validate(task_payload)


def test_plan_rejects_lineup_seat_drift(tmp_path: Path) -> None:
    payload = CampaignCompiler().compile(_load(tmp_path)).model_dump(mode="json")
    lineup = payload["lineups"][1]
    lineup["model_ids"][0], lineup["model_ids"][1] = (
        lineup["model_ids"][1],
        lineup["model_ids"][0],
    )

    with pytest.raises(ValidationError, match="fixed-seat"):
        FrozenCampaignPlan.model_validate(payload)
