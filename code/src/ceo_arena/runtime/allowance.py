"""Durable reported allowance, separate from economic decision receipts."""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

from pydantic import Field

from ceo_arena.models import AgentUsage, DecisionDiagnostics, ProviderFailure, StrictModel
from ceo_arena.runtime._atomic import AtomicModelFile


class AllowanceProgress(StrictModel):
    config_hash: str
    usage: AgentUsage = Field(default_factory=AgentUsage)
    diagnostics: DecisionDiagnostics = Field(default_factory=DecisionDiagnostics)
    failure: ProviderFailure | None = None


class AllowanceLedger:
    """Retain reported failed-attempt costs across retries and manual restarts."""

    def __init__(self, directory: Path, config_hash: str, company_id: str, version: int) -> None:
        identity = hashlib.sha256(f"{company_id}:{version}".encode()).hexdigest()
        self._file = AtomicModelFile(
            directory / "allowances" / f"{identity}.json", AllowanceProgress
        )
        self._config_hash = config_hash

    def load(self) -> AllowanceProgress:
        progress = self._file.load() or AllowanceProgress(config_hash=self._config_hash)
        if progress.config_hash != self._config_hash:
            raise ValueError("reported allowance differs from frozen run configuration")
        return progress

    def save(
        self,
        usage: AgentUsage,
        diagnostics: DecisionDiagnostics,
        failure: ProviderFailure | None = None,
    ) -> None:
        self._file.save(
            AllowanceProgress(
                config_hash=self._config_hash, usage=usage, diagnostics=diagnostics, failure=failure
            )
        )

    async def wait(
        self,
        seconds: float,
        usage: AgentUsage,
        diagnostics: DecisionDiagnostics,
        failure: ProviderFailure,
    ) -> AgentUsage:
        """Charge automatic backoff, including partial waits interrupted by a pause."""
        started = time.monotonic()
        try:
            await asyncio.sleep(seconds)
        finally:
            elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
            usage = usage.plus(AgentUsage(latency_ms=elapsed_ms))
            self.save(usage, diagnostics, failure)
        return usage
