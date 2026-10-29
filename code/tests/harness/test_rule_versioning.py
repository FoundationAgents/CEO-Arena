"""Rule policy versions cannot cross recovery boundaries or affect archived replay."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ceo_arena.campaign.application import CampaignApplication
from ceo_arena.campaign.compiler import CampaignCompiler
from ceo_arena.campaign.repository import RepositoryConflictError
from ceo_arena.campaign.routes import ModelRouteRegistry
from ceo_arena.campaign.spec import ApiSpec, ExperimentSpec
from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.harness import (
    RULE_CEO_HARNESS_ID,
    RuleCEOConfig,
    RuleCeoHarness,
)
from ceo_arena.models import DecisionWindowRequest, DecisionWindowResult
from ceo_arena.protocol import RULE_MODEL_ID
from ceo_arena.provenance import simulation_fingerprints
from ceo_arena.runtime import AgentAssignment, BenchmarkCoordinator, CompanyContext, ExactReplay
from ceo_arena.runtime.persistence import RunCheckpoint, RunConfig

_LEGACY_RULE_ID = "rule-ceo-v1-legacy-test"
_RULE_SOURCE = "src/ceo_arena/harness/rule_ceo.py"
_COMPANY_ID = "company-1"


class _ObservedRule(RuleCeoHarness):
    """Record calls and optionally label a test fixture as an archived policy."""

    def __init__(self, identity: str = RULE_CEO_HARNESS_ID) -> None:
        super().__init__()
        self._identity = identity
        self.days: list[int] = []

    @property
    def harness_id(self) -> str:
        return self._identity

    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        self.days.append(request.observation.day)
        return await super().decide(request, context)


def _config(identity: str = RULE_CEO_HARNESS_ID, fingerprint: str = "current") -> RunConfig:
    return RunConfig(
        run_id="rule-version-test",
        scenario=default_scenario(seed=11, total_days=14),
        assignments=(
            AgentAssignment(company_id=_COMPANY_ID, model_id=RULE_MODEL_ID, harness_id=identity),
        ),
        artifact_fingerprints={_RULE_SOURCE: fingerprint},
    )


def _coordinator(config: RunConfig, path: Path, rule: RuleCeoHarness) -> BenchmarkCoordinator:
    return BenchmarkCoordinator(config, MarketWorld(config.scenario), {_COMPANY_ID: rule}, path)


def _spec() -> ExperimentSpec:
    return ExperimentSpec(
        campaign_id="rule-version-test",
        base_url="https://unused.example/v1",
        company_count=1,
        days=14,
        seeds=(11,),
        api=(ApiSpec(api_key="unused-test-key", models=("test-model",)),),
    )


def test_rule_version_and_parameters_are_part_of_harness_identity() -> None:
    default = RuleCeoHarness()
    changed = RuleCeoHarness(RuleCEOConfig(cash_floor_cents=123))

    assert default.harness_id == RULE_CEO_HARNESS_ID
    assert default.harness_id.startswith("rule-ceo-fixed-v3-")
    assert default.harness_id != changed.harness_id
    assert default.model_id == changed.model_id == RULE_MODEL_ID


def test_rule_source_is_included_in_simulation_fingerprint() -> None:
    root = Path(__file__).resolve().parents[2]
    expected = hashlib.sha256((root / _RULE_SOURCE).read_bytes()).hexdigest()

    assert simulation_fingerprints(root)[_RULE_SOURCE] == expected


def test_installed_profile_is_included_in_formal_fingerprints() -> None:
    root = Path(__file__).resolve().parents[2]
    profile = "src/ceo_arena/harness/rule_profile.json"
    expected = hashlib.sha256((root / profile).read_bytes()).hexdigest()
    assert simulation_fingerprints(root)[profile] == expected


def test_coordinator_rejects_legacy_rule_assignment_before_creating_artifacts(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="harness id mismatch"):
        _coordinator(_config(_LEGACY_RULE_ID), tmp_path / "run", RuleCeoHarness())

    assert not (tmp_path / "run").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("policy", "source"))
async def test_checkpoint_cannot_resume_with_changed_rule_identity_or_source(
    tmp_path: Path, change: str
) -> None:
    previous = _config(_LEGACY_RULE_ID) if change == "policy" else _config(fingerprint="old")
    archived = _coordinator(previous, tmp_path, _ObservedRule(previous.assignments[0].harness_id))
    archived._open_run(resume=False)
    checkpoint_bytes = archived.store.checkpoint_path.read_bytes()
    replacement = _ObservedRule()

    with pytest.raises(ValueError, match="manifest"):
        await _coordinator(_config(), tmp_path, replacement).run()

    assert replacement.days == []
    assert archived.store.checkpoint_path.read_bytes() == checkpoint_bytes


def test_campaign_rejects_current_source_plan_in_legacy_output(tmp_path: Path) -> None:
    spec = _spec()
    old_plan = CampaignCompiler().compile(spec, simulation_fingerprints={_RULE_SOURCE: "old"})
    CampaignApplication(spec, old_plan, tmp_path).prepare()
    frozen_bytes = (tmp_path / "campaign_plan.json").read_bytes()
    current_plan = CampaignCompiler().compile(
        spec, simulation_fingerprints={_RULE_SOURCE: "current"}
    )

    with pytest.raises(RepositoryConflictError, match="different frozen plan"):
        CampaignApplication(spec, current_plan, tmp_path).prepare()

    assert (tmp_path / "campaign_plan.json").read_bytes() == frozen_bytes


def test_campaign_rejects_new_rule_even_when_caller_reuses_old_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec)
    original_identity = ModelRouteRegistry.harness_id

    def archived_identity(model_id: str) -> str:
        return _LEGACY_RULE_ID if model_id == RULE_MODEL_ID else original_identity(model_id)

    with monkeypatch.context() as archived_code:
        archived_code.setattr(ModelRouteRegistry, "harness_id", staticmethod(archived_identity))
        archived = CampaignApplication(spec, plan, tmp_path)
        archived.prepare()
        jobs = archived.repository.list_jobs(spec.campaign_id)

    with pytest.raises(RepositoryConflictError, match="Run task identity"):
        CampaignApplication(spec, plan, tmp_path).prepare()

    assert archived.repository.list_jobs(spec.campaign_id) == jobs


@pytest.mark.asyncio
async def test_current_rule_resumes_collected_decisions_and_replays_without_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    reference = await _coordinator(config, tmp_path / "reference", RuleCeoHarness()).run()
    interrupted_rule = _ObservedRule()
    interrupted = _coordinator(config, tmp_path / "resumed", interrupted_rule)
    checkpoint = interrupted._open_run(resume=False)
    collected = await interrupted._collect_decision_barrier(checkpoint)
    assert collected.collecting_window is not None
    assert len(collected.collecting_window.decisions) == 1
    assert interrupted_rule.days == [0]

    resumed_rule = _ObservedRule()
    resumed = await _coordinator(config, tmp_path / "resumed", resumed_rule).run()

    assert resumed_rule.days == [7]
    assert resumed.final_state == reference.final_state
    assert resumed.windows == reference.windows
    checkpoint = RunCheckpoint.model_validate_json(Path(resumed.checkpoint_path).read_bytes())

    async def forbidden_call(
        self: RuleCeoHarness, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        raise AssertionError("Exact Replay must not call the current Rule")

    monkeypatch.setattr(RuleCeoHarness, "decide", forbidden_call)
    replay = ExactReplay(
        MarketWorld(config.scenario), runtime_trace_path=Path(resumed.trace_path)
    ).verify(checkpoint)
    assert replay.verified_days == 14
    assert replay.final_state_hash == MarketWorld(config.scenario).state_hash(reference.final_state)
