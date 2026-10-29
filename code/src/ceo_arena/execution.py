"""Application-level execution for one durable shared-world benchmark run."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from ceo_arena.economy import MarketWorld
from ceo_arena.evaluation import CompanyScore, score_world
from ceo_arena.models import RecoverySummary, StrictModel
from ceo_arena.runtime import (
    AgentHarness,
    BenchmarkCoordinator,
    BenchmarkRunResult,
    CheckpointStore,
    DecisionAdmission,
    RunConfig,
)
from ceo_arena.runtime._atomic import AtomicModelFile
from ceo_arena.runtime.persistence import CompletedWindow


class RunSummary(StrictModel):
    """Stable public summary derived from one complete checkpoint."""

    run_id: str
    day: int
    world_version: int
    scores: dict[str, CompanyScore]
    decision_status_counts: dict[str, int]
    recovery_by_company: dict[str, RecoverySummary]
    manifest: str
    checkpoint: str
    trace: str


async def execute_run(
    run_config: RunConfig,
    harnesses: Mapping[str, AgentHarness],
    output: Path,
    *,
    fresh: bool,
    checkpoint_store: CheckpointStore | None = None,
    decision_admission: DecisionAdmission | None = None,
    announce: bool = True,
) -> BenchmarkRunResult:
    """Execute or resume one run, then atomically publish its typed summary."""

    coordinator = BenchmarkCoordinator(
        config=run_config,
        world=MarketWorld(run_config.scenario),
        harnesses=harnesses,
        run_directory=output,
        checkpoint_store=checkpoint_store,
        decision_admission=decision_admission,
    )
    async with coordinator:
        result = await coordinator.run(resume=not fresh)
    summary = RunSummary(
        run_id=result.run_id,
        day=result.final_state.day,
        world_version=result.final_state.version,
        scores=score_world(result.final_state, run_config.scenario),
        decision_status_counts=decision_status_counts(result.windows),
        recovery_by_company=recovery_by_company(result.windows),
        manifest=result.manifest_path,
        checkpoint=result.checkpoint_path,
        trace=result.trace_path,
    )
    AtomicModelFile(output / "summary.json", RunSummary).save(summary)
    if announce:
        print(output / "summary.json")
    return result


def decision_status_counts(windows: tuple[CompletedWindow, ...]) -> dict[str, int]:
    """Aggregate every company decision status across committed windows."""

    counts: Counter[str] = Counter()
    for company_counts in decision_status_by_company(windows).values():
        counts.update(company_counts)
    return dict(sorted(counts.items()))


def decision_status_by_company(
    windows: tuple[CompletedWindow, ...],
) -> dict[str, dict[str, int]]:
    """Aggregate decision status diagnostics by fixed company identity."""

    grouped: dict[str, Counter[str]] = {}
    for window in windows:
        for decision in window.decisions:
            grouped.setdefault(decision.company_id, Counter())[decision.agent_status] += 1
    return {
        company_id: dict(sorted(counts.items())) for company_id, counts in sorted(grouped.items())
    }


def recovery_by_company(windows: tuple[CompletedWindow, ...]) -> dict[str, RecoverySummary]:
    """Aggregate model errors without counting successful recovery as window failure."""

    grouped: dict[str, RecoverySummary] = {}
    for window in windows:
        for decision in window.decisions:
            grouped.setdefault(decision.company_id, RecoverySummary()).include(
                decision.diagnostics,
                infrastructure_timeout=(
                    decision.agent_status == "timeout" and decision.failure is not None
                ),
            )
    return dict(sorted(grouped.items()))
