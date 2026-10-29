"""Selection is development-only; validation reuses its winner and normal runtime."""

from pathlib import Path

import pytest

from ceo_arena.evaluation.rule_search import (
    BACKGROUND_FOCI,
    FixedRuleSearch,
    RuleSearchPlan,
    SearchCaseResult,
    candidate_grid,
    search_fingerprints,
    select_winner,
)

ROOT = Path(__file__).resolve().parents[2]


def test_search_grid_has_24_configs_and_fixed_competition() -> None:
    plan = RuleSearchPlan.official(ROOT)
    assert len(candidate_grid()) == 24
    assert len({item.config.harness_id for item in plan.candidates}) == 24
    assert plan.development_seeds == (7, 19, 42)
    assert plan.validation_seeds == (211, 307, 401)
    assert tuple(item.focus for item in plan.background) == BACKGROUND_FOCI
    assert all(item.price_multiplier_bps == 10_000 for item in plan.background)
    assert all(item.cash_floor_cents == 5_000_000 for item in plan.background)
    assert plan.scenario.total_days == 500
    assert all(item.config.reorder_targets == (150, 100, 50) for item in plan.candidates)


def test_search_binds_algorithm_but_not_the_eventual_installed_profile() -> None:
    fingerprints = search_fingerprints(ROOT)
    assert "src/ceo_arena/harness/rule_ceo.py" in fingerprints
    assert "src/ceo_arena/evaluation/rule_search.py" in fingerprints
    assert "scripts/search_rule.py" in fingerprints
    assert "src/ceo_arena/harness/rule_profile.json" not in fingerprints


def test_validation_seeds_cannot_overlap_development() -> None:
    plan = RuleSearchPlan.official(ROOT)
    with pytest.raises(ValueError, match="validation seeds"):
        RuleSearchPlan.model_validate({**plan.model_dump(), "validation_seeds": (7,)})


def _results(plan: RuleSearchPlan) -> tuple[SearchCaseResult, ...]:
    return tuple(
        SearchCaseResult(
            plan_hash=plan.plan_hash,
            split="development",
            candidate_id=candidate.candidate_id,
            config=candidate.config,
            seed=seed,
            run_id="fixture",
            run_config_hash="fixture",
            terminal_day=500,
            focal_score_cents=100,
            companies=(),
            final_state_hash="fixture",
            replay_verified_days=500,
            decision_count=1,
            elapsed_seconds=0,
            artifacts="fixture",
        )
        for candidate in plan.candidates
        for seed in plan.development_seeds
    )


def test_selection_rejects_incomplete_grid_and_ties_use_predeclared_order() -> None:
    plan = RuleSearchPlan.official(ROOT)
    rows = _results(plan)
    assert select_winner(plan, tuple(reversed(rows))).selected == plan.candidates[0]
    with pytest.raises(ValueError, match="missing or duplicated"):
        select_winner(plan, rows[:-1])
    with pytest.raises(ValueError, match="provenance"):
        select_winner(plan, (rows[0].model_copy(update={"split": "validation"}), *rows[1:]))


def test_selection_uses_mean_not_one_lucky_seed() -> None:
    plan = RuleSearchPlan.official(ROOT)
    rows = list(_results(plan))
    rows[0] = rows[0].model_copy(update={"focal_score_cents": 10_000})
    rows[1] = rows[1].model_copy(update={"focal_score_cents": -10_000})
    assert select_winner(plan, tuple(rows)).selected == plan.candidates[1]


def test_short_search_runs_model_free_replays_and_resumes(tmp_path: Path) -> None:
    original = RuleSearchPlan.official(ROOT)
    plan = RuleSearchPlan.model_validate(
        {
            **original.model_dump(),
            "scenario": original.scenario.model_copy(update={"total_days": 2}),
            "candidates": original.candidates[:2],
            "development_seeds": (7,),
            "validation_seeds": (211,),
        }
    )
    search = FixedRuleSearch(ROOT, tmp_path, plan)
    first = search.run(workers=1)
    assert len(first.development) == 2 and len(first.validation) == 1
    assert first.validation[0].candidate_id == first.selection.selected.candidate_id
    assert all(
        row.terminal_day == row.replay_verified_days == 2
        for row in (*first.development, *first.validation)
    )
    assert all(
        len(row.companies) == 9 and row.model_api_calls == 0
        for row in (*first.development, *first.validation)
    )
    second = search.run(workers=1)
    assert second.selection == first.selection
    assert second.development == first.development
    assert second.validation == first.validation
