"""Persistent multi-key Campaign preparation, scheduling, recovery, and publication."""

from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from ceo_arena.campaign.checkpoint import CampaignCheckpointStore
from ceo_arena.campaign.models import CampaignRun, FrozenCampaignPlan
from ceo_arena.campaign.records import (
    CampaignRecord,
    CampaignStatus,
    FailurePhase,
    RunJobRecord,
    RunJobSpec,
    RunJobStatus,
    RunStopReason,
)
from ceo_arena.campaign.recovery_migration import assert_recovery_ready
from ceo_arena.campaign.repository import (
    CampaignRepository,
    CampaignRepositoryError,
    InvalidTransitionError,
    LeaseLostError,
    PauseRequestedError,
    RepositoryConflictError,
    RevisionConflictError,
)
from ceo_arena.campaign.routes import ModelRouteRegistry, RuntimeModelRoute
from ceo_arena.campaign.scheduler import CampaignScheduler, ResourceProbe
from ceo_arena.campaign.spec import ExperimentSpec
from ceo_arena.economy import MarketWorld
from ceo_arena.evaluation import (
    ArenaAnalysis,
    ArenaAnalyzer,
    ArenaLineup,
    ArenaPlan,
    ArenaRunResult,
    lineup_run_slug,
    score_world,
)
from ceo_arena.execution import decision_status_by_company, recovery_by_company
from ceo_arena.harness.worker_pool import WorkerPool
from ceo_arena.models import StrictModel
from ceo_arena.runtime import (
    AgentAssignment,
    AgentHarness,
    BenchmarkCoordinator,
    ExactReplay,
    ProviderDecisionError,
    ReplayMismatchError,
    ReplayReceipt,
    RunConfig,
    RunManifest,
)
from ceo_arena.runtime._atomic import AtomicModelFile
from ceo_arena.runtime._redact import redact_sensitive_text
from ceo_arena.runtime.admission import DecisionAdmission
from ceo_arena.runtime.coordinator import ProviderPermanentError
from ceo_arena.runtime.lock import RunDirectoryLock
from ceo_arena.runtime.persistence import CheckpointPhase, RunCheckpoint

CAMPAIGN_DATABASE_FILE = "campaign.sqlite3"
CAMPAIGN_PLAN_FILE = "campaign_plan.json"
CAMPAIGN_ANALYSIS_FILE = "arena_analysis.json"
FINAL_CHECKPOINT_FILE = "final_checkpoint.json"
_UNVERIFIED_ANALYSIS_FILE = "arena_analysis.unverified.json"
_MAX_RETRY_DELAY_SECONDS = 300.0

type RetryBackoff = Callable[[str, int], float]


class CampaignReport(StrictModel):
    """Compact durable status returned after preparation or execution."""

    campaign_id: str
    campaign_status: str
    expected_runs: int
    status_counts: dict[str, int]
    current_days: dict[str, int]
    analysis_verified: bool = False
    analysis_path: str | None = None

    @property
    def is_complete(self) -> bool:
        """Return whether every frozen Run passed Exact Replay and publication."""

        return (
            self.campaign_status == CampaignStatus.COMPLETED.value
            and self.status_counts.get(RunJobStatus.COMPLETED.value, 0) == self.expected_runs
            and self.analysis_verified
        )


