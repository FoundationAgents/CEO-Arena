"""Deep local control module for launching and observing configured Campaigns."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import subprocess
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from ceo_arena.campaign.application import CampaignApplication
from ceo_arena.campaign.compiler import CampaignCompiler
from ceo_arena.campaign.models import (
    DEFAULT_CAMPAIGN_RUN_LIMIT,
    RULE_MODEL_ID,
    FrozenCampaignPlan,
)
from ceo_arena.campaign.records import (
    CampaignStatus,
    RunJobStatus,
)
from ceo_arena.campaign.repository import (
    RepositoryConflictError,
)
from ceo_arena.campaign.spec import ExperimentSpec, load_experiment_spec
from ceo_arena.dashboard.campaign import CampaignReadStore, CampaignView
from ceo_arena.dashboard.process import (
    CampaignProcessHandle,
    CampaignProcessPort,
    CampaignProcessRequest,
    DetachedProcessAdapter,
)
from ceo_arena.models import StrictModel
from ceo_arena.provenance import simulation_fingerprints
from ceo_arena.runtime._atomic import AtomicModelFile
from ceo_arena.runtime.lock import RunDirectoryLock

_MAX_CONFIG_BYTES = 1_000_000
_MAX_CONFIG_FILES = 100
_MAX_IDEMPOTENCY_RECORDS = 256
_REF_PATTERN = r"^[0-9a-f]{32}$"
_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


class ExperimentStatus(StrEnum):
    """Browser-safe aggregate lifecycle for one configured experiment."""

    READY = "ready"
    STARTING = "starting"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    COMPLETED = "completed"
    CONFLICT = "conflict"
    INVALID = "invalid"


class ExperimentAction(StrEnum):
    """Single button action allowed by the current durable state."""

    RUN = "run"
    RESUME = "resume"
    NONE = "none"


class StartDisposition(StrEnum):
    """Idempotent result of one start request."""

    STARTED = "started"
    RESUMED = "resumed"
    ALREADY_RUNNING = "already_running"
    ALREADY_COMPLETED = "already_completed"


class CampaignControlErrorCode(StrEnum):
    """Stable public error codes that never contain local exception details."""

    NOT_FOUND = "not_found"
    INVALID_CONFIG = "invalid_config"
    CONFIG_CONFLICT = "config_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CAPACITY_REACHED = "capacity_reached"
    NOT_STARTABLE = "not_startable"
    LAUNCH_FAILED = "launch_failed"


class CampaignControlError(RuntimeError):
    """Typed, sanitized failure returned by the dashboard control seam."""

    def __init__(
        self,
        code: CampaignControlErrorCode,
        public_message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.retryable = retryable


class ExperimentSummary(StrictModel):
    """Secret-free merge of one TOML definition and its durable Runs."""

    experiment_ref: str = Field(pattern=_REF_PATTERN)
    campaign_id: str
    model_ids: tuple[str, ...]
    status: ExperimentStatus
    action: ExperimentAction
    can_start: bool
    expected_runs: int = Field(ge=0)
    completed_runs: int = Field(ge=0)
    total_days: int = Field(ge=0)
    seed_count: int = Field(ge=0)
    company_count: int = Field(ge=0)
    status_counts: dict[str, int]
    message: str


class ExperimentCatalog(StrictModel):
    """Complete browser-facing experiment selector state."""

    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiments: tuple[ExperimentSummary, ...]


class StartReceipt(StrictModel):
    """Safe acknowledgement returned before a long Campaign finishes."""

    experiment_ref: str = Field(pattern=_REF_PATTERN)
    campaign_id: str
    disposition: StartDisposition
    status: ExperimentStatus
    message: str


class _StoredReceipt(StrictModel):
    """Persistent browser idempotency receipt containing no local path or secret."""

    idempotency_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment_ref: str = Field(pattern=_REF_PATTERN)
    receipt: StartReceipt


@dataclass(frozen=True, slots=True)
class _ValidConfig:
    path: Path
    spec: ExperimentSpec
    plan: FrozenCampaignPlan
    experiment_ref: str


@dataclass(frozen=True, slots=True)
class _InvalidConfig:
    experiment_ref: str


@dataclass(frozen=True, slots=True)
class _LoadedCampaign:
    store: CampaignReadStore
    view: CampaignView
    status: CampaignStatus


type _ConfigEntry = _ValidConfig | _InvalidConfig


class CampaignControl:
    """Hide config discovery, idempotence, lifecycle truth, and worker launch."""

    def __init__(
        self,
        config_root: Path,
        runs_root: Path,
        *,
        process: CampaignProcessPort | None = None,
        max_active_campaigns: int = 1,
    ) -> None:
        if max_active_campaigns < 1:
            raise ValueError("max_active_campaigns must be positive")
        self.config_root = _prepare_config_root(config_root)
        self.runs_root = runs_root.resolve()
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._receipt_root = _prepare_private_directory(
            self.runs_root / ".control-receipts",
            self.runs_root,
        )
        self._process = process or DetachedProcessAdapter()
        self._max_active_campaigns = max_active_campaigns
        self._simulation_fingerprints = simulation_fingerprints()
        self._handles: dict[Path, CampaignProcessHandle] = {}
        self._launch_failures: dict[Path, str] = {}
        self._receipts: OrderedDict[str, tuple[str, StartReceipt]] = OrderedDict()
        self._lock = threading.RLock()

    def snapshot(self) -> ExperimentCatalog:
        """Return a secret-free union of local configs and durable Campaign Runs."""

        with self._lock:
            experiments = self._summaries()
            payload = json.dumps(
                [item.model_dump(mode="json") for item in experiments],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            revision = hashlib.sha256(
                f"{self._revision_locked()}\x1f{payload}".encode()
            ).hexdigest()
            return ExperimentCatalog(revision=revision, experiments=experiments)

    def start(self, experiment_ref: str, idempotency_key: str) -> StartReceipt:
        """Prepare and idempotently launch one configured Campaign worker."""

        _validate_ref(experiment_ref)
        _validate_idempotency_key(idempotency_key)
        with self._lock:
            cached = self._receipt_for(idempotency_key)
            if cached is not None:
                cached_ref, receipt = cached
                if cached_ref != experiment_ref:
                    raise CampaignControlError(
                        CampaignControlErrorCode.IDEMPOTENCY_CONFLICT,
                        "This start request belongs to another experiment.",
                    )
                return receipt

            config = self._config_for_start(experiment_ref)
            reloaded = self._load_config(config.path)
            if not isinstance(reloaded, _ValidConfig) or reloaded.experiment_ref != experiment_ref:
                raise CampaignControlError(
                    CampaignControlErrorCode.CONFIG_CONFLICT,
                    "Configuration changed before launch. Select the experiment again.",
                )
            if len(reloaded.plan.runs) > DEFAULT_CAMPAIGN_RUN_LIMIT:
                raise CampaignControlError(
                    CampaignControlErrorCode.NOT_STARTABLE,
                    "This experiment exceeds the 500-run dashboard limit. Use the CLI.",
                )
            output = self._output_for(reloaded.plan.campaign_id)
            application = CampaignApplication(reloaded.spec, reloaded.plan, output)
            try:
                application.prepare()
            except RepositoryConflictError as error:
                raise CampaignControlError(
                    CampaignControlErrorCode.CONFIG_CONFLICT,
                    "This campaign ID belongs to a different frozen configuration. Use a new ID.",
                ) from error
            except (OSError, sqlite3.Error, ValueError) as error:
                raise CampaignControlError(
                    CampaignControlErrorCode.INVALID_CONFIG,
                    "Configuration could not be prepared safely. Check the local TOML.",
                ) from error

            summary = self._summary_by_ref(experiment_ref)
            if summary.status is ExperimentStatus.COMPLETED:
                return self._remember(
                    idempotency_key,
                    StartReceipt(
                        experiment_ref=experiment_ref,
                        campaign_id=summary.campaign_id,
                        disposition=StartDisposition.ALREADY_COMPLETED,
                        status=summary.status,
                        message="Experiment already complete; it will not be started again.",
                    ),
                )
            if summary.status in {ExperimentStatus.STARTING, ExperimentStatus.RUNNING}:
                return self._remember(
                    idempotency_key,
                    StartReceipt(
                        experiment_ref=experiment_ref,
                        campaign_id=summary.campaign_id,
                        disposition=StartDisposition.ALREADY_RUNNING,
                        status=summary.status,
                        message="Experiment already running in the background.",
                    ),
                )
            if not summary.can_start:
                raise CampaignControlError(
                    CampaignControlErrorCode.NOT_STARTABLE,
                    summary.message,
                )
            active_campaigns = sum(
                item.status in {ExperimentStatus.STARTING, ExperimentStatus.RUNNING}
                for item in self._summaries()
                if item.experiment_ref != experiment_ref
            )
            if active_campaigns >= self._max_active_campaigns:
                raise CampaignControlError(
                    CampaignControlErrorCode.CAPACITY_REACHED,
                    "Campaign concurrency limit reached. Wait for an active campaign to finish.",
                    retryable=True,
                )

            mode = "resume" if summary.action is ExperimentAction.RESUME else "run"
            self._launch_failures.pop(output, None)
            try:
                handle = self._process.start(
                    CampaignProcessRequest(
                        campaign_id=summary.campaign_id,
                        mode=mode,
                        spec_path=reloaded.path,
                        output=output,
                    )
                )
                if not handle.is_running():
                    raise RuntimeError("Campaign worker exited during launch")
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                raise CampaignControlError(
                    CampaignControlErrorCode.LAUNCH_FAILED,
                    "The experiment process could not start. Retry later.",
                    retryable=True,
                ) from error
            self._handles[output] = handle
            disposition = (
                StartDisposition.RESUMED
                if summary.status is ExperimentStatus.INTERRUPTED
                else StartDisposition.STARTED
            )
            return self._remember(
                idempotency_key,
                StartReceipt(
                    experiment_ref=experiment_ref,
                    campaign_id=summary.campaign_id,
                    disposition=disposition,
                    status=ExperimentStatus.STARTING,
                    message="Experiment dispatched. Progress will update live.",
                ),
            )

    def ensure_campaign_running(
        self,
        campaign_id: str,
        idempotency_key: str,
    ) -> StartReceipt:
        """Launch or observe one configured Campaign selected through a Run."""

        experiment_ref = self.require_configured_campaign(campaign_id)
        return self.start(experiment_ref, idempotency_key)

    def require_configured_campaign(self, campaign_id: str) -> str:
        """Return the opaque experiment reference for one unique local config."""

        with self._lock:
            matches = [
                entry
                for entry in self._scan_configs()
                if isinstance(entry, _ValidConfig) and entry.plan.campaign_id == campaign_id
            ]
            if len(matches) != 1:
                raise CampaignControlError(
                    CampaignControlErrorCode.CONFIG_CONFLICT,
                    "This run has no unique local campaign configuration.",
                )
            return matches[0].experiment_ref

    def revision(self) -> str:
        """Return an opaque digest over configs, durable Runs, locks, and handles."""

        with self._lock:
            return self._revision_locked()

    def _revision_locked(self) -> str:
        """Hash cheap file, SQLite, lease, and process signatures."""

        parts: list[str] = []
        for path in sorted(self.config_root.glob("*.toml"))[:_MAX_CONFIG_FILES]:
            try:
                metadata = path.lstat()
                parts.append(f"config:{path.name}:{metadata.st_mtime_ns}:{metadata.st_size}")
            except OSError:
                parts.append(f"config:{path.name}:unavailable")
        for store in CampaignReadStore.discover_all(self.runs_root):
            parts.append(f"campaign:{store.revision_signature()!r}:{store.lease_signature()}")
        for output in tuple(sorted(self._handles)):
            parts.append(f"handle:{output.name}:{self._handle_running(output)}")
        for output, message in sorted(self._launch_failures.items()):
            parts.append(f"failure:{output.name}:{message}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _summaries(self) -> tuple[ExperimentSummary, ...]:
        entries = self._scan_configs()
        loaded = self._load_campaigns()
        summaries: list[ExperimentSummary] = []
        consumed_roots: set[Path] = set()

        valid = [entry for entry in entries if isinstance(entry, _ValidConfig)]
        by_campaign: dict[str, list[_ValidConfig]] = {}
        for entry in valid:
            by_campaign.setdefault(entry.plan.campaign_id, []).append(entry)
        for campaign_id, configs in by_campaign.items():
            matching = tuple(item for item in loaded if item.view.plan.campaign_id == campaign_id)
            if len(configs) != 1 or len(matching) > 1:
                reference = _opaque_ref("duplicate", campaign_id)
                summaries.append(
                    _unavailable_summary(
                        reference,
                        campaign_id,
                        ExperimentStatus.CONFLICT,
                        "Duplicate campaign ID definitions; launch target is ambiguous.",
                    )
                )
                consumed_roots.update(item.store.root for item in matching)
                continue
            config = configs[0]
            output = self._output_for(config.plan.campaign_id)
            campaign = next((item for item in matching if item.store.root == output), None)
            unexpected = tuple(item for item in matching if item.store.root != output)
            if unexpected:
                summaries.append(self._summary(config, campaign, conflict=True))
                consumed_roots.update(item.store.root for item in matching)
                continue
            if campaign is not None:
                consumed_roots.add(campaign.store.root)
            summaries.append(self._summary(config, campaign))

        summaries.extend(
            _unavailable_summary(
                entry.experiment_ref,
                f"invalid-{entry.experiment_ref[:8]}",
                ExperimentStatus.INVALID,
                "Invalid or unsafe TOML. Check the local configuration.",
            )
            for entry in entries
            if isinstance(entry, _InvalidConfig)
        )
        for campaign in loaded:
            if campaign.store.root in consumed_roots:
                continue
            summaries.append(self._summary(None, campaign))
        priority = {
            ExperimentStatus.RUNNING: 0,
            ExperimentStatus.STARTING: 1,
            ExperimentStatus.INTERRUPTED: 2,
            ExperimentStatus.READY: 3,
            ExperimentStatus.FAILED: 4,
            ExperimentStatus.COMPLETED: 5,
            ExperimentStatus.CONFLICT: 6,
            ExperimentStatus.INVALID: 7,
        }
        summaries.sort(key=lambda item: (priority[item.status], item.campaign_id))
        return tuple(summaries)

    def _summary(
        self,
        config: _ValidConfig | None,
        campaign: _LoadedCampaign | None,
        *,
        conflict: bool = False,
    ) -> ExperimentSummary:
        if config is None and campaign is None:
            raise ValueError("an experiment summary requires a config or Campaign")
        plan = config.plan if config is not None else campaign.view.plan
        reference = config.experiment_ref if config is not None else _experiment_ref(plan)
        output = self._output_for(plan.campaign_id) if config is not None else campaign.store.root
        view = campaign.view if campaign is not None else None
        if campaign is not None and campaign.view.plan.plan_hash != plan.plan_hash:
            conflict = True
        if campaign is None and _has_output_artifacts(output):
            conflict = True

        counts = Counter(run.status.value for run in view.runs) if view is not None else Counter()
        completed = counts[RunJobStatus.COMPLETED.value]
        status, action, can_start, message = self._lifecycle(
            reference,
            output,
            view,
            campaign.status if campaign is not None else None,
            conflict=conflict,
            config_available=config is not None,
        )
        if config is not None and len(plan.runs) > DEFAULT_CAMPAIGN_RUN_LIMIT:
            status = ExperimentStatus.INVALID
            action = ExperimentAction.NONE
            can_start = False
            message = "This experiment exceeds the 500-run dashboard limit. Use the CLI."
        return ExperimentSummary(
            experiment_ref=reference,
            campaign_id=plan.campaign_id,
            model_ids=(*plan.model_order, RULE_MODEL_ID),
            status=status,
            action=action,
            can_start=can_start,
            expected_runs=len(plan.runs),
            completed_runs=completed,
            total_days=plan.days,
            seed_count=len(plan.seeds),
            company_count=plan.company_count,
            status_counts=dict(sorted(counts.items())),
            message=message,
        )

    def _lifecycle(
        self,
        experiment_ref: str,
        output: Path,
        view: CampaignView | None,
        campaign_status: CampaignStatus | None,
        *,
        conflict: bool,
        config_available: bool,
    ) -> tuple[ExperimentStatus, ExperimentAction, bool, str]:
        if conflict:
            return (
                ExperimentStatus.CONFLICT,
                ExperimentAction.NONE,
                False,
                "Local settings differ from the existing campaign. Use a new campaign ID.",
            )
        counts = Counter(run.status for run in view.runs) if view is not None else Counter()
        expected = len(view.runs) if view is not None else 0
        completed = counts[RunJobStatus.COMPLETED]
        if campaign_status is CampaignStatus.COMPLETED and completed == expected and expected:
            return (
                ExperimentStatus.COMPLETED,
                ExperimentAction.NONE,
                False,
                "ExperimentCompleted.",
            )
        if RunDirectoryLock.is_active(output / ".campaign.lock"):
            return (
                ExperimentStatus.RUNNING,
                ExperimentAction.NONE,
                False,
                "Experiment is running in the background.",
            )
        if self._handle_running(output):
            return (
                ExperimentStatus.STARTING,
                ExperimentAction.NONE,
                False,
                "Background process is acquiring the campaign lease.",
            )
        launch_failure = self._launch_failures.get(output)
        if launch_failure is not None:
            return (
                ExperimentStatus.INTERRUPTED,
                ExperimentAction.RESUME
                if counts[RunJobStatus.STOPPED] > 0
                else ExperimentAction.RUN,
                config_available,
                launch_failure,
            )
        if not config_available:
            return (
                ExperimentStatus.INTERRUPTED,
                ExperimentAction.NONE,
                False,
                "Local TOML missing. Existing runs are read-only.",
            )
        if view is None:
            return (
                ExperimentStatus.READY,
                ExperimentAction.RUN,
                True,
                "Configuration ready. Start the experiment.",
            )
        if completed == expected and expected:
            if campaign_status in {
                CampaignStatus.PREPARED,
                CampaignStatus.RUNNING,
                CampaignStatus.INCOMPLETE,
            }:
                return (
                    ExperimentStatus.INTERRUPTED,
                    ExperimentAction.RUN,
                    True,
                    "All runs finished. Resume to complete final validation and publish results.",
                )
            return (
                ExperimentStatus.FAILED,
                ExperimentAction.NONE,
                False,
                "Final validation failed. Check local logs and use a new campaign ID.",
            )
        nonterminal = sum(
            counts[status]
            for status in (
                RunJobStatus.QUEUED,
                RunJobStatus.RUNNING,
                RunJobStatus.INTERRUPTED,
                RunJobStatus.STOPPED,
            )
        )
        if nonterminal:
            stopped = counts[RunJobStatus.STOPPED] > 0
            progressed = stopped or any(run.current_day > 0 for run in view.runs)
            launched_before = self._has_launch_receipt(experiment_ref)
            if not progressed and launched_before:
                return (
                    ExperimentStatus.INTERRUPTED,
                    ExperimentAction.RUN,
                    True,
                    "The previous runner did not start successfully; preflight may have failed.",
                )
            return (
                ExperimentStatus.INTERRUPTED if progressed else ExperimentStatus.READY,
                ExperimentAction.RESUME if stopped else ExperimentAction.RUN,
                True,
                "Experiment can resume from saved progress."
                if progressed
                else "Experiment prepared and ready to run.",
            )
        if counts[RunJobStatus.FAILED] > 0:
            return (
                ExperimentStatus.FAILED,
                ExperimentAction.NONE,
                False,
                "Experiment terminated or failed final validation. Check local logs first.",
            )
        return (
            ExperimentStatus.INTERRUPTED,
            ExperimentAction.RUN,
            True,
            "Incomplete experiment state. Resume to attempt recovery.",
        )

    def _scan_configs(self) -> tuple[_ConfigEntry, ...]:
        entries: list[_ConfigEntry] = []
        paths = sorted(self.config_root.glob("*.toml"))
        if len(paths) > _MAX_CONFIG_FILES:
            entries.append(_InvalidConfig(_opaque_ref("too-many-configs", str(len(paths)))))
            paths = paths[:_MAX_CONFIG_FILES]
        for path in paths:
            entries.append(self._load_config(path))
        return tuple(entries)

    def _load_config(self, path: Path) -> _ConfigEntry:
        try:
            metadata = path.lstat()
            if (
                path.parent != self.config_root
                or path.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_FLAG)
                or metadata.st_size <= 0
                or metadata.st_size > _MAX_CONFIG_BYTES
                or path.resolve().parent != self.config_root
            ):
                raise ValueError("unsafe Campaign config")
            content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            spec = load_experiment_spec(path)
            plan = CampaignCompiler().compile(
                spec,
                simulation_fingerprints=self._simulation_fingerprints,
            )
            return _ValidConfig(path.resolve(), spec, plan, _experiment_ref(plan))
        except (OSError, UnicodeError, ValueError):
            marker = locals().get("content_hash", _opaque_ref("invalid-path", path.name))
            return _InvalidConfig(_opaque_ref("invalid", str(marker)))

    def _load_campaigns(self) -> tuple[_LoadedCampaign, ...]:
        loaded: list[_LoadedCampaign] = []
        for store in CampaignReadStore.discover_all(self.runs_root):
            try:
                view = store.load(include_details=False)
                status = _read_campaign_status(store, view.plan.campaign_id)
                loaded.append(_LoadedCampaign(store, view, status))
            except (OSError, sqlite3.Error, ValueError):
                continue
        return tuple(loaded)

    def _handle_running(self, output: Path) -> bool:
        handle = self._handles.get(output)
        if handle is None:
            return False
        try:
            running = handle.is_running()
        except (OSError, RuntimeError):
            running = False
        if not running:
            try:
                exit_code = handle.exit_code()
            except (OSError, RuntimeError):
                exit_code = None
            self._handles.pop(output, None)
            if exit_code not in (None, 0):
                self._launch_failures[output] = (
                    "Runner preflight or startup failed. Check private local logs before retrying."
                )
        return running

    def _config_for_start(self, experiment_ref: str) -> _ValidConfig:
        entries = self._scan_configs()
        matches = [
            entry
            for entry in entries
            if isinstance(entry, _ValidConfig) and entry.experiment_ref == experiment_ref
        ]
        if not matches:
            invalid = any(
                isinstance(entry, _InvalidConfig) and entry.experiment_ref == experiment_ref
                for entry in entries
            )
            raise CampaignControlError(
                CampaignControlErrorCode.INVALID_CONFIG
                if invalid
                else CampaignControlErrorCode.NOT_FOUND,
                "Invalid experiment configuration."
                if invalid
                else "Experiment configuration not found. Reload the list.",
            )
        campaign_count = sum(
            isinstance(entry, _ValidConfig)
            and entry.plan.campaign_id == matches[0].plan.campaign_id
            for entry in entries
        )
        if len(matches) != 1 or campaign_count != 1:
            raise CampaignControlError(
                CampaignControlErrorCode.CONFIG_CONFLICT,
                "Duplicate campaign ID definitions.",
            )
        return matches[0]

    def _summary_by_ref(self, experiment_ref: str) -> ExperimentSummary:
        try:
            return next(item for item in self._summaries() if item.experiment_ref == experiment_ref)
        except StopIteration as error:
            raise CampaignControlError(
                CampaignControlErrorCode.NOT_FOUND,
                "Experiment state unavailable. Reload the list.",
            ) from error

    def _output_for(self, campaign_id: str) -> Path:
        candidate = self.runs_root / campaign_id
        if candidate.exists():
            metadata = candidate.lstat()
            if (
                candidate.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_FLAG)
            ):
                raise CampaignControlError(
                    CampaignControlErrorCode.INVALID_CONFIG,
                    "Campaign output is not a safe local directory.",
                )
        output = candidate.resolve()
        if output.parent != self.runs_root:
            raise CampaignControlError(
                CampaignControlErrorCode.INVALID_CONFIG,
                "Campaign ID cannot map to a safe output directory.",
            )
        return output

    def _remember(self, key: str, receipt: StartReceipt) -> StartReceipt:
        self._receipts[key] = (receipt.experiment_ref, receipt)
        self._receipts.move_to_end(key)
        while len(self._receipts) > _MAX_IDEMPOTENCY_RECORDS:
            self._receipts.popitem(last=False)
        stored = _StoredReceipt(
            idempotency_hash=hashlib.sha256(key.encode()).hexdigest(),
            experiment_ref=receipt.experiment_ref,
            receipt=receipt,
        )
        file = AtomicModelFile(self._receipt_path(key), _StoredReceipt)
        try:
            existing = file.load()
            if existing is None:
                file.save(stored)
            elif existing != stored:
                raise CampaignControlError(
                    CampaignControlErrorCode.IDEMPOTENCY_CONFLICT,
                    "This start request already has a different result.",
                )
        except OSError:
            pass
        return receipt

    def _receipt_for(self, key: str) -> tuple[str, StartReceipt] | None:
        cached = self._receipts.get(key)
        if cached is not None:
            return cached
        try:
            stored = AtomicModelFile(
                self._receipt_path(key),
                _StoredReceipt,
            ).load()
        except (OSError, ValueError):
            return None
        if stored is None:
            return None
        expected_hash = hashlib.sha256(key.encode()).hexdigest()
        if stored.idempotency_hash != expected_hash:
            return None
        cached = (stored.experiment_ref, stored.receipt)
        self._receipts[key] = cached
        return cached

    def _receipt_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self._receipt_root / f"{digest}.json"

    def _has_launch_receipt(self, experiment_ref: str) -> bool:
        if any(reference == experiment_ref for reference, _receipt in self._receipts.values()):
            return True
        paths = tuple(self._receipt_root.glob("[0-9a-f]*.json"))[:1_024]
        for path in paths:
            if path.is_symlink():
                continue
            try:
                stored = AtomicModelFile(path, _StoredReceipt).load()
            except (OSError, ValueError):
                continue
            if stored is not None and stored.experiment_ref == experiment_ref:
                return True
        return False


def _prepare_config_root(path: Path) -> Path:
    absolute = path.absolute()
    absolute.mkdir(parents=True, exist_ok=True)
    metadata = absolute.lstat()
    if (
        absolute.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_FLAG)
    ):
        raise ValueError("Campaign config root must be a regular local directory")
    return absolute.resolve()


def _prepare_private_directory(path: Path, root: Path) -> Path:
    """Create one non-linked private metadata directory below a fixed root."""

    safe_root = root.resolve()
    candidate = path.absolute()
    candidate.mkdir(parents=True, exist_ok=True)
    resolved = candidate.resolve()
    metadata = candidate.lstat()
    if (
        resolved.parent != safe_root
        or candidate.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_FLAG)
    ):
        raise ValueError("Campaign control metadata directory is unsafe")
    return resolved


def _read_campaign_status(store: CampaignReadStore, campaign_id: str) -> CampaignStatus:
    uri = f"{store.database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute(
            "SELECT status FROM campaigns WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
    if row is None:
        raise ValueError("Campaign lifecycle row is unavailable")
    return CampaignStatus(str(row[0]))


def _experiment_ref(plan: FrozenCampaignPlan) -> str:
    return _opaque_ref("campaign", plan.campaign_id, plan.plan_hash)


def _opaque_ref(*parts: str) -> str:
    encoded = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def _validate_ref(value: str) -> None:
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise CampaignControlError(
            CampaignControlErrorCode.NOT_FOUND,
            "Invalid experiment reference.",
        )


def _validate_idempotency_key(value: str) -> None:
    if not 8 <= len(value) <= 128 or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise CampaignControlError(
            CampaignControlErrorCode.IDEMPOTENCY_CONFLICT,
            "Invalid start request ID.",
        )


def _has_output_artifacts(output: Path) -> bool:
    try:
        return output.is_dir() and next(output.iterdir(), None) is not None
    except OSError:
        return True


def _unavailable_summary(
    experiment_ref: str,
    campaign_id: str,
    status: ExperimentStatus,
    message: str,
) -> ExperimentSummary:
    return ExperimentSummary(
        experiment_ref=experiment_ref,
        campaign_id=campaign_id,
        model_ids=(),
        status=status,
        action=ExperimentAction.NONE,
        can_start=False,
        expected_runs=0,
        completed_runs=0,
        total_days=0,
        seed_count=0,
        company_count=0,
        status_counts={},
        message=message,
    )


__all__ = [
    "CampaignControl",
    "CampaignControlError",
    "CampaignControlErrorCode",
    "ExperimentAction",
    "ExperimentCatalog",
    "ExperimentStatus",
    "ExperimentSummary",
    "StartDisposition",
    "StartReceipt",
]
