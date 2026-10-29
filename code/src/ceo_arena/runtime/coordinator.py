"""Concurrent decision barrier and single-writer shared-world coordination."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Self

from ceo_arena.models import (
    AgentBudget,
    AgentUsage,
    CompanyObservation,
    CompanyStatus,
    DecisionDiagnostics,
    DecisionWindowRequest,
    DecisionWindowResult,
    ProviderFailure,
    StrictModel,
    WorldState,
)
from ceo_arena.runtime._redact import redact_sensitive_text
from ceo_arena.runtime.admission import DecisionAdmission, UnlimitedDecisionAdmission
from ceo_arena.runtime.allowance import AllowanceLedger
from ceo_arena.runtime.context import CompanyContext, _issue_company_context
from ceo_arena.runtime.persistence import (
    CheckpointPhase,
    CheckpointStore,
    CollectingWindow,
    CompletedWindow,
    DayFingerprint,
    PendingWindow,
    RunCheckpoint,
    RunConfig,
    RunManifest,
    SealedDecision,
)
from ceo_arena.runtime.ports import AgentHarness, InferenceAwareHarness, MarketWorld
from ceo_arena.runtime.retry import ProviderRetryPolicy
from ceo_arena.runtime.store import RunStore
from ceo_arena.runtime.trace import (
    TraceSink,
    decision_barrier_event,
    run_completed_event,
    run_started_event,
    window_committed_event,
)


class BenchmarkRunResult(StrictModel):
    """Committed coordinator frontier referencing all durable run artifacts."""

    run_id: str
    final_state: WorldState
    windows: tuple[CompletedWindow, ...]
    manifest_path: str
    checkpoint_path: str
    trace_path: str


class ProviderDecisionError(RuntimeError):
    """A provider-backed decision failed before the economic barrier was sealed."""


class ProviderPermanentError(ProviderDecisionError):
    """A provider failure requires an operator change before resuming this run."""


class ProviderIdentityError(ProviderPermanentError):
    """The serving model differs from the frozen experimental identity."""


class BenchmarkCoordinator:
    """Deep orchestration module enforcing simultaneous decisions and atomic commits."""

    def __init__(
        self,
        config: RunConfig,
        world: MarketWorld,
        harnesses: Mapping[str, AgentHarness],
        run_directory: Path,
        *,
        checkpoint_store: CheckpointStore | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        decision_admission: DecisionAdmission | None = None,
        decision_observer: Callable[[DecisionWindowResult, float], None] | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self.config = config
        self.world = world
        self.run_directory = run_directory
        self._harnesses = dict(harnesses)
        self._assignments = {item.company_id: item for item in config.assignments}
        self._decision_admission = (
            decision_admission if decision_admission is not None else UnlimitedDecisionAdmission()
        )
        self._retry_policy = retry_policy or ProviderRetryPolicy()
        self._decision_observer = decision_observer
        self._validate_dependencies()
        self.store = RunStore(run_directory, trace_sink, checkpoint_store)
        self._trace = self.store.trace

    async def run(
        self, *, resume: bool = True, stop_after_day: int | None = None
    ) -> BenchmarkRunResult:
        """Run to termination or stop at a committed window without changing the horizon."""

        if stop_after_day is not None and (
            stop_after_day <= 0
            or stop_after_day > self.config.scenario.total_days
            or (
                stop_after_day != self.config.scenario.total_days
                and stop_after_day % self.config.scenario.days_per_decision_window
            )
        ):
            raise ValueError("stop_after_day must be a positive window boundary within the horizon")

        with self.store.lock:
            self.store.refresh_after_lock()
            self._trace = self.store.trace
            return await self._run_owned(resume=resume, stop_after_day=stop_after_day)

    async def _run_owned(
        self, *, resume: bool, stop_after_day: int | None = None
    ) -> BenchmarkRunResult:
        """Execute while holding exclusive ownership of the run directory."""

        checkpoint = self._open_run(resume=resume)
        if checkpoint.pending_window is not None:
            self._append_barrier_trace(checkpoint.pending_window)
            checkpoint = self._commit_pending_window(checkpoint)

        while checkpoint.current_state.day < self.config.scenario.total_days:
            if not any(
                company.status is CompanyStatus.ACTIVE
                for company in checkpoint.current_state.companies.values()
            ):
                break
            if stop_after_day is not None and checkpoint.current_state.day >= stop_after_day:
                return self._result(checkpoint)
            checkpoint = await self._collect_decision_barrier(checkpoint)
            collecting = checkpoint.collecting_window
            if collecting is None or len(collecting.decisions) != len(
                collecting.expected_company_ids
            ):
                raise ValueError("decision collection ended before the barrier was complete")
            pending = PendingWindow(
                window_index=collecting.window_index,
                start_day=collecting.start_day,
                start_world_version=collecting.start_world_version,
                start_state_hash=collecting.start_state_hash,
                decisions=collecting.decisions,
            )
            checkpoint = checkpoint.model_copy(
                update={
                    "phase": CheckpointPhase.DECISIONS_SEALED,
                    "collecting_window": None,
                    "pending_window": pending,
                }
            )
            self.store.save_checkpoint(checkpoint)
            self._append_barrier_trace(pending)
            checkpoint = self._commit_pending_window(checkpoint)

        if checkpoint.phase != CheckpointPhase.COMPLETE:
            checkpoint = checkpoint.model_copy(update={"phase": CheckpointPhase.COMPLETE})
            self.store.save_checkpoint(checkpoint)
        final_hash = self.world.state_hash(checkpoint.current_state)
        self._trace.append(
            run_completed_event(
                self.config.run_id,
                checkpoint.current_state.day,
                final_hash,
            )
        )
        return self._result(checkpoint)

    def _result(self, checkpoint: RunCheckpoint) -> BenchmarkRunResult:
        """Return the durable frontier without assigning it a terminal status."""
        return BenchmarkRunResult(
            run_id=self.config.run_id,
            final_state=checkpoint.current_state,
            windows=checkpoint.completed_windows,
            manifest_path=str(self.store.manifest_path),
            checkpoint_path=str(self.store.checkpoint_path),
            trace_path=str(self.store.trace_path),
        )

    async def aclose(self) -> None:
        """Close each distinct Harness that exposes an asynchronous close hook."""

        pending = []
        seen: set[int] = set()
        for harness in self._harnesses.values():
            if id(harness) in seen:
                continue
            seen.add(id(harness))
            close = getattr(harness, "aclose", None)
            if not callable(close):
                continue
            result = close()
            if inspect.isawaitable(result):
                pending.append(result)
        if pending:
            settled = asyncio.gather(*pending, return_exceptions=True)
            try:
                results = await asyncio.shield(settled)
            except asyncio.CancelledError:
                await asyncio.shield(settled)
                raise
            errors = [
                RuntimeError(_cleanup_note("Harness cleanup failed", result))
                for result in results
                if isinstance(result, Exception)
            ]
            if errors:
                raise ExceptionGroup("one or more Agent Harnesses failed to close", errors)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, traceback
        try:
            await self.aclose()
        except Exception as cleanup_error:
            if exc is None:
                raise
            exc.add_note(_cleanup_note("Harness cleanup also failed", cleanup_error))

    def _open_run(self, *, resume: bool) -> RunCheckpoint:
        initial_state = self.world.create_initial_state(self.config.company_ids)
        initial_hash = self.world.state_hash(initial_state)
        manifest = RunManifest.create(self.config, initial_hash)
        self.store.ensure_manifest(manifest)
        existing = self.store.load_checkpoint(
            run_id=self.config.run_id,
            config_hash=self.config.config_hash,
        )
        self._trace.append(run_started_event(self.config.run_id, initial_state.day, initial_hash))
        if existing is not None:
            if not resume:
                raise FileExistsError("checkpoint already exists; pass resume=True")
            if self.world.state_hash(existing.initial_state) != initial_hash:
                raise ValueError("checkpoint initial state differs from the current economy")
            return existing

        checkpoint = RunCheckpoint(
            run_id=self.config.run_id,
            config_hash=self.config.config_hash,
            phase=CheckpointPhase.READY,
            initial_state=initial_state,
            current_state=initial_state,
            next_window_index=0,
        )
        self.store.save_checkpoint(checkpoint)
        return checkpoint

    async def _collect_decision_barrier(self, checkpoint: RunCheckpoint) -> RunCheckpoint:
        """Persist each completed Agent result and return a complete collection."""

        state = checkpoint.current_state
        window_index = checkpoint.next_window_index
        with self.store.performance.measure("projection", window_index=window_index):
            observations = self.world.project_observations(state)
        expected_ids = tuple(
            assignment.company_id
            for assignment in self.config.assignments
            if state.companies[assignment.company_id].status is CompanyStatus.ACTIVE
        )
        missing_observations = set(expected_ids) - set(observations)
        if missing_observations:
            raise ValueError(f"world omitted company observations: {sorted(missing_observations)}")
        collecting = checkpoint.collecting_window
        if collecting is None:
            collecting = CollectingWindow(
                window_index=window_index,
                start_day=state.day,
                start_world_version=state.version,
                start_state_hash=self.world.state_hash(state),
                expected_company_ids=expected_ids,
            )
            checkpoint = checkpoint.model_copy(
                update={
                    "phase": CheckpointPhase.COLLECTING_DECISIONS,
                    "collecting_window": collecting,
                }
            )
            self.store.save_checkpoint(checkpoint)
        elif collecting.expected_company_ids != expected_ids:
            raise ValueError("collecting checkpoint active-company set drifted")

        received = {item.company_id: item for item in collecting.decisions}
        tasks = [
            asyncio.create_task(
                self._request_identified(
                    company_id,
                    collecting,
                    state,
                    observations[company_id],
                ),
                name=f"decision:{company_id}:window:{window_index}",
            )
            for company_id in expected_ids
            if company_id not in received
        ]
        try:
            for completed in asyncio.as_completed(tasks):
                company_id, decision = await completed
                received[company_id] = decision
                collecting = collecting.model_copy(
                    update={
                        "decisions": tuple(
                            received[item] for item in expected_ids if item in received
                        )
                    }
                )
                checkpoint = checkpoint.model_copy(update={"collecting_window": collecting})
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return checkpoint

    async def _request_identified(
        self,
        company_id: str,
        window: CollectingWindow,
        state: WorldState,
        observation: CompanyObservation,
    ) -> tuple[str, SealedDecision]:
        started = time.monotonic()
        attempt = 0
        ledger = AllowanceLedger(
            self.run_directory, self.config.config_hash, company_id, state.version
        )
        progress = ledger.load()
        usage, diagnostics = progress.usage, progress.diagnostics
        failure = progress.failure
        while True:
            self._check_failure(company_id, failure)
            if self.config.budget.exhausted(usage):
                if getattr(self._harnesses[company_id], "enforces_decision_budget", False):
                    # A sealed worker journal may precede a lost IPC reply. Exhausted
                    # allowance permits restoring it, never another model request.
                    restored = await self._request_decision(
                        company_id,
                        state,
                        observation,
                        prior_usage=usage,
                        prior_diagnostics=diagnostics,
                    )
                    restored = self._accumulate_usage(restored, usage, diagnostics)
                    usage, diagnostics = restored.usage, restored.diagnostics
                    ledger.save(usage, diagnostics, restored.failure or failure)
                    self._check_failure(company_id, restored.failure)
                    if restored.agent_status == "sealed":
                        return self._stage_decision(window, restored, started)
                    failure = failure or restored.failure
                failure = failure or ProviderFailure(
                    category="unknown",
                    reason="recovery_allowance_exhausted",
                    message="Failed-attempt allowance exhausted; original cause unavailable",
                )
                decision = SealedDecision.from_result(
                    company_id,
                    state.version,
                    DecisionWindowResult(
                        status="timeout",
                        usage=usage,
                        diagnostics=diagnostics.model_copy(update={"recovered": False}),
                        failure=failure,
                        error=(
                            "Infrastructure exhausted decision allowance; "
                            "existing operating policy retained"
                        ),
                    ),
                )
                return self._stage_decision(window, decision, started)
            attempt += 1
            self.store.performance.record(
                "decision_attempt",
                company_id=company_id,
                window_index=window.window_index,
                attempt=attempt,
            )
            decision = await self._request_decision(
                company_id, state, observation, prior_usage=usage, prior_diagnostics=diagnostics
            )
            prior_turns = usage.provider_turns
            attempt_latency = decision.usage.latency_ms
            decision = self._accumulate_usage(decision, usage, diagnostics)
            usage, diagnostics = decision.usage, decision.diagnostics
            if (
                not self._assignments[company_id].uses_provider
                or decision.agent_status != "provider_error"
            ):
                if decision.agent_status == "timeout" and decision.failure is None and failure:
                    decision = decision.model_copy(update={"failure": failure})
                return self._stage_decision(window, decision, started)
            failure = self._retry_policy.record_failure(
                decision.failure
                or ProviderFailure(
                    category="unknown", message=decision.error or "provider-backed decision failed"
                ),
                failure,
                made_progress=usage.provider_turns > prior_turns,
            )
            ledger.save(usage, diagnostics, failure)
            self._check_failure(company_id, failure)
            self.store.performance.record(
                "provider_retry",
                attempt_latency,
                company_id=company_id,
                window_index=window.window_index,
                attempt=attempt,
                failure=(
                    failure
                    if failure.http_status == 400
                    else failure.model_copy(update={"attempt": attempt})
                ),
            )
            if self.config.budget.exhausted(usage):
                continue
            # Only this company waits. Other tasks remain live and durably stage their receipts.
            with self.store.performance.measure(
                "retry_wait", company_id=company_id, window_index=window.window_index
            ):
                delay = self._retry_policy.delay(
                    (failure.attempt or 1) if failure.http_status == 400 else attempt, failure
                )
                remaining = self.config.budget.remaining_seconds(usage)
                if remaining is not None:
                    delay = min(delay, remaining)
                usage = await ledger.wait(delay, usage, diagnostics, failure)

    def _check_failure(self, company_id: str, failure: ProviderFailure | None) -> None:
        if failure is None or self._retry_policy.can_retry(failure):
            return
        self.store.performance.record(
            "provider_permanent_failure", company_id=company_id, failure=failure
        )
        error = ProviderIdentityError if failure.category == "identity" else ProviderPermanentError
        raise error(f"{company_id}: {failure.message}")

    @staticmethod
    def _accumulate_usage(
        decision: SealedDecision, usage: AgentUsage, diagnostics: DecisionDiagnostics
    ) -> SealedDecision:
        diagnostics = diagnostics.plus(decision.diagnostics)
        if (
            decision.agent_status == "sealed"
            and diagnostics.protocol_errors + diagnostics.tool_errors
        ):
            diagnostics = diagnostics.model_copy(update={"recovered": True})
        return decision.model_copy(
            update={"usage": usage.plus(decision.usage), "diagnostics": diagnostics}
        )

    def _stage_decision(
        self, window: CollectingWindow, decision: SealedDecision, started: float
    ) -> tuple[str, SealedDecision]:
        self.store.stage_decision(
            run_id=self.config.run_id,
            config_hash=self.config.config_hash,
            window=window,
            response=decision,
        )
        if decision.agent_status == "timeout" and decision.failure is not None:
            self.store.performance.record(
                "provider_timeout",
                company_id=decision.company_id,
                window_index=window.window_index,
                failure=decision.failure,
            )
        self.store.performance.record(
            "decision_total",
            (time.monotonic() - started) * 1000,
            company_id=decision.company_id,
            window_index=window.window_index,
        )
        return decision.company_id, decision

    async def _request_decision(
        self,
        company_id: str,
        state: WorldState,
        observation: CompanyObservation,
        budget: AgentBudget | None = None,
        prior_usage: AgentUsage | None = None,
        prior_diagnostics: DecisionDiagnostics | None = None,
    ) -> SealedDecision:
        waiting = time.perf_counter()
        async with self._decision_admission.admit(
            requires_model_slot=self._assignments[company_id].uses_provider
        ):
            self.store.performance.record(
                "queue", (time.perf_counter() - waiting) * 1000, company_id=company_id
            )
            scope = getattr(self._harnesses[company_id], "decision_scope", None)
            async with scope(company_id) if callable(scope) else nullcontext():
                with self.store.performance.measure("sql_projection", company_id=company_id):
                    context = _issue_company_context(
                        observation,
                        partial(self.world.validate_decision, state),
                        timing=lambda stage, duration: self.store.performance.record(
                            stage, duration, company_id=company_id
                        ),
                    )
                try:
                    request = DecisionWindowRequest(
                        run_id=self.config.run_id,
                        company_id=company_id,
                        world_version=state.version,
                        observation=observation.model_copy(deep=True),
                        budget=budget or self.config.budget,
                        prior_usage=prior_usage or AgentUsage(),
                        prior_diagnostics=prior_diagnostics or DecisionDiagnostics(),
                    )
                    started = time.monotonic()
                    with self.store.performance.measure("harness", company_id=company_id):
                        result = await self._invoke_harness(company_id, request, context)
                    if (
                        result.status in {"provider_error", "timeout"}
                        and not result.usage.latency_ms
                    ):
                        result = result.model_copy(
                            update={
                                "usage": result.usage.model_copy(
                                    update={
                                        "latency_ms": max(
                                            1, round((time.monotonic() - started) * 1000)
                                        )
                                    }
                                )
                            }
                        )
                    if self._decision_observer and self._assignments[company_id].uses_provider:
                        self._decision_observer(result, time.monotonic() - started)
                finally:
                    context._revoke()
        result = self._validate_harness_result(
            company_id, state.version, result, budget, prior_usage
        )
        if result.status == "sealed" and result.decision is not None:
            try:
                self.world.validate_decision(state, result.decision)
            except ValueError as exc:
                result = DecisionWindowResult(
                    status="protocol_error",
                    usage=result.usage,
                    diagnostics=result.diagnostics.model_copy(update={"recovered": False}),
                    trace_path=result.trace_path,
                    trace_sha256=result.trace_sha256,
                    error=redact_sensitive_text(f"invalid economic decision: {exc}"),
                )
        return SealedDecision.from_result(company_id, state.version, result)

    async def _invoke_harness(
        self,
        company_id: str,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        """Call one admitted Harness and enforce only its active execution timeout."""

        started = time.monotonic()
        limit = request.budget.remaining_seconds(request.prior_usage)
        worker_deadline = getattr(self._harnesses[company_id], "enforces_decision_budget", False)
        if limit is not None and worker_deadline:
            limit += 15  # IPC/cancellation grace, not additional model execution time.
        deadline = asyncio.timeout(limit)
        try:
            async with deadline:
                return await self._harnesses[company_id].decide(request, context)
        except TimeoutError:
            if not deadline.expired():
                return DecisionWindowResult(
                    status="provider_error",
                    usage=AgentUsage(latency_ms=round((time.monotonic() - started) * 1000)),
                    failure=ProviderFailure(
                        category="transport",
                        reason="TimeoutError",
                        message="provider transport timed out",
                    ),
                    error="provider transport timed out",
                )
            await self._harnesses[company_id].cancel(company_id)
            assert limit is not None
            return DecisionWindowResult(
                status="provider_error" if worker_deadline else "timeout",
                usage=AgentUsage(latency_ms=round((time.monotonic() - started) * 1000)),
                failure=ProviderFailure(
                    category="worker",
                    reason="WorkerDeadlineExceeded",
                    message="Worker did not finish within the remaining decision allowance",
                )
                if worker_deadline
                else None,
                error=f"decision exceeded {limit:g}s timeout",
            )
        except asyncio.CancelledError as cancellation:
            try:
                await asyncio.shield(self._harnesses[company_id].cancel(company_id))
            except Exception as cleanup_error:
                cancellation.add_note(
                    _cleanup_note("Harness cancellation cleanup failed", cleanup_error)
                )
            raise
        except Exception as exc:
            return DecisionWindowResult(
                status="provider_error",
                usage=AgentUsage(latency_ms=round((time.monotonic() - started) * 1000)),
                failure=ProviderFailure(
                    category="worker",
                    reason=type(exc).__name__,
                    message=redact_sensitive_text(str(exc)) or "worker execution failed",
                ),
                error=redact_sensitive_text(f"{type(exc).__name__}: {exc}"),
            )

    def _validate_harness_result(
        self,
        company_id: str,
        world_version: int,
        result: DecisionWindowResult,
        budget: AgentBudget | None = None,
        prior_usage: AgentUsage | None = None,
    ) -> DecisionWindowResult:
        if result.error:
            result = result.model_copy(update={"error": redact_sensitive_text(result.error)})
        if result.failure is not None:
            result = result.model_copy(
                update={
                    "failure": result.failure.model_copy(
                        update={
                            "message": (redact_sensitive_text(result.failure.message) or "")[:2000],
                            "reason": (redact_sensitive_text(result.failure.reason) or "")[:256]
                            or None,
                        }
                    )
                }
            )
        decision = result.decision
        limits = budget or self.config.budget
        usage = result.usage.plus(prior_usage or AgentUsage())
        violation: str | None = None
        if result.status == "sealed" and decision is None:
            violation = "sealed result omitted CompanyDecision"
        elif decision is not None and decision.company_id != company_id:
            violation = "decision company_id does not match issued CompanyContext"
        elif decision is not None and decision.world_version != world_version:
            violation = "decision targets a stale world_version"
        elif usage.provider_turns > limits.max_provider_turns:
            violation = "provider turn budget exceeded"
        elif limits.max_tool_calls is not None and usage.tool_calls > limits.max_tool_calls:
            violation = "tool call budget exceeded"
        elif (
            limits.max_output_tokens is not None and usage.output_tokens > limits.max_output_tokens
        ):
            violation = "output token budget exceeded"
        if violation is None:
            return result
        return DecisionWindowResult(
            status="protocol_error",
            usage=result.usage,
            diagnostics=result.diagnostics.model_copy(update={"recovered": False}),
            trace_path=result.trace_path,
            trace_sha256=result.trace_sha256,
            error=violation,
        )

    def _commit_pending_window(self, checkpoint: RunCheckpoint) -> RunCheckpoint:
        pending = checkpoint.pending_window
        if pending is None:
            raise ValueError("cannot commit a checkpoint without sealed decisions")
        actual_start_hash = self.world.state_hash(checkpoint.current_state)
        if actual_start_hash != pending.start_state_hash:
            raise ValueError("pending decision barrier does not match checkpoint state")
        decisions = {
            item.company_id: item.decision.model_copy(deep=True) for item in pending.decisions
        }
        with self.store.performance.measure("settlement", window_index=pending.window_index):
            result = self.world.advance_window(checkpoint.current_state, decisions)
        completed = CompletedWindow(
            **pending.model_dump(),
            end_day=result.end_day,
            end_world_version=result.world_version,
            day_fingerprints=tuple(
                DayFingerprint(
                    day=day.day,
                    world_version=day.world_version,
                    state_hash=day.state_hash,
                )
                for day in result.days
            ),
            end_state_hash=self.world.state_hash(result.state),
        )
        windows = (*checkpoint.completed_windows, completed)
        phase = (
            CheckpointPhase.COMPLETE
            if result.state.day >= self.config.scenario.total_days
            else CheckpointPhase.READY
        )
        committed = RunCheckpoint(
            run_id=checkpoint.run_id,
            config_hash=checkpoint.config_hash,
            phase=phase,
            initial_state=checkpoint.initial_state,
            current_state=result.state,
            next_window_index=len(windows),
            completed_windows=windows,
        )
        self._trace.append(window_committed_event(self.config.run_id, completed))
        self.store.save_checkpoint(committed)
        return committed

    def _append_barrier_trace(self, pending: PendingWindow) -> None:
        self._trace.append(decision_barrier_event(self.config.run_id, pending))

    def _validate_dependencies(self) -> None:
        if self.world.config != self.config.scenario:
            raise ValueError("MarketWorld config differs from RunConfig scenario")
        expected = set(self.config.company_ids)
        actual = set(self._harnesses)
        if actual != expected:
            raise ValueError(
                f"harness company set mismatch: missing={sorted(expected - actual)}, "
                f"extra={sorted(actual - expected)}"
            )
        for assignment in self.config.assignments:
            harness = self._harnesses[assignment.company_id]
            if harness.harness_id != assignment.harness_id:
                raise ValueError(
                    f"harness id mismatch for {assignment.company_id}: "
                    f"{harness.harness_id!r} != {assignment.harness_id!r}"
                )
            harness_model = harness.model_id
            if (
                isinstance(harness, InferenceAwareHarness)
                and harness.inference_settings != assignment.inference
            ):
                raise ValueError(f"inference settings mismatch for {assignment.company_id}")
            if harness_model != assignment.model_id:
                raise ValueError(
                    f"model id mismatch for {assignment.company_id}: "
                    f"{harness_model!r} != {assignment.model_id!r}"
                )


def _cleanup_note(label: str, error: Exception) -> str:
    detail = redact_sensitive_text(f"{type(error).__name__}: {error}") or type(error).__name__
    return f"{label}: {detail}"[:2_000]
