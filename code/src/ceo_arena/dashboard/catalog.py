"""Deep read-only module that turns durable run artifacts into dashboard views."""

from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean

from ceo_arena.campaign.records import (
    FailurePhase,
    RunJobStatus,
    RunStopReason,
)
from ceo_arena.dashboard.campaign import (
    CampaignReadStore,
    CampaignRunView,
    CampaignStagedDecision,
    CampaignView,
)
from ceo_arena.dashboard.discovery import metadata_files
from ceo_arena.dashboard.models import (
    DashboardAction,
    DashboardCampaignResources,
    DashboardCompany,
    DashboardDailyCashflow,
    DashboardDecision,
    DashboardDecisionStage,
    DashboardEvent,
    DashboardRun,
    DashboardRunList,
    DashboardRunStatus,
    DashboardRunSummary,
    SeriesPoint,
)
from ceo_arena.dashboard.timing import SubmissionTimes
from ceo_arena.economy.market import MarketDynamics
from ceo_arena.evaluation import score_world
from ceo_arena.evaluation.arena import ArenaRunResult, lineup_run_slug
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import CompanyDecision, CompanyState
from ceo_arena.runtime.lock import RunDirectoryLock
from ceo_arena.runtime.performance import PerformanceStatus
from ceo_arena.runtime.persistence import (
    AtomicCheckpointStore,
    CheckpointPhase,
    RunCheckpoint,
    RunManifest,
    SealedDecision,
)
from ceo_arena.runtime.trace import TraceKind, TraceRecord

_MAX_DISCOVERED_RUNS = 100
_MAX_SERIES_POINTS = 160
_MAX_RECENT_EVENTS = 16
_MAX_LEDGER_DAYS = 30
_MAX_ACTION_RECEIPTS = 60


@dataclass(frozen=True)
class _CachedRun:
    signature: tuple[tuple[int, int], ...]
    view: DashboardRun


@dataclass(frozen=True)
class _DecisionReceipt:
    window_index: int
    day: int
    end_day: int | None
    stage: DashboardDecisionStage
    sealed: SealedDecision


@dataclass(frozen=True)
class _LoadedCampaign:
    store: CampaignReadStore
    view: CampaignView


@dataclass(frozen=True, slots=True)
class DashboardRunControlTarget:
    """Private Campaign identity resolved from one opaque browser Run reference."""

    database: Path
    campaign_id: str
    task_key: str
    revision: int
    action: str


