"""Exploratory lean-plus-innovation pairs against a previously frozen lean receipt."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from ceo_arena.economy import MarketWorld
from ceo_arena.evaluation import score_world
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import CompanyDecision, CompanyObservation, CompanyStatus, StrictModel
from scripts.validate_economy import (
    DiagnosticPolicy,
    EconomyValidator,
    PolicyName,
    ValidationReport,
    ValidationRun,
)


class LeanInnovationPolicy(DiagnosticPolicy):
    """Change only the initial investment rule; later lean actions react to each world's state."""

    def __init__(self, tier: Literal[1, 2]) -> None:
        super().__init__(PolicyName.LEAN)
        self.tier = tier

    async def decide(self, observation: CompanyObservation) -> CompanyDecision:
        decision = await super().decide(observation)
        if observation.day != 0:
            return decision
        project = next(item for item in observation.innovation_catalog if item.tier == self.tier)
        if project.cost_cents > observation.company.cash_cents // 3:
            raise ValueError("the requested innovation exceeds the predeclared cash allowance")
        return decision.model_copy(update={"research_project_tier": self.tier})


class InnovationOutcome(StrictModel):
    tier: Literal[1, 2]
    run: ValidationRun
    focal_score_difference_cents: int


class InnovationFollowupReport(StrictModel):
    label: str = "follow-up development: lean plus day-zero innovation"
    rationale: str = (
        "Requested after the original Rule-plus-innovation ablation was negative. "
        "Removing continuous R&D may expose the discrete project's marginal opportunity. "
        "This follow-up is exploratory, not preregistered confirmation; no parameters or "
        "other controller rules are changed and negative results are retained."
    )
    baseline_receipt_sha256: str
    baseline_run: ValidationRun
    scenario_sha256: str
    source_sha256: dict[str, str]
    policy_parameters_sha256: str
    generated_at: str
    elapsed_seconds: float = Field(ge=0.0)
    workers: int = Field(ge=1)
    outcomes: tuple[InnovationOutcome, ...]
    model_api_calls: int = Field(default=0, ge=0, le=0)


class InnovationFollowup:
    """Preserve the earlier validator and compare only matched public-observation policies."""

    def __init__(self, reference: Path, repository: Path) -> None:
        self.reference_path = reference
        self.reference = ValidationReport.model_validate_json(reference.read_text(encoding="utf-8"))
        self.repository = repository
        self.validator = EconomyValidator(
            self.reference.scenario, repository, company_count=self.reference.company_count
        )
        candidates = [
            run
            for run in self.reference.runs
            if run.seed == 7 and run.focal_policy is PolicyName.LEAN
        ]
        if len(candidates) != 1 or not self.reference.all_requested_runs_completed:
            raise ValueError("reference must contain exactly one completed seed-7 lean run")
        self.baseline = candidates[0]
        if self.validator._source_hashes() != self.reference.source_sha256:
            raise ValueError("frozen reference sources differ from current sources")
        if (
            canonical_model_hash(self.validator.parameters)
            != self.reference.policy_parameters_sha256
        ):
            raise ValueError("frozen reference policy parameters differ")

    def run(
        self, tiers: tuple[Literal[1, 2], ...], *, workers: int, max_seconds: float
    ) -> InnovationFollowupReport:
        started = time.monotonic()
        hashes = self._source_hashes()
        jobs = [(self, tier, started + max_seconds) for tier in tiers]
        if workers == 1:
            outcomes = tuple(_run_tier(job) for job in jobs)
        else:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                outcomes = tuple(executor.map(_run_tier, jobs))
        if hashes != self._source_hashes():
            raise RuntimeError("sources changed during exploratory validation")
        return InnovationFollowupReport(
            baseline_receipt_sha256=hashlib.sha256(self.reference_path.read_bytes()).hexdigest(),
            baseline_run=self.baseline,
            scenario_sha256=self.reference.scenario_sha256,
            source_sha256=hashes,
            policy_parameters_sha256=self.reference.policy_parameters_sha256,
            generated_at=datetime.now(UTC).isoformat(),
            elapsed_seconds=time.monotonic() - started,
            workers=workers,
            outcomes=outcomes,
        )

    async def run_tier(self, tier: Literal[1, 2], deadline: float) -> InnovationOutcome:
        scenario = self.reference.scenario.model_copy(update={"seed": 7})
        world = MarketWorld(scenario)
        state = world.create_initial_state(
            tuple(f"company-{index + 1}" for index in range(self.reference.company_count))
        )
        observations = world.project_observations(state)
        focal, background = LeanInnovationPolicy(tier), DiagnosticPolicy(PolicyName.RULE)
        next_progress = 100
        started = time.monotonic()
        # Kept local so the previously frozen validation script and receipts remain untouched.
        while state.day < scenario.total_days:
            if time.monotonic() > deadline:
                raise TimeoutError("exploratory diagnostic time budget exhausted")
            decisions = {
                company_id: await (
                    focal if company_id == EconomyValidator.FOCAL_COMPANY else background
                ).decide(observation)
                for company_id, observation in observations.items()
                if observation.company.status is CompanyStatus.ACTIVE
            }
            if not decisions:
                break
            window = world.advance_window(state, decisions)
            state, observations = window.state, window.observations
            if state.day >= next_progress:
                print(
                    f"followup tier={tier} day={state.day}/{scenario.total_days} "
                    f"elapsed={time.monotonic() - started:.1f}s",
                    flush=True,
                )
                next_progress = (state.day // 100 + 1) * 100
        run = ValidationRun(
            seed=7,
            focal_policy=PolicyName.LEAN,
            terminal_day=state.day,
            scenario_sha256=canonical_model_hash(scenario),
            final_state_sha256=world.state_hash(state),
            system_score_cents=sum(
                item.official_score_cents for item in score_world(state, scenario).values()
            ),
            companies=tuple(
                self.validator._company_result(company, scenario)
                for company in state.companies.values()
            ),
        )
        focal_score = next(
            item.official_score_cents
            for item in run.companies
            if item.company_id == EconomyValidator.FOCAL_COMPANY
        )
        baseline_score = next(
            item.official_score_cents
            for item in self.baseline.companies
            if item.company_id == EconomyValidator.FOCAL_COMPANY
        )
        print(
            f"followup tier={tier} score={focal_score / 100:.2f} "
            f"difference={(focal_score - baseline_score) / 100:.2f}",
            flush=True,
        )
        return InnovationOutcome(
            tier=tier, run=run, focal_score_difference_cents=focal_score - baseline_score
        )

    def _source_hashes(self) -> dict[str, str]:
        name = "scripts/validate_innovation_followup.py"
        return {
            **self.validator._source_hashes(),
            name: hashlib.sha256((self.repository / name).read_bytes()).hexdigest(),
        }


def _run_tier(job: tuple[InnovationFollowup, Literal[1, 2], float]) -> InnovationOutcome:
    experiment, tier, deadline = job
    return asyncio.run(experiment.run_tier(tier, deadline))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=Path("docs/validation/v3-seed7-policies.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("docs/validation/v3-innovation-followup.json")
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-seconds", type=float, default=600)
    args = parser.parse_args()
    if args.workers < 1 or args.max_seconds <= 0:
        parser.error("workers and max-seconds must be positive")
    experiment = InnovationFollowup(args.reference, Path(__file__).resolve().parents[1])
    report = experiment.run((1, 2), workers=args.workers, max_seconds=args.max_seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
