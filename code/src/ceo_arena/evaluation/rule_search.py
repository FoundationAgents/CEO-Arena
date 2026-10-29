"""Predeclared fixed-playbook search against eight freshly simulated rule opponents."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Literal
from zipfile import ZIP_DEFLATED, ZipFile

from pydantic import Field, model_validator

from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.evaluation.scoring import score_world
from ceo_arena.execution import execute_run
from ceo_arena.harness.rule_ceo import MarketFocus, RuleCEOConfig, RuleCeoHarness
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import CompanyStatus, ScenarioConfig, StrictModel
from ceo_arena.provenance import simulation_fingerprints
from ceo_arena.runtime import AgentAssignment, ExactReplay, RunConfig
from ceo_arena.runtime._atomic import AtomicModelFile
from ceo_arena.runtime.lock import RunDirectoryLock
from ceo_arena.runtime.store import RunStore

SEARCH_VERSION = "fixed-rule-grid-v1"
FOCAL_COMPANY = "company-1"
PROFILE_FILE = "src/ceo_arena/harness/rule_profile.json"
BACKGROUND_FOCI: tuple[MarketFocus, ...] = (
    "value",
    "value",
    "value",
    "mainstream",
    "mainstream",
    "mainstream",
    "premium",
    "premium",
)


class RuleCandidate(StrictModel):
    candidate_id: str
    config: RuleCEOConfig


def candidate_grid() -> tuple[RuleCandidate, ...]:
    """Enumerate 2 price books x 3 foci x 2 spend packages x 2 cash floors."""
    candidates: list[RuleCandidate] = []
    foci: tuple[MarketFocus, ...] = ("value", "mainstream", "premium")
    for price, focus, scale, floor in product(
        (9_000, 10_000), foci, (1, 2), (5_000_000, 15_000_000)
    ):
        candidates.append(
            RuleCandidate(
                candidate_id=f"candidate-{len(candidates) + 1:02}",
                config=RuleCEOConfig(
                    focus=focus,
                    price_multiplier_bps=price,
                    development_daily_cents=5_000 * scale,
                    marketing_daily_cents=5_000 * scale,
                    reliability_daily_cents=2_500 * scale,
                    support_daily_cents=5_000 * scale,
                    cash_floor_cents=floor,
                ),
            )
        )
    return tuple(candidates)


def search_fingerprints(root: Path) -> dict[str, str]:
    """Exclude the installed winner: every search participant receives explicit parameters."""
    fingerprints = simulation_fingerprints(root)
    fingerprints.pop(PROFILE_FILE, None)
    for name in ("scripts/search_rule.py", "pyproject.toml", "uv.lock"):
        fingerprints[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    return fingerprints


class RuleSearchPlan(StrictModel):
    version: Literal["fixed-rule-grid-v1"] = SEARCH_VERSION
    scenario: ScenarioConfig
    candidates: tuple[RuleCandidate, ...]
    background: tuple[RuleCEOConfig, ...]
    development_seeds: tuple[int, ...] = (7, 19, 42)
    validation_seeds: tuple[int, ...] = (211, 307, 401)
    source_fingerprints: dict[str, str]
    selection_metric: Literal["focal_mean_official_score"] = "focal_mean_official_score"
    tie_break: Literal["candidate_order"] = "candidate_order"

    @model_validator(mode="after")
    def validate_design(self) -> RuleSearchPlan:
        if len(self.background) != 8 or not self.candidates:
            raise ValueError("search requires one focal and eight background companies")
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate ids must be unique")
        for seeds in (self.development_seeds, self.validation_seeds):
            if not seeds or len(seeds) != len(set(seeds)):
                raise ValueError("seed lists must be nonempty and unique")
        if set(self.development_seeds) & set(self.validation_seeds):
            raise ValueError("validation seeds cannot select the winner")
        return self

    @property
    def plan_hash(self) -> str:
        return canonical_model_hash(self)

    @classmethod
    def official(cls, root: Path) -> RuleSearchPlan:
        return cls(
            scenario=default_scenario(seed=0, total_days=500),
            candidates=candidate_grid(),
            background=tuple(RuleCEOConfig(focus=focus) for focus in BACKGROUND_FOCI),
            source_fingerprints=search_fingerprints(root),
        )


class CompanyResult(StrictModel):
    company_id: str
    score_cents: int
    status: CompanyStatus
    cash_cents: int
    inventory_salvage_cents: int
    revenue_cents: int
    payments_cents: int
    refunds_cents: int


class SearchCaseResult(StrictModel):
    plan_hash: str
    split: Literal["development", "validation"]
    candidate_id: str
    config: RuleCEOConfig
    seed: int
    run_id: str
    run_config_hash: str
    terminal_day: int
    focal_score_cents: int
    companies: tuple[CompanyResult, ...]
    final_state_hash: str
    replay_verified_days: int
    decision_count: int
    model_api_calls: Literal[0] = 0
    elapsed_seconds: float = Field(ge=0)
    artifacts: str


class CandidateScore(StrictModel):
    candidate_id: str
    config: RuleCEOConfig
    scores_cents: tuple[int, ...]
    mean_score_cents: float


class RuleSelection(StrictModel):
    plan_hash: str
    selected: RuleCandidate
    development_scores: tuple[CandidateScore, ...]


def select_winner(plan: RuleSearchPlan, results: tuple[SearchCaseResult, ...]) -> RuleSelection:
    """Require the entire development grid and break exact integer-sum ties by grid order."""
    lookup = {(item.candidate_id, item.seed): item for item in results}
    expected = {
        (item.candidate_id, seed) for item in plan.candidates for seed in plan.development_seeds
    }
    if len(lookup) != len(results) or set(lookup) != expected:
        raise ValueError("cannot select from missing or duplicated development results")
    scores: list[CandidateScore] = []
    for candidate in plan.candidates:
        rows = tuple(lookup[candidate.candidate_id, seed] for seed in plan.development_seeds)
        if any(
            row.split != "development"
            or row.plan_hash != plan.plan_hash
            or row.config != candidate.config
            for row in rows
        ):
            raise ValueError("development result provenance differs from the frozen search")
        values = tuple(row.focal_score_cents for row in rows)
        scores.append(
            CandidateScore(
                candidate_id=candidate.candidate_id,
                config=candidate.config,
                scores_cents=values,
                mean_score_cents=sum(values) / len(values),
            )
        )
    best = max(range(len(scores)), key=lambda index: (sum(scores[index].scores_cents), -index))
    return RuleSelection(
        plan_hash=plan.plan_hash, selected=plan.candidates[best], development_scores=tuple(scores)
    )


class RuleSearchReport(StrictModel):
    plan: RuleSearchPlan
    selection: RuleSelection
    development: tuple[SearchCaseResult, ...]
    validation: tuple[SearchCaseResult, ...]
    generated_at: datetime
    elapsed_seconds: float
    all_requested_runs_completed: Literal[True] = True
    model_api_calls: Literal[0] = 0


class _SearchHarness(RuleCeoHarness):
    def __init__(self, config: RuleCEOConfig, company_id: str) -> None:
        super().__init__(config)
        self._model_id = f"fixed-rule@{company_id}"

    @property
    def model_id(self) -> str:
        return self._model_id


async def _execute_case(
    plan: RuleSearchPlan,
    candidate: RuleCandidate,
    seed: int,
    split: Literal["development", "validation"],
    root: Path,
    output: Path,
) -> SearchCaseResult:
    if search_fingerprints(root) != plan.source_fingerprints:
        raise ValueError("source changed during fixed-rule search")
    started = time.monotonic()
    relative = Path(split) / candidate.candidate_id / f"seed-{seed}"
    directory = output / relative
    configs = (candidate.config, *plan.background)
    harnesses = {
        f"company-{index + 1}": _SearchHarness(config, f"company-{index + 1}")
        for index, config in enumerate(configs)
    }
    config = RunConfig(
        run_id=f"{SEARCH_VERSION}-{plan.plan_hash[:12]}-{split}-{candidate.candidate_id}-s{seed}",
        scenario=plan.scenario.model_copy(update={"seed": seed}),
        assignments=tuple(
            AgentAssignment(
                company_id=company_id, model_id=harness.model_id, harness_id=harness.harness_id
            )
            for company_id, harness in harnesses.items()
        ),
        artifact_fingerprints=plan.source_fingerprints,
    )
    run = await execute_run(
        config,
        harnesses,
        directory,
        fresh=not (directory / "manifest.json").exists(),
        announce=False,
    )
    if any(
        decision.agent_status != "sealed"
        or decision.usage.provider_turns
        or decision.usage.tool_calls
        for window in run.windows
        for decision in window.decisions
    ):
        raise ValueError("rule search must use only valid deterministic, model-free decisions")
    world = MarketWorld(config.scenario)
    store = RunStore(directory)
    with store.lock:
        checkpoint = store.load_checkpoint(run_id=config.run_id, config_hash=config.config_hash)
        if checkpoint is None:
            raise ValueError("completed search run has no checkpoint")
        replay = ExactReplay(
            world, runtime_trace_path=directory / "trace.jsonl", artifact_directory=directory
        ).verify(checkpoint)
    scores = score_world(run.final_state, config.scenario)
    companies: list[CompanyResult] = []
    for company_id, company in sorted(run.final_state.companies.items()):
        if company.cash_cents != (
            company.initial_cash_cents
            + company.cumulative_revenue_cents
            + company.cumulative_refunds_cents
            - company.cumulative_cost_cents
        ):
            raise ValueError("cash identity failed")
        score = scores[company_id]
        companies.append(
            CompanyResult(
                company_id=company_id,
                score_cents=score.official_score_cents,
                status=company.status,
                cash_cents=company.cash_cents,
                inventory_salvage_cents=score.diagnostics.inventory_salvage_cents,
                revenue_cents=company.cumulative_revenue_cents,
                payments_cents=company.cumulative_cost_cents,
                refunds_cents=company.cumulative_refunds_cents,
            )
        )
    if search_fingerprints(root) != plan.source_fingerprints:
        raise ValueError("source changed; cannot publish mixed-version search results")
    result = SearchCaseResult(
        plan_hash=plan.plan_hash,
        split=split,
        candidate_id=candidate.candidate_id,
        config=candidate.config,
        seed=seed,
        run_id=config.run_id,
        run_config_hash=config.config_hash,
        terminal_day=run.final_state.day,
        focal_score_cents=scores[FOCAL_COMPANY].official_score_cents,
        companies=tuple(companies),
        final_state_hash=replay.final_state_hash,
        replay_verified_days=replay.verified_days,
        decision_count=sum(len(window.decisions) for window in run.windows),
        elapsed_seconds=time.monotonic() - started,
        artifacts=relative.as_posix(),
    )
    result_file = AtomicModelFile(directory / "search_result.json", SearchCaseResult)
    previous = result_file.load()
    if previous is not None:
        if previous.model_dump(exclude={"elapsed_seconds"}) != result.model_dump(
            exclude={"elapsed_seconds"}
        ):
            raise ValueError("saved case results do not match verified replay")
        return previous
    result_file.save(result)
    return result


def _run_case(
    plan: RuleSearchPlan,
    candidate: RuleCandidate,
    seed: int,
    split: Literal["development", "validation"],
    root: Path,
    output: Path,
) -> SearchCaseResult:
    return asyncio.run(_execute_case(plan, candidate, seed, split, root, output))


class FixedRuleSearch:
    """Freeze the design, finish development, lock its winner, then validate without retuning."""

    def __init__(self, root: Path, output: Path, plan: RuleSearchPlan | None = None) -> None:
        self.root, self.output = root.resolve(), output.resolve()
        self.plan = plan or RuleSearchPlan.official(self.root)

    def _batch(
        self,
        candidates: tuple[RuleCandidate, ...],
        seeds: tuple[int, ...],
        split: Literal["development", "validation"],
        workers: int,
    ) -> tuple[SearchCaseResult, ...]:
        cases = tuple((candidate, seed) for candidate in candidates for seed in seeds)
        results: list[SearchCaseResult] = []
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_run_case, self.plan, candidate, seed, split, self.root, self.output)
                for candidate, seed in cases
            ]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print(
                    f"{split} {len(results)}/{len(cases)} {result.candidate_id} seed={result.seed} "
                    f"day={result.terminal_day} focal_score=${result.focal_score_cents / 100:.2f}",
                    flush=True,
                )
        order = {
            (candidate.candidate_id, seed): index for index, (candidate, seed) in enumerate(cases)
        }
        return tuple(sorted(results, key=lambda result: order[result.candidate_id, result.seed]))

    def run(self, *, workers: int = 2) -> RuleSearchReport:
        if workers < 1:
            raise ValueError("workers must be positive")
        started = time.monotonic()
        with RunDirectoryLock(self.output / ".search.lock"):
            plan_file = AtomicModelFile(self.output / "search_plan.json", RuleSearchPlan)
            previous = plan_file.load()
            if previous is not None and previous != self.plan:
                raise ValueError("existing output belongs to a different frozen search")
            if previous is None:
                plan_file.save(self.plan)
                with ZipFile(self.output / "source_snapshot.zip", "w", ZIP_DEFLATED) as archive:
                    archive.write(self.root / PROFILE_FILE, PROFILE_FILE)
                    for name in self.plan.source_fingerprints:
                        if ":" not in name and (self.root / name).is_file():
                            archive.write(self.root / name, name)
            development = self._batch(
                self.plan.candidates, self.plan.development_seeds, "development", workers
            )
            selection = select_winner(self.plan, development)
            selection_file = AtomicModelFile(self.output / "selection.json", RuleSelection)
            previous_selection = selection_file.load()
            if previous_selection is not None and previous_selection != selection:
                raise ValueError("frozen development winner changed")
            selection_file.save(selection)
            print(
                f"FROZEN WINNER: {selection.selected.candidate_id}; validation cannot change it",
                flush=True,
            )
            validation = self._batch(
                (selection.selected,), self.plan.validation_seeds, "validation", workers
            )
            report = RuleSearchReport(
                plan=self.plan,
                selection=selection,
                development=development,
                validation=validation,
                generated_at=datetime.now(UTC),
                elapsed_seconds=time.monotonic() - started,
            )
            AtomicModelFile(self.output / "report.json", RuleSearchReport).save(report)
            AtomicModelFile(self.output / "selected_config.json", RuleCEOConfig).save(
                selection.selected.config
            )
            with (self.output / "scores.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    ("split", "candidate", "seed", "day", "focal_score_usd", "focal_status")
                )
                for result in (*development, *validation):
                    focal = next(
                        company
                        for company in result.companies
                        if company.company_id == FOCAL_COMPANY
                    )
                    writer.writerow(
                        (
                            result.split,
                            result.candidate_id,
                            result.seed,
                            result.terminal_day,
                            f"{result.focal_score_cents / 100:.2f}",
                            focal.status,
                        )
                    )
            return report