class RunDashboard:
    """Project immutable run files through two small, typed read interfaces."""

    def __init__(self, runs_root: Path, *, controls_enabled: bool = True) -> None:
        self.runs_root = runs_root.resolve()
        self.controls_enabled = controls_enabled
        self._campaigns: dict[Path, CampaignReadStore] = {}
        self._failed_campaign_roots: set[Path] = set()
        self._cache: dict[Path, _CachedRun] = {}
        self._submission_times: dict[Path, SubmissionTimes] = {}
        self._lock = threading.RLock()

    def list_runs(self) -> DashboardRunList:
        """Return recent valid runs ordered by durable artifact activity."""

        with self._lock:
            campaigns = self._load_campaigns()
            campaign_paths = self._campaign_paths(campaigns)
            views = [
                self._campaign_summary(loaded, run, campaign_paths[run.run_id])
                for loaded in campaigns
                for run in loaded.view.runs
            ]
            views.extend(
                (
                    self._invalid_run(path.parent, path)
                    if self._belongs_to_failed_campaign(path)
                    else self._standalone_summary(path)
                )
                for path in self._discover_manifests()
                if path.parent not in set(campaign_paths.values())
            )
            views.sort(key=lambda view: (view.updated_at, view.run_id), reverse=True)
            resources = tuple(
                DashboardCampaignResources(
                    campaign_id=loaded.view.plan.campaign_id,
                    scheduler=loaded.store.scheduler_status(loaded.view.plan),
                )
                for loaded in campaigns
            )
        fields = set(DashboardRunSummary.model_fields)
        summaries = tuple(
            DashboardRunSummary.model_validate(view.model_dump(include=fields)) for view in views
        )
        return DashboardRunList(runs=summaries, resources=resources)

    def get_run(self, run_ref: str) -> DashboardRun:
        """Return one run by its server-issued path-opaque reference."""

        if len(run_ref) != 16 or any(character not in "0123456789abcdef" for character in run_ref):
            raise KeyError(run_ref)
        with self._lock:
            campaigns = self._load_campaigns()
            campaign_paths = self._campaign_paths(campaigns)
            for loaded in campaigns:
                for run in loaded.view.runs:
                    run_directory = campaign_paths[run.run_id]
                    if self._run_ref(run_directory) == run_ref:
                        return self._with_timing(
                            run_directory, self._load_campaign_run(loaded, run, run_directory)
                        )
            candidates = [
                path
                for path in self._discover_manifests()
                if path.parent not in set(campaign_paths.values())
                and self._run_ref(path.parent) == run_ref
            ]
            if not candidates:
                raise KeyError(run_ref)
            path = candidates[0]
            view = (
                self._invalid_run(path.parent, path)
                if self._belongs_to_failed_campaign(path)
                else self._load_run(path)
            )
            return self._with_timing(path.parent, view)

    def get_control_target(self, run_ref: str) -> DashboardRunControlTarget:
        """Resolve a controllable Campaign Run without exposing its task identity."""

        if not self.controls_enabled:
            raise KeyError(run_ref)
        if len(run_ref) != 16 or any(character not in "0123456789abcdef" for character in run_ref):
            raise KeyError(run_ref)
        with self._lock:
            campaigns = self._load_campaigns()
            campaign_paths = self._campaign_paths(campaigns)
            for loaded in campaigns:
                for run in loaded.view.runs:
                    run_directory = campaign_paths[run.run_id]
                    if self._run_ref(run_directory) != run_ref:
                        continue
                    projected = self._load_campaign_run(loaded, run, run_directory)
                    action, _label = self._campaign_control(
                        run,
                        waiting_for_previous_seed=loaded.view.waiting_for_previous_seed(run),
                    )
                    if projected.status == "invalid":
                        action = "none"
                    return DashboardRunControlTarget(
                        database=loaded.store.database,
                        campaign_id=loaded.view.plan.campaign_id,
                        task_key=run.task_key,
                        revision=run.revision,
                        action=action,
                    )
        raise KeyError(run_ref)

    def revision(self) -> str:
        """Return an opaque digest that changes with dashboard-visible artifacts."""

        with self._lock:
            parts = [
                f"{self._run_ref(path.parent)}:"
                f"{self._signature(path.parent, include_performance=True)!r}"
                for path in self._discover_manifests()
            ]
            parts.extend(
                f"campaign:{store.revision_signature()!r}:{store.lease_signature()}:"
                f"sampler:{store.scheduler_is_live()}"
                for store in self._refresh_campaigns()
            )
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _load_campaigns(self) -> tuple[_LoadedCampaign, ...]:
        loaded: list[_LoadedCampaign] = []
        failed: set[Path] = set()
        for store in self._refresh_campaigns():
            try:
                loaded.append(_LoadedCampaign(store, store.load(include_details=False)))
            except (OSError, sqlite3.Error, ValueError):
                failed.add(store.root)
        self._failed_campaign_roots = failed
        return tuple(loaded)

    def _refresh_campaigns(self) -> tuple[CampaignReadStore, ...]:
        discovered = CampaignReadStore.discover_all(self.runs_root)
        self._campaigns = {
            candidate.root: self._campaigns.get(candidate.root, candidate)
            for candidate in discovered
        }
        return tuple(self._campaigns[path] for path in sorted(self._campaigns))

    def _belongs_to_failed_campaign(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(resolved.is_relative_to(root) for root in self._failed_campaign_roots)

    def _campaign_paths(self, campaigns: tuple[_LoadedCampaign, ...]) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        for loaded in campaigns:
            for run in loaded.view.runs:
                unresolved = (
                    loaded.store.root / "runs" / f"seed-{run.seed}" / lineup_run_slug(run.lineup_id)
                )
                if self._path_has_link(unresolved, loaded.store.root):
                    raise ValueError("Campaign Run path contains a link or junction")
                path = unresolved.resolve()
                if not path.is_relative_to(loaded.store.root):
                    raise ValueError("Campaign Run path escapes its Campaign root")
                if run.run_id in paths:
                    raise ValueError("Campaign Run identity is duplicated below dashboard root")
                paths[run.run_id] = path
        return paths

    def _discover_manifests(self) -> tuple[Path, ...]:
        candidates: list[tuple[int, Path]] = []
        for manifest_path in metadata_files(self.runs_root, "manifest.json"):
            if any(
                part.casefold() == "agent-memory"
                for part in manifest_path.relative_to(self.runs_root).parts
            ):
                continue  # Agent-authored files cannot impersonate benchmark runs.
            if self._is_link(manifest_path):
                continue
            resolved = manifest_path.resolve()
            if not resolved.is_relative_to(self.runs_root) or not resolved.is_file():
                continue
            try:
                activity = max(
                    path.stat().st_mtime_ns
                    for path in self._artifact_paths(resolved.parent)
                    if path.is_file()
                )
            except OSError:
                continue
            candidates.append((activity, resolved))
        candidates.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
        return tuple(path for _, path in candidates[:_MAX_DISCOVERED_RUNS])

    def _load_campaign_run(
        self,
        loaded: _LoadedCampaign,
        run: CampaignRunView,
        run_directory: Path,
    ) -> DashboardRun:
        """Project one Campaign job from SQLite, never from a recovery JSON file."""

        waiting_for_previous_seed = loaded.view.waiting_for_previous_seed(run)
        campaign_key = f"{run.cache_key}:seed-wait:{waiting_for_previous_seed}"
        signature = self._signature(run_directory, campaign_key=campaign_key)
        cached = self._cache.get(run_directory)
        if cached and cached.signature == signature:
            return self._decorate_campaign(loaded, run, cached.view)
        manifest_path = run_directory / "manifest.json"
        try:
            details = loaded.store.load(run_id=run.run_id)
            loaded = _LoadedCampaign(loaded.store, details)
            run = next(item for item in details.runs if item.run_id == run.run_id)
            manifest = (
                RunManifest.model_validate_json(self._read_text(manifest_path, max_bytes=4_000_000))
                if manifest_path.is_file()
                else None
            )
            if manifest is not None:
                self._validate_campaign_manifest(loaded.view, run, manifest)
            checkpoint = run.checkpoint
            include_events = True
            snapshot_invalid = False
            if run.status is RunJobStatus.COMPLETED:
                try:
                    final_checkpoint = self._final_checkpoint(run_directory, manifest, run)
                except (OSError, ValueError):
                    final_checkpoint = None
                    snapshot_invalid = True
                if (
                    checkpoint is not None
                    and final_checkpoint is not None
                    and checkpoint != final_checkpoint
                ):
                    snapshot_invalid = True
                if checkpoint is None:
                    checkpoint = final_checkpoint
                include_events = self._campaign_trace_verified(run_directory, run)
            if checkpoint is not None:
                if manifest is None:
                    raise ValueError("Campaign checkpoint has no immutable manifest")
                self._validate_checkpoint_identity(manifest, checkpoint)
            if manifest is None:
                view = self._campaign_placeholder(loaded.view, run, run_directory)
            elif checkpoint is not None:
                view = self._project_run(
                    run_directory,
                    manifest,
                    checkpoint,
                    campaign_id=loaded.view.plan.campaign_id,
                    seed=run.seed,
                    job_status=run.status,
                    updated_at_override=run.updated_at,
                    staged_decisions=run.staged_decisions,
                    campaign_interrupted=run.lease_expired,
                    include_events=include_events,
                )
                if run.result is not None:
                    self._validate_campaign_result(manifest, checkpoint, run.result)
                if snapshot_invalid:
                    view = view.model_copy(
                        update={
                            "error": (
                                "Final audit snapshot failed integrity validation; "
                                "SQLite detail remains authoritative."
                            )
                        }
                    )
            elif run.status is RunJobStatus.COMPLETED and run.result is not None:
                view = self._campaign_result_view(
                    run_directory,
                    manifest,
                    run,
                    campaign_id=loaded.view.plan.campaign_id,
                    include_events=include_events,
                    snapshot_invalid=snapshot_invalid,
                )
            else:
                view = self._project_run(
                    run_directory,
                    manifest,
                    None,
                    campaign_id=loaded.view.plan.campaign_id,
                    seed=run.seed,
                    job_status=run.status,
                    updated_at_override=run.updated_at,
                )
        except (KeyError, OSError, ValueError):
            view = self._invalid_run(
                run_directory,
                manifest_path,
                campaign_id=loaded.view.plan.campaign_id,
                seed=run.seed,
                job_status=run.status,
            )
        self._cache[run_directory] = _CachedRun(signature=signature, view=view)
        return self._decorate_campaign(loaded, run, view)

    def _campaign_summary(
        self, loaded: _LoadedCampaign, run: CampaignRunView, run_directory: Path
    ) -> DashboardRun:
        """Build a lifecycle-only listing without loading any economic snapshot."""
        view = self._campaign_placeholder(loaded.view, run, run_directory)
        view = view.model_copy(
            update={
                "detail_available": False,
                "phase": run.checkpoint_phase or run.status.value,
                "active_company_count": run.active_companies
                if run.active_companies is not None
                else len(run.company_ids),
            }
        )
        return self._decorate_campaign(loaded, run, view)

    def _decorate_campaign(
        self, loaded: _LoadedCampaign, run: CampaignRunView, view: DashboardRun
    ) -> DashboardRun:
        """Refresh lightweight lease/control state without invalidating economic data."""
        waiting_for_previous_seed = loaded.view.waiting_for_previous_seed(run)
        if view.status != "invalid":
            view = view.model_copy(
                update={
                    "status": self._campaign_status(run.status, lease_expired=run.lease_expired),
                    "job_status": run.status.value,
                    "updated_at": run.updated_at,
                    "error": run.last_error
                    or (
                        view.error
                        if run.status is RunJobStatus.COMPLETED
                        else self._campaign_error(run.status, lease_expired=run.lease_expired)
                    ),
                    "economic_revision": run.cache_key,
                    "retry_not_before": run.retry_not_before,
                    "last_error": run.last_error,
                    "attempt": run.attempt,
                    "attempt_limit": run.attempt_limit,
                    "performance": self._read_performance(
                        loaded.store.root
                        / "runs"
                        / f"seed-{run.seed}"
                        / lineup_run_slug(run.lineup_id),
                        run.run_id,
                        run.config_hash,
                    ),
                }
            )
        if waiting_for_previous_seed and view.status != "invalid":
            view = view.model_copy(
                update={
                    "status": "waiting_seed",
                    "phase": "waiting_previous_seed",
                    "error": None,
                }
            )
        elif loaded.view.plan.execution.schedule == "seed_priority" and view.status != "invalid":
            if run.status is RunJobStatus.QUEUED:
                view = view.model_copy(update={"status": "queued", "phase": "waiting_capacity"})
            elif run.status is RunJobStatus.INTERRUPTED:
                view = view.model_copy(update={"status": "waiting_retry", "phase": "retry_backoff"})
        control_action, control_label = self._campaign_control(
            run,
            waiting_for_previous_seed=waiting_for_previous_seed,
        )
        if not self.controls_enabled:
            control_action, control_label = "none", "Read-only run"
        if view.status == "invalid":
            control_action, control_label = "none", "Not recoverable"
        view = view.model_copy(
            update={
                "revision": run.revision,
                "control_action": control_action,
                "control_label": control_label,
            }
        )
        return view

    @staticmethod
    def _campaign_control(
        run: CampaignRunView,
        *,
        waiting_for_previous_seed: bool = False,
    ) -> tuple[str, str]:
        """Project exactly one safe action from the durable Run lifecycle."""

        if waiting_for_previous_seed:
            return "none", "Waiting for the previous seed"
        if run.pause_requested:
            if run.lease_expired:
                return "resume", "Resume"
            return "none", "Pausing..."
        if run.status is RunJobStatus.QUEUED:
            return "resume", "Resume"
        if run.status is RunJobStatus.RUNNING and run.lease_expired:
            return "resume", "Resume"
        if run.status in {RunJobStatus.RUNNING, RunJobStatus.INTERRUPTED}:
            return "pause", "Pause"
        if run.status is RunJobStatus.STOPPED and run.stop_reason is RunStopReason.USER_REQUESTED:
            return "resume", "Resume"
        if (
            run.status in {RunJobStatus.STOPPED, RunJobStatus.FAILED}
            and (
                run.status is RunJobStatus.FAILED
                or run.stop_reason is RunStopReason.PROVIDER_RETRY_EXHAUSTED
            )
            and run.last_failure_phase is FailurePhase.PROVIDER
            and run.checkpoint_hash is not None
        ):
            return "resume", "Resume"
        if run.status is RunJobStatus.COMPLETED:
            return "none", "Completed"
        return "none", "Not recoverable"

    @staticmethod
    def _validate_campaign_manifest(
        campaign: CampaignView,
        run: CampaignRunView,
        manifest: RunManifest,
    ) -> None:
        assignments = manifest.config.assignments
        scenario = manifest.config.scenario
        if (
            manifest.run_id != run.run_id
            or manifest.config_hash != run.config_hash
            or tuple(item.company_id for item in assignments) != run.company_ids
            or tuple(item.model_id for item in assignments) != run.model_ids
            or scenario.seed != run.seed
            or scenario.total_days != campaign.plan.days
            or scenario.model_copy(update={"seed": 0}) != campaign.plan.scenario_template
        ):
            raise ValueError("Campaign manifest differs from its frozen plan and database")

    def _final_checkpoint(
        self,
        run_directory: Path,
        manifest: RunManifest | None,
        run: CampaignRunView,
    ) -> RunCheckpoint | None:
        snapshot_path = run_directory / "final_checkpoint.json"
        if not snapshot_path.is_file():
            return None
        checkpoint = RunCheckpoint.model_validate_json(
            self._read_text(snapshot_path, max_bytes=128_000_000)
        )
        receipt = run.replay_receipt
        if (
            manifest is None
            or checkpoint.phase is not CheckpointPhase.COMPLETE
            or checkpoint.run_id != run.run_id
            or checkpoint.config_hash != run.config_hash
            or checkpoint.current_state.day != run.current_day
            or receipt is None
            or receipt.checkpoint_sha256 != canonical_model_hash(checkpoint)
            or receipt.final_state_hash != canonical_model_hash(checkpoint.current_state)
            or receipt.verified_windows != len(checkpoint.completed_windows)
            or receipt.verified_days != checkpoint.current_state.day - checkpoint.initial_state.day
        ):
            raise ValueError("Campaign final checkpoint identity is invalid")
        return checkpoint

    def _campaign_trace_verified(
        self,
        run_directory: Path,
        run: CampaignRunView,
    ) -> bool:
        receipt = run.replay_receipt
        trace_path = run_directory / "trace.jsonl"
        if receipt is None or not trace_path.is_file():
            return False
        try:
            safe_path = self._safe_artifact(trace_path)
            if safe_path.stat().st_size > 32_000_000:
                return False
            digest = hashlib.sha256(safe_path.read_bytes()).hexdigest()
        except (OSError, ValueError):
            return False
        return digest == receipt.runtime_trace_sha256

    @staticmethod
    def _validate_campaign_result(
        manifest: RunManifest,
        checkpoint: RunCheckpoint,
        result: ArenaRunResult,
    ) -> None:
        scores = score_world(checkpoint.current_state, manifest.config.scenario)
        expected_scores = {
            company_id: score.official_score_cents for company_id, score in scores.items()
        }
        expected_statuses = {
            company_id: dict(
                sorted(
                    Counter(
                        decision.agent_status
                        for window in checkpoint.completed_windows
                        for decision in window.decisions
                        if decision.company_id == company_id
                    ).items()
                )
            )
            for company_id in manifest.config.company_ids
        }
        if (
            result.assignments != manifest.config.assignments
            or result.scenario_id != manifest.config.scenario.scenario_id
            or result.official_score_by_company_cents != expected_scores
            or result.decision_status_by_company != expected_statuses
        ):
            raise ValueError("Campaign result differs from its final checkpoint")

    def _campaign_placeholder(
        self,
        campaign: CampaignView,
        run: CampaignRunView,
        run_directory: Path,
    ) -> DashboardRun:
        scenario = campaign.plan.scenario_template.model_copy(update={"seed": run.seed})
        return DashboardRun(
            run_ref=self._run_ref(run_directory),
            run_id=run.run_id,
            campaign_id=campaign.plan.campaign_id,
            seed=run.seed,
            scenario_id=scenario.scenario_id,
            model_ids=run.model_ids,
            job_status=run.status.value,
            status=self._campaign_status(run.status, lease_expired=run.lease_expired),
            phase=run.status.value,
            day=run.current_day,
            total_days=campaign.plan.days,
            progress=min(1.0, run.current_day / campaign.plan.days),
            company_count=len(run.company_ids),
            active_company_count=len(run.company_ids),
            updated_at=run.updated_at,
            error=self._campaign_error(run.status, lease_expired=run.lease_expired),
            macro_index=scenario.macro_mean,
            total_score_cents=0,
            total_cash_cents=scenario.initial_cash_cents * len(run.company_ids),
            completed_windows=0,
            current_window=0,
            decision_status_counts={},
            companies=(),
            recent_events=(),
            artifacts={},
        )

    def _campaign_result_view(
        self,
        run_directory: Path,
        manifest: RunManifest,
        run: CampaignRunView,
        *,
        campaign_id: str,
        include_events: bool,
        snapshot_invalid: bool,
    ) -> DashboardRun:
        result = run.result
        if result is None or result.assignments != manifest.config.assignments:
            raise ValueError("Campaign verified result differs from its manifest")
        companies = tuple(
            DashboardCompany(
                company_id=assignment.company_id,
                model_id=assignment.model_id,
                status="finished",
                score_cents=result.official_score_by_company_cents[assignment.company_id],
                detail_available=False,
                cash_cents=None,
                revenue_cents=None,
                cost_cents=None,
                inventory_units=None,
                backlog_units=None,
                support_backlog=None,
                satisfaction=None,
                last_agent_status="finished",
                last_decision="Final audit snapshot unavailable; showing verified results only",
                provider_turns=0,
                tool_calls=0,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                cash_series=(),
                trajectory=(),
            )
            for assignment in result.assignments
        )
        status_counts = Counter(
            status
            for counts in result.decision_status_by_company.values()
            for status, count in counts.items()
            for _ in range(count)
        )
        completed_windows = max(
            (sum(counts.values()) for counts in result.decision_status_by_company.values()),
            default=0,
        )
        return DashboardRun(
            run_ref=self._run_ref(run_directory),
            run_id=manifest.run_id,
            campaign_id=campaign_id,
            seed=run.seed,
            scenario_id=manifest.config.scenario.scenario_id,
            model_ids=tuple(item.model_id for item in result.assignments),
            job_status=run.status.value,
            status="complete",
            phase="verified_result_only",
            day=run.current_day,
            total_days=manifest.config.scenario.total_days,
            progress=1.0,
            company_count=len(companies),
            active_company_count=0,
            updated_at=run.updated_at,
            error=(
                "Final audit snapshot failed integrity validation; showing verified scores only."
                if snapshot_invalid
                else None
            ),
            detail_available=False,
            macro_index=None,
            total_score_cents=sum(result.official_score_by_company_cents.values()),
            total_cash_cents=None,
            completed_windows=completed_windows,
            current_window=completed_windows,
            decision_status_counts=dict(sorted(status_counts.items())),
            companies=companies,
            recent_events=(
                self._read_events(run_directory / "trace.jsonl", expected_run_id=manifest.run_id)
                if include_events
                else ()
            ),
            artifacts=self._artifact_links(run_directory),
        )

    @staticmethod
    def _campaign_status(
        status: RunJobStatus,
        *,
        lease_expired: bool = False,
    ) -> DashboardRunStatus:
        if lease_expired:
            return "interrupted"
        if status is RunJobStatus.COMPLETED:
            return "complete"
        if status is RunJobStatus.RUNNING:
            return "running"
        if status is RunJobStatus.FAILED:
            return "failed"
        if status in {RunJobStatus.INTERRUPTED, RunJobStatus.STOPPED}:
            return "interrupted"
        return "starting"

    @staticmethod
    def _campaign_error(status: RunJobStatus, *, lease_expired: bool = False) -> str | None:
        if lease_expired:
            return "Campaign Run lease expired and is recoverable."
        if status is RunJobStatus.FAILED:
            return "Campaign Run failed; inspect the local Campaign report for details."
        if status is RunJobStatus.STOPPED:
            return "Campaign Run is stopped."
        return None

    def _load_run(self, manifest_path: Path) -> DashboardRun:
        run_directory = manifest_path.parent
        signature = self._signature(run_directory)
        cached = self._cache.get(run_directory)
        if cached and cached.signature == signature:
            return self._with_performance(run_directory, cached.view)
        try:
            manifest_text = self._read_text(manifest_path, max_bytes=4_000_000)
            manifest = RunManifest.model_validate_json(manifest_text)
            checkpoint_path = run_directory / "checkpoint.json"
            checkpoint = AtomicCheckpointStore(checkpoint_path).load(
                run_id=manifest.run_id,
                config_hash=manifest.config_hash,
            )
            if checkpoint is not None:
                self._validate_checkpoint_identity(manifest, checkpoint)
            view = self._project_run(run_directory, manifest, checkpoint)
        except (KeyError, OSError, ValueError):
            view = self._invalid_run(run_directory, manifest_path)
        self._cache[run_directory] = _CachedRun(signature=signature, view=view)
        return self._with_performance(run_directory, view)

    def _standalone_summary(self, manifest_path: Path) -> DashboardRun:
        """Use a small diagnostic frontier for listings; details still validate real state."""
        try:
            manifest = RunManifest.model_validate_json(
                self._read_text(manifest_path, max_bytes=4_000_000)
            )
            performance = self._read_performance(
                manifest_path.parent, manifest.run_id, manifest.config_hash
            )
            if performance is None:
                return self._load_run(manifest_path)
            if (
                performance.total_days != manifest.config.scenario.total_days
                or not 0 <= performance.day <= performance.total_days
            ):
                return self._load_run(manifest_path)
            view = self._project_run(manifest_path.parent, manifest, None)
            status = (
                "complete"
                if performance.phase == "complete"
                else ("running" if self._is_locked(manifest_path.parent) else "interrupted")
            )
            return view.model_copy(
                update={
                    "detail_available": False,
                    "day": performance.day,
                    "phase": performance.phase,
                    "status": status,
                    "progress": min(1.0, performance.day / manifest.config.scenario.total_days),
                    "active_company_count": performance.active_company_count,
                    "updated_at": performance.updated_at,
                }
            )
        except (OSError, ValueError):
            return self._load_run(manifest_path)

    def _with_performance(self, run_directory: Path, view: DashboardRun) -> DashboardRun:
        performance = self._read_performance(run_directory, view.run_id)
        return view.model_copy(update={"performance": performance})

    def _with_timing(self, run_directory: Path, view: DashboardRun) -> DashboardRun:
        """Decorate cached decisions without reloading the economic checkpoint."""
        if view.status == "invalid" or not view.detail_available:
            return view
        try:
            path = self._safe_artifact(run_directory / "performance.jsonl")
            submissions = self._submission_times.setdefault(run_directory, SubmissionTimes())
            submissions.refresh(path)
        except (OSError, ValueError):
            self._submission_times.pop(run_directory, None)
            submissions = SubmissionTimes()
        try:
            records = self._read_trace(run_directory / "trace.jsonl", expected_run_id=view.run_id)
        except (OSError, ValueError):
            records = ()
        settlements = {
            record.window_index: record.recorded_at
            for record in records
            if record.kind is TraceKind.WINDOW_COMMITTED
            and record.window_index is not None
            and record.recorded_at.utcoffset() is not None
        }
        companies = tuple(submissions.project(company, settlements) for company in view.companies)
        return view.model_copy(update={"companies": companies})

    def _read_performance(
        self, run_directory: Path, run_id: str, config_hash: str | None = None
    ) -> PerformanceStatus | None:
        """Read bounded, prompt-free diagnostic data; never trust it for recovery."""
        try:
            value = PerformanceStatus.model_validate_json(
                self._read_text(run_directory / "performance_status.json", max_bytes=1_000_000)
            )
            if value.run_id != run_id or (
                config_hash is not None and value.config_hash != config_hash
            ):
                return None
            return value
        except (OSError, ValueError):
            return None

    def _project_run(
        self,
        run_directory: Path,
        manifest: RunManifest,
        checkpoint: RunCheckpoint | None,
        *,
        campaign_id: str | None = None,
        seed: int | None = None,
        job_status: RunJobStatus | None = None,
        updated_at_override: datetime | None = None,
        staged_decisions: tuple[CampaignStagedDecision, ...] = (),
        campaign_interrupted: bool = False,
        include_events: bool = True,
    ) -> DashboardRun:
        scenario = manifest.config.scenario
        updated_at = updated_at_override or self._updated_at(run_directory)
        if checkpoint is None:
            active = self._is_locked(run_directory)
            return DashboardRun(
                run_ref=self._run_ref(run_directory),
                run_id=manifest.run_id,
                campaign_id=campaign_id,
                seed=scenario.seed if seed is None else seed,
                scenario_id=scenario.scenario_id,
                model_ids=tuple(assignment.model_id for assignment in manifest.config.assignments),
                job_status=job_status.value if job_status is not None else None,
                status=(
                    self._campaign_status(job_status, lease_expired=campaign_interrupted)
                    if job_status is not None
                    else "starting"
                    if active
                    else "interrupted"
                ),
                phase=job_status.value if job_status is not None else "manifest_only",
                day=0,
                total_days=scenario.total_days,
                progress=0.0,
                company_count=len(manifest.config.assignments),
                active_company_count=len(manifest.config.assignments),
                updated_at=updated_at,
                error=(
                    self._campaign_error(job_status, lease_expired=campaign_interrupted)
                    if job_status is not None
                    else None
                ),
                macro_index=scenario.macro_mean,
                total_score_cents=0,
                total_cash_cents=scenario.initial_cash_cents * len(manifest.config.assignments),
                completed_windows=0,
                current_window=0,
                decision_status_counts={},
                companies=(),
                recent_events=(
                    self._read_events(
                        run_directory / "trace.jsonl", expected_run_id=manifest.run_id
                    )
                    if include_events
                    else ()
                ),
                artifacts=self._artifact_links(run_directory),
            )

        state = checkpoint.current_state
        assignment_by_company = {item.company_id: item for item in manifest.config.assignments}
        scores = score_world(state, scenario)
        receipts = tuple(self._decision_receipts(checkpoint, staged_decisions))
        status_counts = Counter(item.sealed.agent_status for item in receipts)
        latest = self._latest_receipts(receipts)
        companies = tuple(
            self._company_view(
                company_id,
                state.companies[company_id],
                assignment_by_company[company_id].model_id,
                scores[company_id].official_score_cents,
                receipts,
                latest.get(company_id),
            )
            for company_id in sorted(state.companies)
        )
        status: DashboardRunStatus = (
            self._campaign_status(job_status, lease_expired=campaign_interrupted)
            if job_status is not None
            else "complete"
        )
        if job_status is None and checkpoint.phase is not CheckpointPhase.COMPLETE:
            status = "running" if self._is_locked(run_directory) else "interrupted"
        active = sum(company.status.value == "active" for company in state.companies.values())
        return DashboardRun(
            run_ref=self._run_ref(run_directory),
            run_id=manifest.run_id,
            campaign_id=campaign_id,
            seed=scenario.seed if seed is None else seed,
            scenario_id=scenario.scenario_id,
            model_ids=tuple(assignment.model_id for assignment in manifest.config.assignments),
            job_status=job_status.value if job_status is not None else None,
            status=status,
            phase=checkpoint.phase.value,
            day=state.day,
            total_days=scenario.total_days,
            progress=min(1.0, state.day / scenario.total_days),
            company_count=len(state.companies),
            active_company_count=active,
            updated_at=updated_at,
            error=(
                self._campaign_error(job_status, lease_expired=campaign_interrupted)
                if job_status is not None
                else None
            ),
            macro_index=state.macro_index,
            market_signal=MarketDynamics(scenario).public_signal(state),
            total_score_cents=sum(item.official_score_cents for item in scores.values()),
            total_cash_cents=sum(company.cash_cents for company in state.companies.values()),
            completed_windows=len(checkpoint.completed_windows),
            current_window=checkpoint.next_window_index,
            decision_status_counts=dict(sorted(status_counts.items())),
            companies=companies,
            recent_events=(
                self._read_events(run_directory / "trace.jsonl", expected_run_id=manifest.run_id)
                if include_events
                else ()
            ),
            artifacts=self._artifact_links(run_directory),
        )

    @staticmethod
    def _validate_checkpoint_identity(
        manifest: RunManifest,
        checkpoint: RunCheckpoint,
    ) -> None:
        expected_companies = set(manifest.config.company_ids)
        if (
            checkpoint.run_id != manifest.run_id
            or checkpoint.config_hash != manifest.config_hash
            or canonical_model_hash(checkpoint.initial_state) != manifest.initial_state_hash
            or checkpoint.current_state.scenario_id != manifest.config.scenario.scenario_id
            or set(checkpoint.current_state.companies) != expected_companies
            or set(checkpoint.initial_state.companies) != expected_companies
        ):
            raise ValueError("checkpoint identity does not match its manifest")

    @staticmethod
    def _decision_receipts(
        checkpoint: RunCheckpoint,
        staged_decisions: tuple[CampaignStagedDecision, ...] = (),
    ) -> Iterable[_DecisionReceipt]:
        projected: set[tuple[int, str]] = set()
        for window in checkpoint.completed_windows:
            for decision in window.decisions:
                projected.add((window.window_index, decision.company_id))
                yield _DecisionReceipt(
                    window_index=window.window_index,
                    day=window.start_day,
                    end_day=window.end_day,
                    stage="completed",
                    sealed=decision,
                )
        if checkpoint.pending_window is not None:
            for decision in checkpoint.pending_window.decisions:
                projected.add((checkpoint.pending_window.window_index, decision.company_id))
                yield _DecisionReceipt(
                    window_index=checkpoint.pending_window.window_index,
                    day=checkpoint.pending_window.start_day,
                    end_day=None,
                    stage="pending",
                    sealed=decision,
                )
        if checkpoint.collecting_window is not None:
            for decision in checkpoint.collecting_window.decisions:
                projected.add((checkpoint.collecting_window.window_index, decision.company_id))
                yield _DecisionReceipt(
                    window_index=checkpoint.collecting_window.window_index,
                    day=checkpoint.collecting_window.start_day,
                    end_day=None,
                    stage="collecting",
                    sealed=decision,
                )
        for staged in staged_decisions:
            key = (staged.window_index, staged.company_id)
            if staged.window_index < checkpoint.next_window_index or key in projected:
                continue
            if (
                staged.window_index != checkpoint.next_window_index
                or staged.response.decision.world_version != checkpoint.current_state.version
            ):
                raise ValueError("staged Campaign decision is ahead of its checkpoint frontier")
            yield _DecisionReceipt(
                window_index=staged.window_index,
                day=checkpoint.current_state.day,
                end_day=None,
                stage="collecting",
                sealed=staged.response,
            )

    @staticmethod
    def _latest_receipts(
        receipts: tuple[_DecisionReceipt, ...],
    ) -> dict[str, _DecisionReceipt]:
        latest: dict[str, _DecisionReceipt] = {}
        for receipt in receipts:
            latest[receipt.sealed.company_id] = receipt
        return latest

    def _company_view(
        self,
        company_id: str,
        company: CompanyState,
        model_id: str,
        score_cents: int,
        receipts: tuple[_DecisionReceipt, ...],
        latest: _DecisionReceipt | None,
    ) -> DashboardCompany:
        own_receipts = [item for item in receipts if item.sealed.company_id == company_id]
        satisfaction = fmean(company.satisfaction.values()) if company.satisfaction else 0.0
        latest_status = latest.sealed.agent_status if latest else "waiting"
        latest_decision = (
            self._decision_summary(latest.sealed.decision) if latest else "Awaiting CEO decision"
        )
        return DashboardCompany(
            company_id=company_id,
            model_id=model_id,
            status=company.status.value,
            score_cents=score_cents,
            cash_cents=company.cash_cents,
            revenue_cents=company.cumulative_revenue_cents,
            cost_cents=company.cumulative_cost_cents,
            refunds_cents=company.cumulative_refunds_cents,
            gross_profit_cents=company.cumulative_revenue_cents
            - sum(item.cogs_cents for item in company.daily_history),
            daily_cashflow=self._cashflow_view(company),
            action_receipts=tuple(company.action_receipts[-_MAX_ACTION_RECEIPTS:]),
            inventory_units=sum(company.inventory.values()),
            backlog_units=sum(order.quantity for order in company.backlog),
            support_backlog=company.support_backlog,
            satisfaction=satisfaction,
            last_agent_status=latest_status,
            last_decision=latest_decision,
            provider_turns=sum(item.sealed.usage.provider_turns for item in own_receipts),
            tool_calls=sum(item.sealed.usage.tool_calls for item in own_receipts),
            input_tokens=sum(item.sealed.usage.input_tokens for item in own_receipts),
            output_tokens=sum(item.sealed.usage.output_tokens for item in own_receipts),
            latency_ms=sum(item.sealed.usage.latency_ms for item in own_receipts),
            cash_series=self._cash_series(company),
            trajectory=tuple(self._trajectory_view(item) for item in own_receipts),
        )

    def _trajectory_view(self, receipt: _DecisionReceipt) -> DashboardDecision:
        decision = receipt.sealed.decision
        usage = receipt.sealed.usage
        summary = self._decision_summary(decision)
        if receipt.sealed.agent_status == "budget_exhausted":
            summary = "Decision budget exhausted; draft not applied; prior policy retained"
        elif receipt.sealed.agent_status == "timeout":
            kind = "Infrastructure timeout" if receipt.sealed.failure else "Decision timeout"
            summary = f"{kind}; draft not applied; prior policy retained"
        return DashboardDecision(
            window_index=receipt.window_index,
            start_day=receipt.day,
            end_day=receipt.end_day,
            stage=receipt.stage,
            status=receipt.sealed.agent_status,
            summary=summary,
            failure=receipt.sealed.failure,
            actions=self._decision_actions(decision),
            provider_turns=usage.provider_turns,
            tool_calls=usage.tool_calls,
            output_tokens=usage.output_tokens,
            decision_duration_ms=usage.latency_ms or None,
        )

    @staticmethod
    def _cashflow_view(company: CompanyState) -> tuple[DashboardDailyCashflow, ...]:
        history = company.daily_history
        start_index = max(0, len(history) - _MAX_LEDGER_DAYS)
        opening_cash = (
            history[start_index - 1].ending_cash_cents
            if start_index
            else company.initial_cash_cents
        )
        rows: list[DashboardDailyCashflow] = []
        for record in history[start_index:]:
            rows.append(
                DashboardDailyCashflow(
                    day=record.day,
                    opening_cash_cents=opening_cash,
                    revenue_cents=record.revenue_cents,
                    refunds_cents=record.refunds_cents,
                    payments_cents=record.costs_cents,
                    cogs_cents=record.cogs_cents,
                    gross_profit_cents=record.gross_profit_cents,
                    net_cashflow_cents=record.net_cashflow_cents,
                    ending_cash_cents=record.ending_cash_cents,
                    expenses=record.expense_breakdown,
                )
            )
            opening_cash = record.ending_cash_cents
        return tuple(rows)

    @staticmethod
    def _decision_summary(decision: CompanyDecision) -> str:
        actions: list[str] = []
        if decision.products:
            price_changes = [
                f"{item.product_id} ${item.price_cents / 100:.2f}"
                for item in decision.products
                if item.price_cents is not None
            ]
            actions.append(
                f"Prices {', '.join(price_changes)}"
                if price_changes
                else f"{len(decision.products)} product policies"
            )
        if decision.marketing:
            spend = sum(item.daily_budget_cents for item in decision.marketing)
            actions.append(f"Marketing ${spend / 100:.0f}/day")
        if decision.reliability_budget_cents is not None:
            actions.append(f"Reliability ${decision.reliability_budget_cents / 100:.0f}/day")
        if decision.support_budget_cents is not None:
            actions.append(f"Support ${decision.support_budget_cents / 100:.0f}/day")
        if decision.research_project_tier is not None:
            actions.append(f"Innovation tier {decision.research_project_tier}")
        if decision.capacity_target_tier is not None:
            actions.append(f"Capacity tier {decision.capacity_target_tier}")
        if decision.capacity_downsize_tier is not None:
            actions.append(f"Downsize to tier {decision.capacity_downsize_tier}")
        if decision.cancel_capacity_project_id is not None:
            actions.append("Cancel capacity construction")
        if decision.enterprise_bids:
            actions.append(f"{len(decision.enterprise_bids)} enterprise bids")
        if decision.market_research:
            actions.append(f"{len(decision.market_research)} market research requests")
        return " · ".join(actions) if actions else "Keep prior operating policy"

    @staticmethod
    def _decision_actions(decision: CompanyDecision) -> tuple[DashboardAction, ...]:
        """Project the operating decision without notes, prompts, errors, or trace paths."""

        actions: list[DashboardAction] = []
        for product in decision.products:
            values: list[str] = []
            if product.price_cents is not None:
                values.append(f"Price {RunDashboard._money(product.price_cents)}")
            if product.promotion_cents:
                values.append(f"Promotion {RunDashboard._money(product.promotion_cents)}")
            if product.reorder_target is not None:
                values.append(f"Reorder target {product.reorder_target:,} units")
            if product.development_budget_cents:
                values.append(
                    "Product development "
                    f"{RunDashboard._money(product.development_budget_cents)}/day"
                )
            actions.append(
                DashboardAction(
                    category="product",
                    label=product.product_id,
                    value="; ".join(values) if values else "Review product policy",
                )
            )
        actions.extend(
            DashboardAction(
                category="marketing",
                label=f"{allocation.segment_id} · {allocation.channel}",
                value=f"{RunDashboard._money(allocation.daily_budget_cents)}/day",
            )
            for allocation in decision.marketing
        )
        for label, budget in (
            ("Reliability", decision.reliability_budget_cents),
            ("Customer support", decision.support_budget_cents),
        ):
            if budget is not None:
                actions.append(
                    DashboardAction(
                        category="operations",
                        label=label,
                        value=f"{RunDashboard._money(budget)}/day",
                    )
                )
        if decision.research_project_tier is not None:
            actions.append(
                DashboardAction(
                    category="research",
                    label="Product innovation",
                    value=f"Tier {decision.research_project_tier}",
                )
            )
        if decision.capacity_target_tier is not None:
            actions.append(
                DashboardAction(
                    category="capacity",
                    label="Capacity target",
                    value=f"Tier {decision.capacity_target_tier}",
                )
            )
        if decision.cancel_capacity_project_id is not None:
            actions.append(
                DashboardAction(
                    category="capacity",
                    label="Cancel construction",
                    value=f"Project {decision.cancel_capacity_project_id[:8]} "
                    "· See execution receipt",
                )
            )
        if decision.capacity_downsize_tier is not None:
            actions.append(
                DashboardAction(
                    category="capacity",
                    label="Downsize request",
                    value=f"Tier {decision.capacity_downsize_tier} · See execution receipt",
                )
            )
        actions.extend(
            DashboardAction(
                category="enterprise",
                label=f"Bid {bid.tender_id[:6]}",
                value=f"Quote {RunDashboard._money(bid.unit_price_cents)}/unit",
            )
            for bid in decision.enterprise_bids
        )
        actions.extend(
            DashboardAction(
                category="market_research",
                label=request.segment_id,
                value=f"Tier {request.level}",
            )
            for request in decision.market_research
        )
        return tuple(actions)

    @staticmethod
    def _money(cents: int) -> str:
        return f"${cents / 100:,.2f}"

    @staticmethod
    def _cash_series(company: CompanyState) -> tuple[SeriesPoint, ...]:
        points = [SeriesPoint(day=0, value=company.initial_cash_cents)]
        points.extend(
            SeriesPoint(day=item.day, value=item.ending_cash_cents)
            for item in company.daily_history
        )
        if len(points) <= _MAX_SERIES_POINTS:
            return tuple(points)
        step = math.ceil((len(points) - 1) / (_MAX_SERIES_POINTS - 1))
        sampled = points[:-1:step]
        if sampled[-1] != points[-1]:
            sampled.append(points[-1])
        return tuple(sampled)

    def _read_events(self, trace_path: Path, *, expected_run_id: str) -> tuple[DashboardEvent, ...]:
        records = self._read_trace(trace_path, expected_run_id=expected_run_id)
        return tuple(self._event_view(item) for item in records[-_MAX_RECENT_EVENTS:])

    def _read_trace(self, trace_path: Path, *, expected_run_id: str) -> tuple[TraceRecord, ...]:
        if not trace_path.is_file():
            return ()
        records: list[TraceRecord] = []
        try:
            payload = self._safe_artifact(trace_path).read_bytes()
        except OSError:
            return ()
        if len(payload) > 32_000_000:
            raise ValueError("runtime trace exceeds dashboard read limit")
        lines = payload.splitlines(keepends=True)
        for index, raw in enumerate(lines):
            try:
                record = TraceRecord.model_validate_json(raw.rstrip(b"\r\n"))
            except ValueError as exc:
                torn_tail = index == len(lines) - 1 and not payload.endswith((b"\n", b"\r"))
                if torn_tail:
                    break
                raise ValueError("runtime trace contains an invalid record") from exc
            if record.sequence != len(records):
                raise ValueError("runtime trace sequence is not contiguous")
            if record.run_id != expected_run_id:
                raise ValueError("runtime trace identity does not match its manifest")
            records.append(record)
        return tuple(records)

    @staticmethod
    def _event_view(record: TraceRecord) -> DashboardEvent:
        tone = "success"
        if record.kind is TraceKind.RUN_STARTED:
            label, detail, tone = "Experiment started", "Initial world state saved", "info"
        elif record.kind is TraceKind.DECISION_BARRIER_SEALED:
            counts = Counter(item.status for item in record.decisions)
            sealed = counts.get("sealed", 0)
            failures = sum(counts.values()) - sealed
            label = f"Window {record.window_index + 1} Decisions sealed"
            detail = f"{sealed} submitted · {failures} fallback"
            tone = "success" if failures == 0 else "warning"
        elif record.kind is TraceKind.WINDOW_COMMITTED:
            label = f"Window {record.window_index + 1} settled"
            detail = f"World advanced to Day {record.end_day}"
        elif record.kind is TraceKind.RUN_COMPLETED:
            label, detail = "ExperimentCompleted", f"Finished at Day {record.end_day}"
        else:
            label, detail = (
                "Exact replay verified",
                "Saved state and decision history match exactly",
            )
        return DashboardEvent(
            sequence=record.sequence,
            kind=record.kind.value,
            label=label,
            detail=detail,
            day=record.end_day if record.end_day is not None else record.start_day,
            recorded_at=record.recorded_at if record.recorded_at.utcoffset() is not None else None,
            tone=tone,
        )

    def _invalid_run(
        self,
        run_directory: Path,
        manifest_path: Path,
        *,
        campaign_id: str | None = None,
        seed: int | None = None,
        job_status: RunJobStatus | None = None,
    ) -> DashboardRun:
        updated_at = self._updated_at(run_directory)
        return DashboardRun(
            run_ref=self._run_ref(run_directory),
            run_id=run_directory.name,
            campaign_id=campaign_id,
            seed=seed,
            scenario_id="unknown",
            model_ids=(),
            job_status=job_status.value if job_status is not None else None,
            status="invalid",
            phase="incompatible",
            day=0,
            total_days=1,
            progress=0.0,
            company_count=0,
            active_company_count=0,
            updated_at=updated_at,
            error="Run artifacts are incompatible with the current protocol or incomplete.",
            macro_index=1.0,
            total_score_cents=0,
            total_cash_cents=0,
            completed_windows=0,
            current_window=0,
            decision_status_counts={},
            companies=(),
            recent_events=(),
            artifacts={"manifest": manifest_path.name},
        )

    @staticmethod
    def _artifact_paths(run_directory: Path) -> tuple[Path, ...]:
        return tuple(
            run_directory / name
            for name in (
                "manifest.json",
                "checkpoint.json",
                "final_checkpoint.json",
                "summary.json",
                "trace.jsonl",
                "decisions.sqlite3",
                "decisions.sqlite3-wal",
                "decisions.sqlite3-shm",
                "performance_status.json",
                "performance.jsonl",
            )
        )

    def _signature(
        self,
        run_directory: Path,
        *,
        campaign_key: str | None = None,
        include_performance: bool = False,
    ) -> tuple[tuple[int, int], ...]:
        signature: list[tuple[int, int]] = [(int(self._is_locked(run_directory)), 0)]
        for path in self._artifact_paths(run_directory):
            if path.name.startswith("performance") and not include_performance:
                continue
            try:
                stat = self._safe_artifact(path).stat()
                signature.append((stat.st_mtime_ns, stat.st_size))
            except (OSError, ValueError):
                signature.append((0, 0))
        if campaign_key is not None:
            digest = hashlib.sha256(campaign_key.encode("utf-8")).hexdigest()
            signature.append((int(digest[:16], 16), int(digest[16:32], 16)))
        return tuple(signature)

    def _updated_at(self, run_directory: Path) -> datetime:
        timestamps: list[float] = []
        for path in self._artifact_paths(run_directory):
            try:
                timestamps.append(self._safe_artifact(path).stat().st_mtime)
            except (OSError, ValueError):
                continue
        return datetime.fromtimestamp(max(timestamps, default=0.0), tz=UTC)

    def _artifact_links(self, run_directory: Path) -> dict[str, str]:
        links: dict[str, str] = {}
        for path in self._artifact_paths(run_directory):
            try:
                self._safe_artifact(path)
            except (OSError, ValueError):
                continue
            if path.is_file():
                links[path.stem] = path.name
        agent_traces = run_directory / "agent-traces"
        if agent_traces.is_dir() and not self._is_link(agent_traces):
            links["agent_traces"] = agent_traces.name
        return links

    def _run_ref(self, run_directory: Path) -> str:
        relative = run_directory.resolve().relative_to(self.runs_root).as_posix()
        return hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]

    def _read_text(self, path: Path, *, max_bytes: int) -> str:
        safe_path = self._safe_artifact(path)
        if safe_path.stat().st_size > max_bytes:
            raise ValueError(f"artifact exceeds {max_bytes} byte dashboard limit")
        return safe_path.read_text(encoding="utf-8")

    def _safe_artifact(self, path: Path) -> Path:
        resolved = path.resolve()
        if (
            not resolved.is_relative_to(self.runs_root)
            or self._is_link(path)
            or not resolved.is_file()
        ):
            raise ValueError("dashboard artifact path is unsafe")
        return resolved

    @staticmethod
    def _is_link(path: Path) -> bool:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())

    @staticmethod
    def _path_has_link(path: Path, root: Path) -> bool:
        current = root
        for part in path.relative_to(root).parts:
            current /= part
            if RunDashboard._is_link(current):
                return True
        return False

    @staticmethod
    def _is_locked(run_directory: Path) -> bool:
        return RunDirectoryLock.is_active(run_directory / ".run.lock")
