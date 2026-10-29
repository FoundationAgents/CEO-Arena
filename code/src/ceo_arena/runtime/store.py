"""Unified durable artifact store for one benchmark run."""

from __future__ import annotations

import time
from pathlib import Path

from ceo_arena.models import ProviderFailure
from ceo_arena.runtime.lock import RunDirectoryLock
from ceo_arena.runtime.performance import PerformanceRecorder
from ceo_arena.runtime.persistence import (
    AtomicCheckpointStore,
    CheckpointStore,
    CollectingWindow,
    RunCheckpoint,
    RunManifest,
    RunManifestStore,
    SealedDecision,
)
from ceo_arena.runtime.trace import JsonlTraceSink, TraceSink


class RunStore:
    """Own manifest, checkpoint, and append-only trace paths behind one boundary."""

    def __init__(
        self,
        run_directory: Path,
        trace_sink: TraceSink | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.run_directory = run_directory
        self.manifest_path = run_directory / "manifest.json"
        self.checkpoint_path = run_directory / "checkpoint.json"
        self.trace_path = run_directory / "trace.jsonl"
        self.lock = RunDirectoryLock(run_directory / ".run.lock")
        self._external_trace = trace_sink is not None
        self.manifests = RunManifestStore(self.manifest_path)
        self.checkpoints = checkpoint_store or AtomicCheckpointStore(self.checkpoint_path)
        self.performance = PerformanceRecorder(run_directory)
        self.trace = trace_sink or JsonlTraceSink(
            self.trace_path,
            tolerate_torn_tail=True,
        )

    def refresh_after_lock(self) -> None:
        """Reload disk-backed trace state after exclusive ownership begins."""

        if not self._external_trace:
            self.trace = JsonlTraceSink(self.trace_path, repair_torn_tail=True)

    def ensure_manifest(self, manifest: RunManifest) -> RunManifest:
        existing = self.manifests.ensure(manifest)
        self.performance.bind(existing)
        return existing

    def load_checkpoint(self, *, run_id: str, config_hash: str) -> RunCheckpoint | None:
        checkpoint = self.checkpoints.load(run_id=run_id, config_hash=config_hash)
        if checkpoint is not None:
            self.performance.update_frontier(checkpoint)
        return checkpoint

    def save_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        started = time.perf_counter()
        self.checkpoints.save(checkpoint)
        self.performance.update_frontier(checkpoint)
        self.performance.record(
            "checkpoint",
            (time.perf_counter() - started) * 1000,
            window_index=checkpoint.next_window_index,
        )

    def stage_decision(
        self, *, run_id: str, config_hash: str, window: CollectingWindow, response: SealedDecision
    ) -> None:
        started = time.perf_counter()
        try:
            self.checkpoints.stage_decision(
                run_id=run_id, config_hash=config_hash, window=window, response=response
            )
        except BaseException as error:
            self.performance.record(
                "decision_stage_failed",
                (time.perf_counter() - started) * 1000,
                company_id=response.company_id,
                window_index=window.window_index,
                failure=ProviderFailure(
                    category="internal",
                    message="Decision receipt persistence failed",
                    retryable=False,
                    reason=type(error).__name__,
                ),
            )
            raise
        self.performance.record(
            "decision_stage",
            (time.perf_counter() - started) * 1000,
            company_id=response.company_id,
            window_index=window.window_index,
        )
