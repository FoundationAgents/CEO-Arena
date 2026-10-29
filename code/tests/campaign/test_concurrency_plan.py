"""Execution capacity changes cannot silently rewrite a frozen experiment."""

from pathlib import Path

import pytest

from ceo_arena.campaign import CampaignApplication, CampaignCompiler, load_experiment_spec
from ceo_arena.campaign.models import CampaignExecutionSpec
from ceo_arena.campaign.repository import RepositoryConflictError


def test_thirty_run_capacity_preserves_design_but_requires_a_new_frozen_plan(
    tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[2] / "examples/campaign.example.toml"
    current = load_experiment_spec(source)
    previous = current.model_copy(
        update={"execution": CampaignExecutionSpec(max_concurrent_runs=20)}
    )
    compiler = CampaignCompiler()
    before, after = compiler.compile(previous), compiler.compile(current)

    assert after.execution.max_concurrent_runs == 30
    assert len(after.runs) == (len(current.model_ids) + 1) * len(current.seeds)
    for field in (
        "scenario_template",
        "model_order",
        "seeds",
        "days",
        "lineups",
        "runs",
        "routes",
        "budget",
    ):
        assert getattr(before, field) == getattr(after, field)
    assert before.plan_hash != after.plan_hash

    CampaignApplication(previous, before, tmp_path).prepare()
    frozen = (tmp_path / "campaign_plan.json").read_bytes()
    with pytest.raises(RepositoryConflictError, match="different frozen plan"):
        CampaignApplication(current, after, tmp_path).prepare()
    assert (tmp_path / "campaign_plan.json").read_bytes() == frozen
