"""Isolated one-model speed screening against identical independent Rule CEOs."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from statistics import mean

import httpx
from pydantic import ConfigDict, Field, computed_field

from ceo_arena.campaign.spec import ExperimentSpec
from ceo_arena.economy import MarketWorld
from ceo_arena.harness import (
    OPENCODE_LLM_COMMIT,
    RULE_CEO_HARNESS_ID,
    NewApiProbe,
    OpenCodeHarness,
    OpenCodeHarnessConfig,
    RuleCeoHarness,
)
from ceo_arena.harness.worker_pool import WorkerPool
from ceo_arena.hashing import provider_endpoint_fingerprint
from ceo_arena.models import StrictModel
from ceo_arena.protocol import RULE_MODEL_ID
from ceo_arena.provenance import simulation_fingerprints
from ceo_arena.runtime import (
    AgentAssignment,
    AgentHarness,
    BenchmarkCoordinator,
    ExactReplay,
    RunCheckpoint,
)
from ceo_arena.runtime._atomic import AtomicModelFile
from ceo_arena.runtime.lock import RunDirectoryLock
from ceo_arena.runtime.performance import PerformanceEvent
from ceo_arena.runtime.persistence import RunConfig
from ceo_arena.runtime.replay import ReplayReport


class ReferenceManifest(StrictModel):
    """Read an existing configuration without reinterpreting its historical manifest hash."""

    model_config = ConfigDict(extra="ignore")
    config: RunConfig


class ScreeningCase(StrictModel):
    model_id: str
    api_index: int = Field(ge=0)
    config: RunConfig


class ScreeningPlan(StrictModel):
    reference_sha256: str
    target_company: str = "company-8"
    stop_after_day: int = 28
    cases: tuple[ScreeningCase, ...]


class WeeklyTiming(StrictModel):
    start_day: int
    end_day: int
    seconds: float | None = Field(default=None, ge=0)
    status: str
    provider_turns: int = 0
    protocol_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class CandidateTiming(StrictModel):
    model_id: str
    day: int = 0
    expected_days: int = 28
    expected_windows: int = 4
    weeks: tuple[WeeklyTiming, ...] = ()
    replay_verified: bool = False
    error: str | None = None

    @computed_field
    @property
    def eligible(self) -> bool:
        return (
            self.error is None
            and self.replay_verified
            and self.day == self.expected_days
            and len(self.weeks) == self.expected_windows
            and all(w.status == "sealed" and w.seconds is not None for w in self.weeks)
        )

    @computed_field
    @property
    def mean_seconds(self) -> float | None:
        if len(self.weeks) != self.expected_windows or any(w.seconds is None for w in self.weeks):
            return None
        return mean(w.seconds for w in self.weeks if w.seconds is not None)


class ScreeningReport(StrictModel):
    candidates: tuple[CandidateTiming, ...]

    @computed_field
    @property
    def winner(self) -> str | None:
        valid = [c for c in self.candidates if c.eligible and c.mean_seconds is not None]
        return min(valid, key=lambda c: (c.mean_seconds, c.model_id)).model_id if valid else None

    def markdown(self) -> str:
        """Render raw measured timing separately from eligibility and recommendation."""
        lines = [
            "# Luna replacement speed screening",
            "",
            "Seed 42; 9 companies; 1 candidate + 8 independent Rule CEOs; first 28 days of a "
            "500-day horizon. Times include worker startup, tools, provider retries and backoff.",
            "",
            "| Model | Week 1 | Week 2 | Week 3 | Week 4 | Mean | Eligible |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        for c in self.candidates:
            values = [format_duration(w.seconds) for w in c.weeks]
            values += ["—"] * (c.expected_windows - len(values))
            lines.append(
                f"| {c.model_id} | {' | '.join(values)} | {format_duration(c.mean_seconds)} "
                f"| {'yes' if c.eligible else 'no'} |"
            )
            if c.error:
                lines.append(f"\n{c.model_id}: {c.error}\n")
        lines += [
            "",
            f"Fastest eligible candidate: **{self.winner or 'none'}**.",
            "",
            "Eligibility requires four genuinely sealed decisions and verified Exact Replay. "
            "Budget-exhausted/failed windows do not qualify. This screens operational speed, "
            "not long-horizon capability equivalence or formal benchmark Score.",
        ]
        return "\n".join(lines) + "\n"


def format_duration(seconds: float | None) -> str:
    """Render a measured duration as minutes:seconds without converting missing data to zero."""
    if seconds is None:
        return "—"
    minutes, remainder = divmod(round(seconds), 60)
    return f"{minutes}:{remainder:02d}"


class SpeedScreening:
    """Freeze three independent cases, run them concurrently and audit their measured outcomes."""

    def __init__(self, plan: ScreeningPlan, credentials: ExperimentSpec, output: Path) -> None:
        self.plan, self.credentials, self.output = plan, credentials, output

    @staticmethod
    def plan_from(
        reference: Path, credentials: ExperimentSpec, models: tuple[str, ...], api_index: int
    ) -> ScreeningPlan:
        """Reuse the reference economy and budget; vary only the candidate and its opponents."""
        raw = reference.read_bytes()
        base = ReferenceManifest.model_validate_json(raw).config
        if len(base.assignments) != 9 or base.scenario.total_days != 500:
            raise ValueError("Screening requires the nine-company, 500-day reference")
        if base.scenario.seed != 42 or base.scenario.days_per_decision_window != 7:
            raise ValueError("Screening requires seed 42 and seven-day windows")
        if len(models) != 3 or len(set(models)) != 3 or RULE_MODEL_ID in models:
            raise ValueError("Select exactly three different provider-backed models")
        if not 0 <= api_index < len(credentials.api):
            raise ValueError("Selected API entry does not exist")
        endpoint = provider_endpoint_fingerprint(credentials.base_url)
        if endpoint != base.provider_endpoint_sha256:
            raise ValueError("Screening endpoint differs from the reference endpoint")
        fingerprints = simulation_fingerprints()
        root = Path(__file__).resolve().parents[2]
        for relative in ("src/ceo_arena/screening.py", "scripts/screen_candidate_models.py"):
            fingerprints[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        cases = []
        for model in models:
            identifier = hashlib.sha256(model.encode()).hexdigest()[:10]
            config = RunConfig(
                run_id=f"speed-screen-s42-{identifier}",
                scenario=base.scenario,
                budget=base.budget,
                allow_repeated_rule=True,
                provider_endpoint_sha256=endpoint,
                artifact_fingerprints=fingerprints,
                assignments=tuple(
                    AgentAssignment(
                        company_id=company,
                        model_id=model if company == "company-8" else RULE_MODEL_ID,
                        harness_id=(
                            f"opencode-native-{OPENCODE_LLM_COMMIT[:12]}"
                            if company == "company-8"
                            else RULE_CEO_HARNESS_ID
                        ),
                        api_index=api_index if company == "company-8" else None,
                    )
                    for company in base.company_ids
                ),
            )
            cases.append(ScreeningCase(model_id=model, api_index=api_index, config=config))
        return ScreeningPlan(reference_sha256=hashlib.sha256(raw).hexdigest(), cases=tuple(cases))

    async def run(self, *, paid: bool = False) -> ScreeningReport | None:
        """Catalog-only by default; the explicit paid switch runs all three isolated worlds."""
        with RunDirectoryLock(self.output / ".screening.lock"):
            plan_file = AtomicModelFile(self.output / "screening_plan.json", ScreeningPlan)
            previous = plan_file.load()
            if previous is not None and previous != self.plan:
                raise ValueError("Frozen screening plan changed; use a fresh output directory")
            plan_file.save(self.plan)
            for index in sorted({case.api_index for case in self.plan.cases}):
                key = self.credentials.api[index].api_key.get_secret_value()
                models = tuple(c.model_id for c in self.plan.cases if c.api_index == index)
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=30,
                    follow_redirects=False,
                ) as client:
                    probe = await NewApiProbe(client, self.credentials.base_url).probe(models)
                if probe.missing_models:
                    raise ValueError(f"Unavailable models: {probe.missing_models}")
            if not paid:
                return None
            results = await asyncio.gather(*(self._run_case(case) for case in self.plan.cases))
            report = ScreeningReport(candidates=tuple(results))
            AtomicModelFile(self.output / "report.json", ScreeningReport).save(report)
            (self.output / "report.md").write_text(report.markdown(), encoding="utf-8")
            return report

    async def _run_case(self, case: ScreeningCase) -> CandidateTiming:
        directory = self.output / case.model_id / "run"
        pool = WorkerPool()
        error = None
        verified = False
        try:
            harnesses: dict[str, AgentHarness] = {
                company: RuleCeoHarness() for company in case.config.company_ids
            }
            harnesses[self.plan.target_company] = OpenCodeHarness(
                OpenCodeHarnessConfig(
                    base_url=self.credentials.base_url,
                    api_key=self.credentials.api[case.api_index].api_key,
                    model=case.model_id,
                    trace_directory=directory / "agent-traces",
                ),
                worker_pool=pool,
            )
            async with BenchmarkCoordinator(
                case.config, MarketWorld(case.config.scenario), harnesses, directory
            ) as coordinator:
                await coordinator.run(stop_after_day=self.plan.stop_after_day)
            checkpoint = RunCheckpoint.model_validate_json(
                (directory / "checkpoint.json").read_bytes()
            )
            replay = await asyncio.to_thread(
                ExactReplay(
                    MarketWorld(case.config.scenario),
                    runtime_trace_path=directory / "trace.jsonl",
                    artifact_directory=directory / "agent-traces",
                ).verify_prefix,
                checkpoint,
            )
            AtomicModelFile(directory.parent / "replay.json", ReplayReport).save(replay)
            verified = replay.verified_days == self.plan.stop_after_day
        except Exception as failure:
            # Tracebacks can contain credentials; raw provider details stay in redacted traces.
            error = type(failure).__name__
        finally:
            await pool.aclose()
        result = self.measure(case, directory, replay_verified=verified, error=error)
        AtomicModelFile(directory.parent / "timing.json", CandidateTiming).save(result)
        print(result.model_dump_json(), flush=True)
        return result

    def measure(
        self,
        case: ScreeningCase,
        directory: Path,
        *,
        replay_verified: bool,
        error: str | None = None,
    ) -> CandidateTiming:
        """Join committed decisions to monotonic full-window durations, never to token estimates."""
        checkpoint_path = directory / "checkpoint.json"
        checkpoint = (
            RunCheckpoint.model_validate_json(checkpoint_path.read_bytes())
            if checkpoint_path.exists()
            else None
        )
        durations: dict[int, list[float]] = {}
        timing_path = directory / "performance.jsonl"
        if timing_path.exists():
            for line in timing_path.read_text(encoding="utf-8").splitlines():
                event = PerformanceEvent.model_validate_json(line)
                if (
                    event.stage == "decision_total"
                    and event.company_id == self.plan.target_company
                    and event.window_index is not None
                ):
                    durations.setdefault(event.window_index, []).append(event.duration_ms / 1000)
        weeks = []
        for window in checkpoint.completed_windows if checkpoint else ():
            decision = next(
                (d for d in window.decisions if d.company_id == self.plan.target_company), None
            )
            if decision is None:
                continue
            recorded = durations.get(window.window_index, [])
            weeks.append(
                WeeklyTiming(
                    start_day=window.start_day,
                    end_day=window.end_day,
                    seconds=recorded[0] if len(recorded) == 1 else None,
                    status=decision.agent_status,
                    provider_turns=decision.usage.provider_turns,
                    protocol_errors=decision.diagnostics.protocol_errors,
                    input_tokens=decision.usage.input_tokens,
                    output_tokens=decision.usage.output_tokens,
                )
            )
        return CandidateTiming(
            model_id=case.model_id,
            day=checkpoint.current_state.day if checkpoint else 0,
            expected_days=self.plan.stop_after_day,
            expected_windows=self.plan.stop_after_day
            // case.config.scenario.days_per_decision_window,
            weeks=tuple(weeks),
            replay_verified=replay_verified,
            error=error,
        )