class CampaignApplication:
    """Own the complete local Campaign lifecycle behind one small interface."""

    def __init__(
        self,
        spec: ExperimentSpec,
        plan: FrozenCampaignPlan,
        output: Path,
        *,
        repository: CampaignRepository | None = None,
        model_harness_builder: Callable[[RuntimeModelRoute, Path], AgentHarness] | None = None,
        lease_seconds: float = 120.0,
        heartbeat_seconds: float | None = None,
        retry_backoff: RetryBackoff | None = None,
        worker_poll_seconds: float = 1.0,
        resource_probe: ResourceProbe | None = None,
    ) -> None:
        if spec.campaign_id != plan.campaign_id:
            raise ValueError("Campaign spec and plan identities differ")
        if lease_seconds < 90:
            raise ValueError("Campaign lease must be at least 90 seconds")
        if heartbeat_seconds is not None and not 0 < heartbeat_seconds < lease_seconds:
            raise ValueError("Campaign heartbeat must be positive and shorter than its lease")
        if worker_poll_seconds <= 0:
            raise ValueError("Campaign worker polling must be positive")
        self.spec = spec
        self.plan = plan
        self.output = output.resolve()
        self.database_path = self.output / CAMPAIGN_DATABASE_FILE
        self.plan_path = self.output / CAMPAIGN_PLAN_FILE
        self.analysis_path = self.output / CAMPAIGN_ANALYSIS_FILE
        self.repository = repository or CampaignRepository(self.database_path)
        self._campaign_lock = RunDirectoryLock(self.output / ".campaign.lock")
        self._model_harness_builder = model_harness_builder
        self._resource_probe = resource_probe
        self._scheduler: CampaignScheduler | None = None
        self.routes = ModelRouteRegistry(spec, model_harness_builder)
        self.routes.validate_plan(plan)
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds or min(30.0, lease_seconds / 3.0)
        self._retry_backoff = retry_backoff or _deterministic_retry_backoff
        self._worker_poll_seconds = worker_poll_seconds

    def prepare(self) -> CampaignReport:
        """Persist the secret-free plan and idempotently enqueue every Run."""

        assert_recovery_ready(self.output)
        self.output.mkdir(parents=True, exist_ok=True)
        plan_file = AtomicModelFile(self.plan_path, FrozenCampaignPlan)
        existing_plan = plan_file.load()
        if existing_plan is not None and existing_plan != self.plan:
            raise RepositoryConflictError("Campaign output contains a different frozen plan")
        campaign = self.repository.create_campaign(
            campaign_id=self.plan.campaign_id,
            spec_hash=self.spec.spec_hash,
            plan_hash=self.plan.plan_hash,
            expected_run_count=len(self.plan.runs),
        )
        if existing_plan is None:
            plan_file.save(self.plan)
        expected_jobs = tuple(self._job_spec(run) for run in self.plan.runs)
        if campaign.status is CampaignStatus.COMPLETED:
            actual_jobs = {
                job.task_key: job for job in self.repository.list_jobs(campaign.campaign_id)
            }
            if len(actual_jobs) != len(expected_jobs) or any(
                expected.task_key not in actual_jobs
                or actual_jobs[expected.task_key].config_hash != expected.config_hash
                or actual_jobs[expected.task_key].run_payload_json != expected.run_payload_json
                for expected in expected_jobs
            ):
                raise RepositoryConflictError("completed Campaign Run set differs from its plan")
        else:
            self.repository.enqueue_runs(
                expected_jobs,
                attempt_limit=self.plan.execution.max_run_attempts,
            )
        return self.report()

    async def run(self) -> CampaignReport:
        """Resume eligible Runs under the frozen resource policy and publish a full Arena."""

        with self._campaign_lock:
            return await self._run_owned()

    async def resume(self) -> CampaignReport:
        """Explicitly requeue stopped Runs, then continue normal crash recovery."""

        with self._campaign_lock:
            self.prepare()
            for job in self.repository.list_jobs(self.plan.campaign_id):
                if job.status is not RunJobStatus.STOPPED:
                    if job.status is not RunJobStatus.FAILED:
                        continue
                    latest_failure = self.repository.list_failure_history(job.task_key)[-1:]
                    if not latest_failure or latest_failure[0].phase is not FailurePhase.PROVIDER:
                        continue
                self.repository.resume_recoverable(
                    job.task_key,
                    expected_revision=job.revision,
                )
            return await self._run_owned()

    async def _run_owned(self) -> CampaignReport:
        """Execute while one local scheduler owns all global Campaign limits."""

        self.prepare()
        jobs = self.repository.list_jobs(self.plan.campaign_id)
        if any(job.status.is_scheduler_runnable for job in jobs):
            await self.routes.validate_catalog()
            pool = WorkerPool(
                capacity=self.plan.execution.max_inflight_decisions,
                max_idle=self.plan.execution.max_idle_workers,
            )
            self.routes = ModelRouteRegistry(
                self.spec, self._model_harness_builder, worker_pool=pool
            )
            self._scheduler = CampaignScheduler(
                self.plan,
                self.repository,
                self.output,
                self._run_claimed,
                pool,
                resource_probe=self._resource_probe,
                lease_seconds=self._lease_seconds,
                poll_seconds=self._worker_poll_seconds,
            )
            try:
                await self._scheduler.run()
            finally:
                await pool.aclose()
                self._scheduler = None
        jobs = self.repository.list_jobs(self.plan.campaign_id)
        if all(job.status is RunJobStatus.COMPLETED for job in jobs):
            try:
                await asyncio.to_thread(self._verify_published_runs)
                analysis = self._build_analysis()
            except Exception:
                self.repository.mark_campaign_incomplete(self.plan.campaign_id)
                self._quarantine_analysis(force=True)
                raise
            self.repository.complete_campaign(
                self.plan.campaign_id,
                analysis=analysis,
            )
            self._publish_analysis(analysis)
            self._export_final_checkpoints()
        elif all(job.status.is_terminal or job.status is RunJobStatus.STOPPED for job in jobs):
            self.repository.mark_campaign_incomplete(self.plan.campaign_id)
            self._quarantine_analysis()
        else:
            self._quarantine_analysis()
        return self.report()

    def report(self) -> CampaignReport:
        """Read a stable Campaign status without touching credentials or model providers."""

        campaign = self.repository.get_campaign(self.plan.campaign_id)
        if campaign is None:
            raise KeyError(f"Campaign is not prepared: {self.plan.campaign_id}")
        return _report_from_repository(self.output, campaign, self.repository)

    async def _run_claimed(
        self,
        job: RunJobRecord,
        owner: str,
        admission: DecisionAdmission,
    ) -> None:
        checkpoint_store = CampaignCheckpointStore(
            self.repository,
            task_key=job.task_key,
            lease_owner=owner,
            revision=job.revision,
        )
        if job.attempt_limit is not None and job.attempt > job.attempt_limit:
            self.repository.fail_run(
                job.task_key,
                lease_owner=owner,
                expected_revision=checkpoint_store.revision,
                error="maximum Run attempts exceeded before execution",
                phase=FailurePhase.RUNTIME,
            )
            return
        heartbeat = asyncio.create_task(
            self._heartbeat(checkpoint_store),
            name=f"campaign:{job.task_key}:lease",
        )
        execution = asyncio.create_task(
            self._execute_claimed(job, checkpoint_store, admission),
            name=f"campaign:{job.task_key}:runtime",
        )
        try:
            done, _pending = await asyncio.wait(
                {heartbeat, execution},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                error = heartbeat.exception()
                if error is not None:
                    raise error
                await self._cancel_task(execution)
                self._stop_for_pause(checkpoint_store)
                return
            checkpoint, result, receipt = execution.result()
            await self._cancel_task(heartbeat)
            if checkpoint_store.renew_lease(self._lease_seconds):
                self._stop_for_pause(checkpoint_store)
                return
            self.repository.complete_run(
                job.task_key,
                lease_owner=owner,
                expected_revision=checkpoint_store.revision,
                checkpoint=checkpoint,
                result=result,
                receipt=receipt,
            )
        except asyncio.CancelledError:
            await self._cancel_task(execution)
            await self._cancel_task(heartbeat)
            self._release_failed_attempt(job, checkpoint_store, "Campaign execution cancelled")
            raise
        except (LeaseLostError, RevisionConflictError):
            await self._cancel_task(execution)
            await self._cancel_task(heartbeat)
        except PauseRequestedError as error:
            await self._cancel_task(execution)
            await self._cancel_task(heartbeat)
            checkpoint_store.revision = error.revision
            self._stop_for_pause(checkpoint_store)
        except Exception as error:
            await self._cancel_task(execution)
            await self._cancel_task(heartbeat)
            self._release_failed_attempt(job, checkpoint_store, _safe_error(error), error)

    async def _execute_claimed(
        self,
        job: RunJobRecord,
        checkpoint_store: CampaignCheckpointStore,
        admission: DecisionAdmission,
    ) -> tuple[RunCheckpoint, ArenaRunResult, ReplayReceipt]:
        run = job.run_payload
        config = self._run_config(run)
        if config.config_hash != job.config_hash:
            raise RepositoryConflictError("frozen Run configuration hash drifted")
        run_directory = self._run_directory(run)
        harnesses = self.routes.create_harnesses(run, run_directory / "agent-traces")
        coordinator = BenchmarkCoordinator(
            config=config,
            world=MarketWorld(config.scenario),
            harnesses=harnesses,
            run_directory=run_directory,
            checkpoint_store=checkpoint_store,
            decision_admission=admission,
            decision_observer=(
                self._scheduler.observe_decision if self._scheduler is not None else None
            ),
        )
        async with coordinator:
            await coordinator.run(resume=True)
        checkpoint = checkpoint_store.load(run_id=config.run_id, config_hash=config.config_hash)
        if checkpoint is None or checkpoint.phase is not CheckpointPhase.COMPLETE:
            raise RuntimeError("runtime returned without a complete Campaign checkpoint")
        trace_path = run_directory / "trace.jsonl"
        report = await asyncio.to_thread(
            ExactReplay(
                MarketWorld(config.scenario),
                artifact_directory=run_directory / "agent-traces",
                runtime_trace_path=trace_path,
            ).verify,
            checkpoint,
        )
        receipt = ReplayReceipt.create_from_checkpoint(
            report,
            checkpoint=checkpoint,
            config_hash=config.config_hash,
            runtime_trace_path=trace_path,
            verifier_fingerprints=config.artifact_fingerprints,
        )
        return checkpoint, self._result(run, config, checkpoint), receipt

    @staticmethod
    def _ensure_final_checkpoint(run_directory: Path, checkpoint: RunCheckpoint) -> None:
        """Create one immutable audit snapshot without making it recovery authority."""

        snapshot = AtomicModelFile(run_directory / FINAL_CHECKPOINT_FILE, RunCheckpoint)
        existing = snapshot.load()
        if existing is None:
            snapshot.save(checkpoint)
        elif existing != checkpoint:
            raise RepositoryConflictError("immutable final checkpoint differs from replayed state")

    async def _heartbeat(self, checkpoint_store: CampaignCheckpointStore) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            if checkpoint_store.renew_lease(self._lease_seconds):
                return

    def _stop_for_pause(self, checkpoint_store: CampaignCheckpointStore) -> None:
        """Consume an observed pause without recording a false failure."""

        try:
            self.repository.stop_run(
                checkpoint_store.task_key,
                lease_owner=checkpoint_store.lease_owner,
                expected_revision=checkpoint_store.revision,
                reason=RunStopReason.USER_REQUESTED,
                error="user requested pause",
            )
        except (LeaseLostError, RevisionConflictError):
            return

    def _release_failed_attempt(
        self,
        job: RunJobRecord,
        checkpoint_store: CampaignCheckpointStore,
        message: str,
        error: Exception | None = None,
    ) -> None:
        phase = _failure_phase(error)
        try:
            if _is_permanent(error):
                self.repository.fail_run(
                    job.task_key,
                    lease_owner=checkpoint_store.lease_owner,
                    expected_revision=checkpoint_store.revision,
                    error=message,
                    phase=phase,
                )
            elif (
                phase is FailurePhase.PROVIDER
                and job.attempt_limit is not None
                and job.attempt >= job.attempt_limit
            ):
                self.repository.stop_run(
                    job.task_key,
                    lease_owner=checkpoint_store.lease_owner,
                    expected_revision=checkpoint_store.revision,
                    reason=RunStopReason.PROVIDER_RETRY_EXHAUSTED,
                    error=message,
                    phase=phase,
                )
            elif job.attempt_limit is not None and job.attempt >= job.attempt_limit:
                self.repository.fail_run(
                    job.task_key,
                    lease_owner=checkpoint_store.lease_owner,
                    expected_revision=checkpoint_store.revision,
                    error=message,
                    phase=phase,
                )
            else:
                self.repository.interrupt_run(
                    job.task_key,
                    lease_owner=checkpoint_store.lease_owner,
                    expected_revision=checkpoint_store.revision,
                    error=message,
                    phase=phase,
                    retry_delay_seconds=self._retry_delay(job),
                )
        except (LeaseLostError, RevisionConflictError):
            return

    def _retry_delay(self, job: RunJobRecord) -> float:
        delay = float(self._retry_backoff(job.task_key, job.attempt))
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("retry backoff must return a finite non-negative delay")
        return min(delay, _MAX_RETRY_DELAY_SECONDS)

    def _job_spec(self, run: CampaignRun) -> RunJobSpec:
        config = self._run_config(run)
        return RunJobSpec.create(
            campaign_id=run.campaign_id,
            run_id=run.run_id,
            lineup_id=run.lineup_id,
            seed=run.seed,
            config_hash=config.config_hash,
            run_payload_json=run.model_dump_json(),
        )

    def _run_config(self, run: CampaignRun) -> RunConfig:
        return RunConfig(
            run_id=run.run_id,
            scenario=self.plan.scenario_template.model_copy(update={"seed": run.seed}),
            assignments=self._assignments(
                run.company_ids,
                run.model_ids,
                run.api_indices,
            ),
            budget=self.plan.budget,
            provider_endpoint_sha256=self.plan.provider_endpoint_sha256,
            artifact_fingerprints=self.plan.simulation_fingerprints,
        )

    def _run_directory(self, run: CampaignRun) -> Path:
        return self.output / "runs" / f"seed-{run.seed}" / lineup_run_slug(run.lineup_id)

    @staticmethod
    def _result(
        run: CampaignRun,
        config: RunConfig,
        checkpoint: RunCheckpoint,
    ) -> ArenaRunResult:
        return ArenaRunResult(
            run_id=run.run_id,
            lineup_id=run.lineup_id,
            seed=run.seed,
            assignments=config.assignments,
            official_score_by_company_cents={
                company_id: score.official_score_cents
                for company_id, score in score_world(
                    checkpoint.current_state,
                    config.scenario,
                ).items()
            },
            decision_status_by_company=decision_status_by_company(checkpoint.completed_windows),
            recovery_by_company=recovery_by_company(checkpoint.completed_windows),
            scenario_id=config.scenario.scenario_id,
            config_hash=config.config_hash,
            artifact_fingerprints=config.artifact_fingerprints,
        )

    def _build_analysis(self) -> ArenaAnalysis:
        """Derive Tracks, retaining explicit non-completions but rejecting provider failures."""

        results = self.repository.list_results(self.plan.campaign_id)
        arena_plan = self._arena_plan()
        analyzer = ArenaAnalyzer(results, arena_plan)
        analysis = analyzer.analyze()
        model_ids = tuple(route.model_id for route in self.plan.routes)
        invalid = tuple(
            model_id
            for model_id in model_ids
            if (
                analysis.health[model_id].status_counts.get("sealed", 0)
                + analysis.health[model_id].status_counts.get("budget_exhausted", 0)
            )
            == 0
            or analysis.health[model_id].status_counts.get("provider_error", 0) > 0
        )
        if invalid:
            raise InvalidTransitionError(
                "official analysis rejects unavailable models or provider errors: "
                + ", ".join(invalid)
            )
        return analysis

    def _publish_analysis(self, analysis: ArenaAnalysis) -> None:
        """Project the database-authoritative analysis to a convenient JSON file."""

        analysis_file = AtomicModelFile(self.analysis_path, ArenaAnalysis)
        try:
            existing = analysis_file.load()
            if existing != analysis:
                analysis_file.save(analysis)
            (self.output / _UNVERIFIED_ANALYSIS_FILE).unlink(missing_ok=True)
        except (OSError, ValueError):
            return

    def _verify_published_runs(self) -> None:
        """Fail closed if any completed Run artifact changed after publication."""

        results = {
            result.run_id: result for result in self.repository.list_results(self.plan.campaign_id)
        }
        receipts = {
            receipt.run_id: receipt
            for receipt in self.repository.list_replay_receipts(self.plan.campaign_id)
        }
        expected_ids = {run.run_id for run in self.plan.runs}
        if set(results) != expected_ids or set(receipts) != expected_ids:
            raise RepositoryConflictError("published Run artifact set is incomplete")
        for run in self.plan.runs:
            config = self._run_config(run)
            run_directory = self._run_directory(run)
            manifest = RunManifest.model_validate_json(
                (run_directory / "manifest.json").read_text(encoding="utf-8")
            )
            world = MarketWorld(config.scenario)
            initial_state = world.create_initial_state(config.company_ids)
            if manifest.config != config or manifest.initial_state_hash != world.state_hash(
                initial_state
            ):
                raise RepositoryConflictError("published Run manifest differs from its plan")
            recovery = self.repository.load_recovery(run.task_key)
            if recovery is None or recovery.checkpoint is None:
                raise RepositoryConflictError("published Run lost its terminal checkpoint")
            checkpoint = recovery.checkpoint
            if (
                checkpoint.phase is not CheckpointPhase.COMPLETE
                or checkpoint.run_id != config.run_id
                or checkpoint.config_hash != config.config_hash
            ):
                raise RepositoryConflictError("published final checkpoint identity is invalid")
            trace_path = run_directory / "trace.jsonl"
            receipt = receipts[run.run_id]
            receipt.validate_checkpoint_inputs(
                checkpoint=checkpoint,
                config_hash=config.config_hash,
                runtime_trace_path=trace_path,
                verifier_fingerprints=config.artifact_fingerprints,
            )
            ExactReplay(
                world,
                artifact_directory=run_directory / "agent-traces",
                runtime_trace_path=trace_path,
            ).verify_artifacts(checkpoint)
            expected_result = self._result(run, config, checkpoint)
            if results[run.run_id] != expected_result:
                raise RepositoryConflictError("published Run result differs from its audit state")

    def _export_final_checkpoints(self) -> None:
        """Best-effort rebuild convenient JSON projections from authoritative SQLite."""

        for run in self.plan.runs:
            recovery = self.repository.load_recovery(run.task_key)
            if recovery is None or recovery.checkpoint is None:
                continue
            with suppress(OSError, ValueError):
                self._ensure_final_checkpoint(
                    self._run_directory(run),
                    recovery.checkpoint,
                )

    def _arena_plan(self) -> ArenaPlan:
        route_indices = {route.model_id: route.api_index for route in self.plan.routes}
        company_ids = self.plan.runs[0].company_ids
        lineups = tuple(
            ArenaLineup(
                lineup_id=lineup.lineup_id,
                assignments=self._assignments(
                    company_ids,
                    lineup.model_ids,
                    tuple(route_indices.get(model_id) for model_id in lineup.model_ids),
                ),
            )
            for lineup in self.plan.lineups
        )
        return ArenaPlan(
            scenario_id=self.plan.scenario_template.scenario_id,
            scenario_template=self.plan.scenario_template,
            scenario_hash=self.plan.scenario_hash,
            seeds=self.plan.seeds,
            lineups=lineups,
            budget=self.plan.budget,
            provider_endpoint_sha256=self.plan.provider_endpoint_sha256,
            artifact_fingerprints=self.plan.simulation_fingerprints,
        )

    def _assignments(
        self,
        company_ids: tuple[str, ...],
        model_ids: tuple[str, ...],
        api_indices: tuple[int | None, ...],
    ) -> tuple[AgentAssignment, ...]:
        """Build the sole canonical company-to-model assignment representation."""

        return tuple(
            AgentAssignment(
                company_id=company_id,
                model_id=model_id,
                harness_id=self.routes.harness_id(model_id),
                api_index=api_index,
                inference=(
                    self.routes.resolve(model_id, api_index).inference
                    if api_index is not None
                    else None
                ),
            )
            for company_id, model_id, api_index in zip(
                company_ids,
                model_ids,
                api_indices,
                strict=True,
            )
        )

    def _quarantine_analysis(self, *, force: bool = False) -> None:
        campaign = self.repository.get_campaign(self.plan.campaign_id)
        if not force and campaign is not None and campaign.status is CampaignStatus.COMPLETED:
            return
        with suppress(OSError):
            if self.analysis_path.exists():
                self.analysis_path.replace(self.output / _UNVERIFIED_ANALYSIS_FILE)

    @staticmethod
    async def _cancel_task(task: asyncio.Task[object]) -> None:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


def load_campaign_plan(output: Path) -> FrozenCampaignPlan:
    """Load the secret-free frozen plan needed for status inspection."""

    path = output.resolve() / CAMPAIGN_PLAN_FILE
    return FrozenCampaignPlan.model_validate_json(path.read_text(encoding="utf-8"))


def campaign_report(output: Path) -> CampaignReport:
    """Read Campaign status without loading the credential-bearing TOML spec."""

    resolved = output.resolve()
    plan = load_campaign_plan(resolved)
    repository = CampaignRepository(resolved / CAMPAIGN_DATABASE_FILE)
    campaign = repository.get_campaign(plan.campaign_id)
    if campaign is None:
        raise KeyError(f"Campaign is not prepared: {plan.campaign_id}")
    return _report_from_repository(resolved, campaign, repository)


def _report_from_repository(
    output: Path,
    campaign: CampaignRecord,
    repository: CampaignRepository,
) -> CampaignReport:
    jobs = repository.list_jobs(campaign.campaign_id)
    counts = Counter(job.status.value for job in jobs)
    analysis_path = output / CAMPAIGN_ANALYSIS_FILE
    analysis = repository.get_analysis(campaign.campaign_id)
    projection_matches = False
    if analysis is not None and analysis_path.is_file():
        with suppress(OSError, ValueError):
            projection_matches = (
                ArenaAnalysis.model_validate_json(analysis_path.read_text(encoding="utf-8"))
                == analysis
            )
    return CampaignReport(
        campaign_id=campaign.campaign_id,
        campaign_status=campaign.status.value,
        expected_runs=campaign.expected_run_count,
        status_counts=dict(sorted(counts.items())),
        current_days={job.task_key: job.current_day for job in jobs},
        analysis_verified=analysis is not None,
        analysis_path=str(analysis_path) if projection_matches else None,
    )


def _deterministic_retry_backoff(task_key: str, attempt: int) -> float:
    """Return equal-jitter exponential retry delay capped at five minutes."""

    exponent = min(max(attempt - 1, 0), 9)
    ceiling = min(_MAX_RETRY_DELAY_SECONDS, 10.0 * 2.0**exponent)
    digest = hashlib.sha256(f"ceos-retry-v1:{task_key}:{attempt}".encode()).digest()
    unit = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
    return min(_MAX_RETRY_DELAY_SECONDS, ceiling * (0.5 + unit / 2.0))


def _failure_phase(error: Exception | None) -> FailurePhase:
    if isinstance(error, ProviderDecisionError):
        return FailurePhase.PROVIDER
    if isinstance(error, ReplayMismatchError):
        return FailurePhase.REPLAY
    if isinstance(error, sqlite3.Error):
        return FailurePhase.PERSISTENCE
    if isinstance(error, (RepositoryConflictError, InvalidTransitionError, ValueError)):
        return FailurePhase.CONFIGURATION
    if isinstance(error, CampaignRepositoryError):
        return FailurePhase.PERSISTENCE
    return FailurePhase.RUNTIME


def _is_permanent(error: Exception | None) -> bool:
    if isinstance(error, ProviderPermanentError):
        return True
    return isinstance(
        error,
        (
            FileNotFoundError,
            InvalidTransitionError,
            ReplayMismatchError,
            RepositoryConflictError,
            ValueError,
        ),
    )


def _safe_error(error: Exception) -> str:
    detail = redact_sensitive_text(f"{type(error).__name__}: {error}")
    return " ".join((detail or type(error).__name__).split())[:2_000]


__all__ = [
    "CAMPAIGN_ANALYSIS_FILE",
    "CAMPAIGN_DATABASE_FILE",
    "CAMPAIGN_PLAN_FILE",
    "FINAL_CHECKPOINT_FILE",
    "CampaignApplication",
    "CampaignReport",
    "campaign_report",
    "load_campaign_plan",
]
