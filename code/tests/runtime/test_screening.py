"""Offline checks for repeated Rule admission, prefix stopping and timing qualification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from ceo_arena import screening
from ceo_arena.campaign.spec import ApiSpec, ExperimentSpec
from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.harness import OPENCODE_LLM_COMMIT, RULE_CEO_HARNESS_ID, RuleCeoHarness
from ceo_arena.harness.newapi import NewApiProbeResult
from ceo_arena.hashing import provider_endpoint_fingerprint
from ceo_arena.models import DecisionWindowRequest, DecisionWindowResult, StrictModel
from ceo_arena.protocol import RULE_MODEL_ID
from ceo_arena.runtime import BenchmarkCoordinator, ExactReplay
from ceo_arena.runtime.context import CompanyContext
from ceo_arena.runtime.persistence import AgentAssignment, CheckpointPhase, RunCheckpoint, RunConfig
from ceo_arena.screening import CandidateTiming, ScreeningReport, SpeedScreening, WeeklyTiming


def rule_config(*, repeated: bool = True, total_days: int = 500) -> RunConfig:
    return RunConfig(
        run_id="screening-test",
        scenario=default_scenario(total_days=total_days),
        allow_repeated_rule=repeated,
        assignments=tuple(
            AgentAssignment(
                company_id=f"company-{i}",
                model_id=RULE_MODEL_ID,
                harness_id=RULE_CEO_HARNESS_ID,
            )
            for i in range(1, 10)
        ),
    )


class ObservedRule(RuleCeoHarness):
    def __init__(self) -> None:
        super().__init__()
        self.observed_days: list[int] = []

    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        assert request.observation.total_days == 500
        self.observed_days.append(request.observation.day)
        return await super().decide(request, context)


def test_repeated_rule_is_explicit_and_does_not_allow_duplicate_models() -> None:
    with pytest.raises(ValidationError, match="model ids must be unique"):
        rule_config(repeated=False)
    raw = rule_config().model_dump()
    for assignment in raw["assignments"][:2]:
        assignment.update(model_id="same-llm", harness_id="opencode-native-test", api_index=0)
    raw["provider_endpoint_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="model ids must be unique"):
        RunConfig.model_validate(raw)


def test_default_config_serialization_preserves_existing_run_identity() -> None:
    enabled = rule_config()
    raw = enabled.model_dump()
    raw["assignments"] = raw["assignments"][:1]
    raw.pop("allow_repeated_rule")
    default = RunConfig.model_validate(raw)
    assert default.model_dump() == raw
    assert enabled.model_dump()["allow_repeated_rule"] is True


@pytest.mark.asyncio
async def test_stop_commits_four_weeks_without_a_fifth_call_and_can_resume(tmp_path: Path) -> None:
    config = rule_config()
    harnesses = {company: ObservedRule() for company in config.company_ids}
    async with BenchmarkCoordinator(
        config, MarketWorld(config.scenario), harnesses, tmp_path
    ) as run:
        result = await run.run(stop_after_day=28, resume=False)
    assert result.final_state.day == 28
    assert all(h.observed_days == [0, 7, 14, 21] for h in harnesses.values())
    checkpoint = RunCheckpoint.model_validate_json((tmp_path / "checkpoint.json").read_bytes())
    assert checkpoint.phase is not CheckpointPhase.COMPLETE
    assert checkpoint.collecting_window is None and checkpoint.pending_window is None
    assert ExactReplay(MarketWorld(config.scenario)).verify_prefix(checkpoint).verified_days == 28
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert len(events) == 9 and not any(e["kind"] == "run_completed" for e in events)
    timings = [
        json.loads(line) for line in (tmp_path / "performance.jsonl").read_text().splitlines()
    ]
    assert len([e for e in timings if e["stage"] == "decision_total"]) == 36
    async with BenchmarkCoordinator(
        config, MarketWorld(config.scenario), harnesses, tmp_path
    ) as run:
        result = await run.run(stop_after_day=35)
    assert result.final_state.day == 35
    assert all(h.observed_days == [0, 7, 14, 21, 28] for h in harnesses.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [0, -7, 1, 29, 501])
async def test_stop_rejects_invalid_boundaries(tmp_path: Path, day: int) -> None:
    config = rule_config()
    harnesses = {company: RuleCeoHarness() for company in config.company_ids}
    async with BenchmarkCoordinator(
        config, MarketWorld(config.scenario), harnesses, tmp_path
    ) as run:
        with pytest.raises(ValueError, match="window boundary"):
            await run.run(stop_after_day=day)


@pytest.mark.asyncio
async def test_terminal_stop_still_marks_complete(tmp_path: Path) -> None:
    config = rule_config(total_days=10)
    harnesses = {company: RuleCeoHarness() for company in config.company_ids}
    async with BenchmarkCoordinator(
        config, MarketWorld(config.scenario), harnesses, tmp_path
    ) as run:
        result = await run.run(stop_after_day=10)
    assert result.final_state.day == 10
    checkpoint = RunCheckpoint.model_validate_json((tmp_path / "checkpoint.json").read_bytes())
    assert checkpoint.phase is CheckpointPhase.COMPLETE


def test_speed_ranking_excludes_fast_fallback_and_missing_measurements() -> None:
    weeks = tuple(
        WeeklyTiming(start_day=i * 7, end_day=(i + 1) * 7, seconds=60, status="sealed")
        for i in range(4)
    )
    valid = CandidateTiming(model_id="valid", day=28, weeks=weeks, replay_verified=True)
    fallback = CandidateTiming(
        model_id="fast-fallback",
        day=28,
        replay_verified=True,
        weeks=tuple(
            w.model_copy(update={"seconds": 1, "status": "budget_exhausted"}) for w in weeks
        ),
    )
    missing = CandidateTiming(
        model_id="missing",
        day=28,
        replay_verified=True,
        weeks=tuple(w.model_copy(update={"seconds": None}) for w in weeks),
    )
    report = ScreeningReport(candidates=(fallback, missing, valid))
    assert report.winner == "valid" and valid.mean_seconds == 60
    assert missing.mean_seconds is None
    assert ScreeningReport(candidates=(fallback, missing)).winner is None
    assert "0:01" in report.markdown() and "1:00" in report.markdown()


class OfflineConfig(StrictModel):
    base_url: str
    api_key: SecretStr
    model: str
    trace_directory: Path


class OfflineCandidate(ObservedRule):
    def __init__(self, config: OfflineConfig, *, worker_pool: object) -> None:
        super().__init__()
        self._model = config.model
        self._trace_directory = config.trace_directory

    @property
    def harness_id(self) -> str:
        return f"opencode-native-{OPENCODE_LLM_COMMIT[:12]}"

    @property
    def model_id(self) -> str:
        return self._model

    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        result = await super().decide(request, context)
        relative = f"{request.run_id}/{request.company_id}-{request.world_version}.ndjson"
        trace = self._trace_directory / relative
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text('{"kind":"offline-test-decision"}\n', encoding="utf-8")
        return result.model_copy(
            update={
                "trace_path": relative,
                "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            }
        )


@pytest.mark.asyncio
async def test_three_case_screening_and_idempotent_resume_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://offline.example/v1"
    credentials = ExperimentSpec(
        campaign_id="offline",
        base_url=url,
        company_count=1,
        days=500,
        seeds=(42,),
        api=(ApiSpec(api_key=SecretStr("offline-only"), models=("placeholder",)),),
    )
    base = rule_config().model_copy(
        update={"provider_endpoint_sha256": provider_endpoint_fingerprint(url)}
    )
    reference = tmp_path / "reference.json"
    reference.write_text(json.dumps({"config": base.model_dump(mode="json")}), encoding="utf-8")
    plan = SpeedScreening.plan_from(reference, credentials, ("fast-a", "fast-b", "fast-c"), 0)
    monkeypatch.setattr(screening, "OpenCodeHarnessConfig", OfflineConfig)
    monkeypatch.setattr(screening, "OpenCodeHarness", OfflineCandidate)

    async def probe(self: object, models: tuple[str, ...]) -> NewApiProbeResult:
        return NewApiProbeResult(
            base_url=url,
            available_model_count=3,
            required_models=models,
            missing_models=(),
        )

    monkeypatch.setattr(screening.NewApiProbe, "probe", probe)
    experiment = SpeedScreening(plan, credentials, tmp_path / "results")
    report = await experiment.run(paid=True)
    assert report is not None and report.winner is not None
    assert all(c.eligible and c.day == 28 and len(c.weeks) == 4 for c in report.candidates)
    repeated = await experiment.run(paid=True)
    assert repeated == report
    assert "offline-only" not in (tmp_path / "results/screening_plan.json").read_text()
