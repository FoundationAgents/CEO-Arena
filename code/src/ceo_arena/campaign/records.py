"""Typed Campaign lifecycle records, independent of SQLite connections."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from ceo_arena.campaign.models import CampaignRun, campaign_task_key
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import StrictModel
from ceo_arena.runtime.persistence import RunCheckpoint, SealedDecision

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_SECRET_NAME = re.compile(r"(^|_)(api_?key|authorization|access_?token|secret)($|_)", re.I)


class CampaignStatus(StrEnum):
    PREPARED = "prepared"
    RUNNING = "running"
    INCOMPLETE = "incomplete"
    COMPLETED = "completed"


class RunJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"
    FAILED = "failed"
    COMPLETED = "completed"

    @property
    def is_terminal(self) -> bool:
        """Return whether automatic or explicit resume is forbidden."""

        return self in {RunJobStatus.FAILED, RunJobStatus.COMPLETED}

    @property
    def is_scheduler_runnable(self) -> bool:
        """Return whether the Campaign scheduler can automatically claim this state."""

        return self in {
            RunJobStatus.QUEUED,
            RunJobStatus.RUNNING,
            RunJobStatus.INTERRUPTED,
        }


class RunStopReason(StrEnum):
    USER_REQUESTED = "user_requested"
    PROVIDER_RETRY_EXHAUSTED = "provider_retry_exhausted"


class DecisionResponseStatus(StrEnum):
    SEALED = "sealed"
    APPLIED = "applied"


class FailurePhase(StrEnum):
    PROVIDER = "provider"
    PERSISTENCE = "persistence"
    RUNTIME = "runtime"
    REPLAY = "replay"
    CONFIGURATION = "configuration"
    UNKNOWN = "unknown"


class CampaignRecord(StrictModel):
    """Immutable Campaign identity plus its aggregate lifecycle."""

    campaign_id: str = Field(min_length=1, max_length=128)
    spec_hash: str = Field(pattern=_HASH_PATTERN)
    plan_hash: str = Field(pattern=_HASH_PATTERN)
    expected_run_count: int = Field(gt=0)
    status: CampaignStatus = CampaignStatus.PREPARED
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_timestamps(self) -> CampaignRecord:
        require_aware_timestamp(self.created_at)
        require_aware_timestamp(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("Campaign update cannot predate creation")
        return self


class RunJobSpec(StrictModel):
    """Secret-free immutable payload required to reconstruct one Run."""

    task_key: str = Field(min_length=1, max_length=128)
    campaign_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=256)
    lineup_id: str = Field(min_length=1)
    seed: int
    config_hash: str = Field(pattern=_HASH_PATTERN)
    run_payload_json: str = Field(min_length=2)

    @field_validator("run_payload_json")
    @classmethod
    def validate_payload(cls, payload: str) -> str:
        """Canonicalize a JSON object and reject secret-bearing fields."""

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("run payload must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("run payload must be a JSON object")
        _reject_secret_fields(parsed)
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @model_validator(mode="after")
    def validate_task_key(self) -> RunJobSpec:
        run = CampaignRun.model_validate_json(self.run_payload_json)
        expected = campaign_task_key(self.campaign_id, self.lineup_id, self.seed)
        if self.task_key != expected:
            raise ValueError("Campaign task key is not canonical")
        if (
            run.task_key != self.task_key
            or run.campaign_id != self.campaign_id
            or run.run_id != self.run_id
            or run.lineup_id != self.lineup_id
            or run.seed != self.seed
        ):
            raise ValueError("Run payload identity differs from its durable task")
        return self

    @property
    def run_payload(self) -> CampaignRun:
        """Return the strongly typed, secret-free execution payload."""

        return CampaignRun.model_validate_json(self.run_payload_json)

    @classmethod
    def create(
        cls,
        *,
        campaign_id: str,
        run_id: str,
        lineup_id: str,
        seed: int,
        config_hash: str,
        run_payload_json: str,
    ) -> Self:
        """Build a Run specification with its deterministic task key."""

        return cls(
            task_key=campaign_task_key(campaign_id, lineup_id, seed),
            campaign_id=campaign_id,
            run_id=run_id,
            lineup_id=lineup_id,
            seed=seed,
            config_hash=config_hash,
            run_payload_json=run_payload_json,
        )


class RunJobRecord(RunJobSpec):
    """One durable Run lifecycle record returned by the repository."""

    status: RunJobStatus = RunJobStatus.QUEUED
    revision: int = Field(default=0, ge=0)
    current_day: int = Field(default=0, ge=0)
    attempt: int = Field(default=0, ge=0)
    attempt_limit: int | None = Field(default=None, ge=1)
    pause_requested: bool = False
    stop_reason: RunStopReason | None = None
    retry_not_before: datetime | None = None
    lease_owner: str | None = Field(default=None, min_length=1, max_length=128)
    lease_until: datetime | None = None
    last_error: str | None = Field(default=None, max_length=2_000)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_lifecycle(self) -> RunJobRecord:
        require_aware_timestamp(self.created_at)
        require_aware_timestamp(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("Run update cannot predate creation")
        if self.status is RunJobStatus.RUNNING:
            if self.lease_owner is None or self.lease_until is None:
                raise ValueError("running Run requires a lease")
            require_aware_timestamp(self.lease_until)
            if self.attempt <= 0:
                raise ValueError("running Run requires an attempt")
        elif self.lease_owner is not None or self.lease_until is not None:
            raise ValueError("only a running Run may hold a lease")
        if self.pause_requested and self.status is not RunJobStatus.RUNNING:
            raise ValueError("only a running Run may have a pending pause request")
        if (self.status is RunJobStatus.STOPPED) != (self.stop_reason is not None):
            raise ValueError("only a stopped Run requires a typed stop reason")
        if self.retry_not_before is not None:
            require_aware_timestamp(self.retry_not_before)
            if self.status is not RunJobStatus.INTERRUPTED:
                raise ValueError("only an interrupted Run may have a retry deadline")
        if self.status is RunJobStatus.FAILED and self.last_error is None:
            raise ValueError("failed Run requires an error")
        if self.status is RunJobStatus.COMPLETED and self.last_error is not None:
            raise ValueError("completed Run cannot retain an error")
        return self


class DecisionResponseRecord(StrictModel):
    """One normalized provider response staged before economic application."""

    decision_id: str
    task_key: str
    window_index: int = Field(ge=0)
    company_id: str = Field(min_length=1)
    status: DecisionResponseStatus
    response_hash: str = Field(pattern=_HASH_PATTERN)
    response: SealedDecision
    staged_at: datetime
    applied_at: datetime | None = None

    @model_validator(mode="after")
    def validate_response(self) -> DecisionResponseRecord:
        if self.decision_id != campaign_decision_id(
            self.task_key, self.window_index, self.company_id
        ):
            raise ValueError("decision response id is not canonical")
        if self.response.company_id != self.company_id:
            raise ValueError("decision response company identity differs")
        if self.response_hash != canonical_model_hash(self.response):
            raise ValueError("decision response hash is invalid")
        require_aware_timestamp(self.staged_at)
        if self.status is DecisionResponseStatus.APPLIED:
            if self.applied_at is None:
                raise ValueError("applied response requires applied_at")
            require_aware_timestamp(self.applied_at)
        elif self.applied_at is not None:
            raise ValueError("sealed response cannot have applied_at")
        return self


class FailureHistoryRecord(StrictModel):
    """Append-only secret-redacted attempt failure."""

    sequence: int = Field(ge=1)
    task_key: str
    attempt: int = Field(ge=1)
    phase: FailurePhase
    error: str = Field(min_length=1, max_length=2_000)
    occurred_at: datetime


class RunRecoveryRecord(StrictModel):
    """Authoritative recovery frontier and any staged responses."""

    job: RunJobRecord
    checkpoint: RunCheckpoint | None
    decisions: tuple[DecisionResponseRecord, ...]


def campaign_decision_id(task_key: str, window_index: int, company_id: str) -> str:
    """Return the stable identity of one company response window."""

    identity = f"ceos-campaign-decision-v1\x1f{task_key}\x1f{window_index}\x1f{company_id}"
    return f"decision-{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _reject_secret_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_NAME.search(str(key).replace("-", "_")):
                raise ValueError("run payload cannot contain credentials")
            _reject_secret_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_fields(child)


def require_aware_timestamp(value: datetime) -> None:
    """Reject timezone-free lifecycle timestamps."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Campaign timestamps must be timezone-aware")
