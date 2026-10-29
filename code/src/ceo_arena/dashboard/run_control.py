"""CAS-protected control of one opaque Campaign Run from the local dashboard."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum

from ceo_arena.campaign.records import (
    RunJobRecord,
    RunJobStatus,
)
from ceo_arena.campaign.repository import (
    CampaignRepository,
    InvalidTransitionError,
    RevisionConflictError,
)
from ceo_arena.dashboard.catalog import DashboardRunControlTarget, RunDashboard
from ceo_arena.dashboard.control import CampaignControl, CampaignControlError
from ceo_arena.dashboard.models import RunControlReceipt, RunControlRequest

_MAX_IDEMPOTENCY_RECORDS = 256


class RunControlErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    NOT_CONFIGURED = "not_configured"
    ACTION_CONFLICT = "action_conflict"
    REVISION_CONFLICT = "revision_conflict"
    LAUNCH_FAILED = "launch_failed"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class RunControlError(RuntimeError):
    """Typed safe error projected at the HTTP seam."""

    def __init__(
        self,
        code: RunControlErrorCode,
        public_message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class _PendingResume:
    run_ref: str
    request: RunControlRequest
    campaign_id: str
    revision: int


class RunControl:
    """Hide opaque target resolution, repository CAS, launch, and idempotence."""

    def __init__(self, dashboard: RunDashboard, campaign_control: CampaignControl) -> None:
        self._dashboard = dashboard
        self._campaign_control = campaign_control
        self._receipts: OrderedDict[str, tuple[str, RunControlRequest, RunControlReceipt]] = (
            OrderedDict()
        )
        self._pending: OrderedDict[str, _PendingResume] = OrderedDict()
        self._lock = threading.RLock()

    def control(
        self,
        run_ref: str,
        request: RunControlRequest,
        idempotency_key: str,
    ) -> RunControlReceipt:
        """Apply exactly one explicit action to one revision of a Campaign Run."""

        with self._lock:
            cached = self._receipts.get(idempotency_key)
            if cached is not None:
                cached_ref, cached_request, receipt = cached
                self._require_same_request(run_ref, request, cached_ref, cached_request)
                return receipt
            pending = self._pending.get(idempotency_key)
            if pending is not None:
                self._require_same_request(run_ref, request, pending.run_ref, pending.request)
                return self._finish_resume(idempotency_key, pending)

            target = self._target(run_ref)
            if target.revision != request.revision:
                raise RunControlError(
                    RunControlErrorCode.REVISION_CONFLICT,
                    "Run state changed. Refresh and retry.",
                )
            if target.action != request.action:
                raise RunControlError(
                    RunControlErrorCode.ACTION_CONFLICT,
                    "This operation is unavailable in the current run state.",
                )
            repository = CampaignRepository(target.database)
            try:
                if request.action == "pause":
                    job = repository.request_pause(
                        target.task_key,
                        expected_revision=request.revision,
                    )
                    return self._remember(
                        idempotency_key,
                        run_ref,
                        request,
                        self._pause_receipt(run_ref, job),
                    )
                try:
                    self._campaign_control.require_configured_campaign(target.campaign_id)
                except CampaignControlError as error:
                    raise RunControlError(
                        RunControlErrorCode.NOT_CONFIGURED,
                        "No local campaign configuration is available for this run.",
                    ) from error
                job = repository.resume_recoverable(
                    target.task_key,
                    expected_revision=request.revision,
                )
            except RevisionConflictError as error:
                raise RunControlError(
                    RunControlErrorCode.REVISION_CONFLICT,
                    "Run state changed. Refresh and retry.",
                ) from error
            except InvalidTransitionError as error:
                raise RunControlError(
                    RunControlErrorCode.ACTION_CONFLICT,
                    "This operation is unavailable in the current run state.",
                ) from error

            pending = _PendingResume(
                run_ref=run_ref,
                request=request,
                campaign_id=target.campaign_id,
                revision=job.revision,
            )
            self._pending[idempotency_key] = pending
            self._trim(self._pending)
            return self._finish_resume(idempotency_key, pending)

    def _finish_resume(
        self,
        idempotency_key: str,
        pending: _PendingResume,
    ) -> RunControlReceipt:
        try:
            self._campaign_control.ensure_campaign_running(
                pending.campaign_id,
                self._launch_key(pending.run_ref, idempotency_key),
            )
        except CampaignControlError as error:
            raise RunControlError(
                RunControlErrorCode.LAUNCH_FAILED,
                "Run requeued, but the background process has not started. You can retry.",
                retryable=True,
            ) from error
        receipt = RunControlReceipt(
            run_ref=pending.run_ref,
            action="resume",
            revision=pending.revision,
            status=RunJobStatus.QUEUED.value,
            control_action="pause",
            control_label="Pause",
            message="Run resumed. The scheduler will continue from saved progress.",
        )
        self._pending.pop(idempotency_key, None)
        return self._remember(
            idempotency_key,
            pending.run_ref,
            pending.request,
            receipt,
        )

    def _target(self, run_ref: str) -> DashboardRunControlTarget:
        try:
            return self._dashboard.get_control_target(run_ref)
        except KeyError as error:
            raise RunControlError(
                RunControlErrorCode.NOT_FOUND,
                "No controllable campaign run found.",
            ) from error

    @staticmethod
    def _pause_receipt(run_ref: str, job: RunJobRecord) -> RunControlReceipt:
        waiting = job.status is RunJobStatus.RUNNING and job.pause_requested
        return RunControlReceipt(
            run_ref=run_ref,
            action="pause",
            revision=job.revision,
            status=job.status.value,
            control_action="none" if waiting else "resume",
            control_label="Pausing..." if waiting else "Resume",
            message=(
                "Pause requested. The run will stop at a safe checkpoint."
                if waiting
                else "Run paused. Resume from its saved progress."
            ),
        )

    @staticmethod
    def _require_same_request(
        run_ref: str,
        request: RunControlRequest,
        cached_ref: str,
        cached_request: RunControlRequest,
    ) -> None:
        if run_ref != cached_ref or request != cached_request:
            raise RunControlError(
                RunControlErrorCode.IDEMPOTENCY_CONFLICT,
                "This request ID belongs to another run-control operation.",
            )

    def _remember(
        self,
        key: str,
        run_ref: str,
        request: RunControlRequest,
        receipt: RunControlReceipt,
    ) -> RunControlReceipt:
        self._receipts[key] = (run_ref, request, receipt)
        self._receipts.move_to_end(key)
        self._trim(self._receipts)
        return receipt

    @staticmethod
    def _trim(records: OrderedDict[str, object]) -> None:
        while len(records) > _MAX_IDEMPOTENCY_RECORDS:
            records.popitem(last=False)

    @staticmethod
    def _launch_key(run_ref: str, idempotency_key: str) -> str:
        payload = f"run-resume\x1f{run_ref}\x1f{idempotency_key}".encode()
        return hashlib.sha256(payload).hexdigest()


__all__ = ["RunControl", "RunControlError", "RunControlErrorCode"]
