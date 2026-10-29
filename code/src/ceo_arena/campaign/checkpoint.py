"""Runtime CheckpointStore adapter backed by one leased Campaign Run."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ceo_arena.campaign.records import (
    DecisionResponseStatus,
    RunJobStatus,
    RunRecoveryRecord,
)
from ceo_arena.campaign.repository import (
    CampaignRepository,
    LeaseLostError,
    RepositoryConflictError,
)
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import CompanyStatus
from ceo_arena.runtime.persistence import (
    CheckpointPhase,
    CollectingWindow,
    RunCheckpoint,
    SealedDecision,
)


class CampaignCheckpointStore:
    """Bridge the runtime's tiny store Interface to a revisioned Campaign lease."""

    def __init__(
        self,
        repository: CampaignRepository,
        *,
        task_key: str,
        lease_owner: str,
        revision: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not lease_owner:
            raise ValueError("lease_owner must not be empty")
        if revision < 0:
            raise ValueError("revision must be non-negative")
        self._repository = repository
        self.task_key = task_key
        self.lease_owner = lease_owner
        self.revision = revision
        self._clock = clock or _utc_now

    def load(self, *, run_id: str, config_hash: str) -> RunCheckpoint | None:
        """Load and merge any response staged just ahead of the checkpoint."""

        recovery = self._repository.load_recovery(self.task_key)
        if recovery is None:
            raise KeyError(f"unknown Campaign Run: {self.task_key}")
        job = recovery.job
        if (
            job.status is not RunJobStatus.RUNNING
            or job.lease_owner != self.lease_owner
            or job.lease_until is None
            or job.lease_until <= self._clock()
        ):
            raise LeaseLostError("Campaign CheckpointStore no longer owns the Run")
        if job.run_id != run_id or job.config_hash != config_hash:
            raise RepositoryConflictError("runtime checkpoint identity differs from Campaign task")
        self.revision = job.revision
        if recovery.checkpoint is None:
            if recovery.decisions:
                raise RepositoryConflictError("staged responses exist without a checkpoint")
            return None
        return _merge_staged_responses(recovery.checkpoint, recovery)

    def save(self, checkpoint: RunCheckpoint) -> None:
        """Save one runtime phase and advance this Adapter's CAS revision."""

        job = self._repository.save_checkpoint(
            self.task_key,
            lease_owner=self.lease_owner,
            expected_revision=self.revision,
            checkpoint=checkpoint,
            now=self._clock(),
        )
        self.revision = job.revision

    def stage_decision(
        self,
        *,
        run_id: str,
        config_hash: str,
        window: CollectingWindow,
        response: SealedDecision,
    ) -> None:
        """Persist a received response before the coordinator observes completion."""

        job = self._repository.stage_decision(
            self.task_key,
            lease_owner=self.lease_owner,
            expected_revision=self.revision,
            window_index=window.window_index,
            response=response,
            window=window,
            run_id=run_id,
            config_hash=config_hash,
            now=self._clock(),
        )
        self.revision = job.revision

    def renew_lease(self, lease_seconds: float) -> bool:
        """Extend the lease and report an orthogonal user pause request."""

        job = self._repository.renew_lease(
            self.task_key,
            lease_owner=self.lease_owner,
            expected_revision=self.revision,
            lease_seconds=lease_seconds,
            now=self._clock(),
        )
        self.revision = job.revision
        return job.pause_requested


def _merge_staged_responses(
    checkpoint: RunCheckpoint,
    recovery: RunRecoveryRecord,
) -> RunCheckpoint:
    """Make durable provider responses visible to runtime resume without another call."""

    staged = tuple(
        item for item in recovery.decisions if item.status is DecisionResponseStatus.SEALED
    )
    if not staged:
        return checkpoint
    if any(item.window_index != checkpoint.next_window_index for item in staged):
        raise RepositoryConflictError("sealed response lies outside the active checkpoint window")
    run = recovery.job.run_payload
    active_ids = tuple(
        company_id
        for company_id in run.company_ids
        if checkpoint.current_state.companies[company_id].status is CompanyStatus.ACTIVE
    )
    staged_by_company = {item.company_id: item.response for item in staged}
    if not set(staged_by_company) <= set(active_ids):
        raise RepositoryConflictError("staged response belongs to an inactive company")
    if checkpoint.pending_window is not None:
        pending = {item.company_id: item for item in checkpoint.pending_window.decisions}
        if pending != staged_by_company:
            raise RepositoryConflictError("sealed checkpoint differs from staged responses")
        return checkpoint
    collecting = checkpoint.collecting_window
    embedded = (
        {item.company_id: item for item in collecting.decisions} if collecting is not None else {}
    )
    if any(
        company_id in embedded and embedded[company_id] != response
        for company_id, response in staged_by_company.items()
    ):
        raise RepositoryConflictError("collecting checkpoint differs from staged response")
    merged = {**staged_by_company, **embedded}
    decisions = tuple(merged[company_id] for company_id in active_ids if company_id in merged)
    collecting = CollectingWindow(
        window_index=checkpoint.next_window_index,
        start_day=checkpoint.current_state.day,
        start_world_version=checkpoint.current_state.version,
        start_state_hash=canonical_model_hash(checkpoint.current_state),
        expected_company_ids=active_ids,
        decisions=decisions,
    )
    return checkpoint.model_copy(
        update={
            "phase": CheckpointPhase.COLLECTING_DECISIONS,
            "collecting_window": collecting,
        }
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
