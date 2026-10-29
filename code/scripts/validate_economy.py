"""Run public-observation scripted-policy diagnostics without calling a model provider."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from statistics import mean

from pydantic import Field

from ceo_arena.config import default_scenario, load_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.evaluation import official_score, score_world
from ceo_arena.harness.rule_ceo import RuleCeoHarness
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import (
    CapacityActionQuote,
    CompanyDecision,
    CompanyObservation,
    CompanyState,
    CompanyStatus,
    DecisionWindowRequest,
    ExpenseBreakdown,
    ExpenseCategory,
    MarketingAllocation,
    ProductDecision,
    ScenarioConfig,
    StrictModel,
)
from ceo_arena.runtime.context import _issue_company_context

DEVELOPMENT_SEEDS = (7, 19, 42)
VALIDATION_SEEDS = (73, 101)


class PolicyName(StrEnum):
    RULE = "rule"
    NO_ACTION = "no_action"
    LEAN = "lean"
    ADAPTIVE = "adaptive"
    RECKLESS = "reckless"
    RULE_NO_RD = "rule_no_rd"
    RULE_INNOVATION = "rule_innovation"
    RULE_EXPAND = "rule_expand"


class PolicyParameters(StrictModel):
    """Predeclared diagnostic heuristics; not fitted to any evaluated LLM score."""

    support_cents_per_backlogged_ticket: int = 500
    support_daily_cap_cents: int = 10_000
    margin_target: float = Field(default=0.35, gt=0.0, lt=1.0)
    inventory_safety_days: int = 3
    adaptive_marketing_fraction_of_gross: float = 0.05
    adaptive_marketing_floor_cents: int = 6_000
    adaptive_marketing_cap_cents: int = 12_000
    adaptive_development_fraction_of_gross: float = 0.06
    adaptive_development_cap_cents: int = 8_000
    development_quality_index_trigger: float = 1.03
    investment_return_buffer: float = 1.5
    expansion_utilization_trigger: float = 0.9
    reckless_marketing_total_cents: int = 60_000
    reckless_development_total_cents: int = 30_000
    reckless_operations_daily_cents: int = 30_000


class DiagnosticPolicy:
    """Modify the original Rule using only the same company-local observation.

    These intentionally simple policies diagnose mechanisms, not model ability or
    optimal control. Rule remains unchanged. Every background company decides
    again in each world; no historical opponent actions are replayed.
    """

    def __init__(self, name: PolicyName, parameters: PolicyParameters | None = None) -> None:
        self.name = name
        self.parameters = parameters or PolicyParameters()
        self._rule = RuleCeoHarness()

    async def decide(self, observation: CompanyObservation) -> CompanyDecision:
        if self.name is PolicyName.NO_ACTION:
            return CompanyDecision(
                company_id=observation.company.company_id, world_version=observation.world_version
            )
        request = DecisionWindowRequest(
            run_id="model-free-diagnostic",
            company_id=observation.company.company_id,
            world_version=observation.world_version,
            observation=observation,
        )
        with _issue_company_context(observation) as context:
            result = await self._rule.decide(request, context)
        if result.status != "sealed" or result.decision is None:
            raise RuntimeError(f"Rule failed to seal: {result.status}")
        decision = result.decision
        if self.name is PolicyName.RULE:
            return decision
        if self.name is PolicyName.RULE_INNOVATION:
            project = next(
                (item for item in observation.innovation_catalog if item.tier == 2), None
            )
            if (
                observation.day == 0
                and project
                and project.cost_cents <= observation.company.cash_cents // 3
            ):
                return decision.model_copy(update={"research_project_tier": 2})
            return decision
        if self.name is PolicyName.RULE_EXPAND:
            quote = next(
                (
                    item
                    for item in observation.capacity_action_quotes
                    if item.action == "expand_capacity" and item.charged_cents > 0
                ),
                None,
            )
            if (
                observation.day == 0
                and quote
                and quote.charged_cents <= observation.company.cash_cents // 3
            ):
                return self._with_capacity(decision, quote)
            return decision
        if self.name is PolicyName.RECKLESS:
            return self._reckless(observation, decision)
        products = tuple(
            item.model_copy(update={"development_budget_cents": 0}) for item in decision.products
        )
        decision = decision.model_copy(update={"products": products})
        if self.name is PolicyName.RULE_NO_RD:
            return decision
        support = min(
            self.parameters.support_daily_cap_cents,
            observation.company.support_backlog
            * self.parameters.support_cents_per_backlogged_ticket,
        )
        decision = decision.model_copy(update={"support_budget_cents": support})
        return (
            self._adaptive(observation, decision) if self.name is PolicyName.ADAPTIVE else decision
        )

    def _adaptive(
        self, observation: CompanyObservation, decision: CompanyDecision
    ) -> CompanyDecision:
        company = observation.company
        history = company.daily_history[-14:]
        gross = mean(item.gross_profit_cents for item in history) if history else 0.0
        cash = max(0, company.cash_cents)
        development = (
            min(
                self.parameters.adaptive_development_cap_cents,
                int(max(0.0, gross) * self.parameters.adaptive_development_fraction_of_gross),
            )
            if observation.days_remaining > 90
            and observation.published_market_signal.quality_expectation_index
            >= self.parameters.development_quality_index_trigger
            else 0
        )
        preferred = max(
            observation.product_catalog, key=lambda product: product.base_quality
        ).product_id
        products: list[ProductDecision] = []
        for product in observation.product_catalog:
            daily_sales = (
                mean(item.fulfilled_by_product.get(product.product_id, 0) for item in history)
                if history
                else 0.0
            )
            coverage = (
                product.procurement_lead_days
                + observation.days_per_decision_window
                + self.parameters.inventory_safety_days
            )
            target = (
                math.ceil(daily_sales * coverage)
                if history
                else company.reorder_targets[product.product_id]
            )
            target = min(
                target, cash // (2 * len(observation.product_catalog) * product.unit_cost_cents)
            )
            if observation.days_remaining <= product.procurement_lead_days:
                target = 0
            products.append(
                ProductDecision(
                    product_id=product.product_id,
                    price_cents=math.ceil(
                        product.unit_cost_cents / (1 - self.parameters.margin_target)
                    ),
                    promotion_cents=0,
                    reorder_target=target,
                    development_budget_cents=development if product.product_id == preferred else 0,
                )
            )
        marketing = min(
            self.parameters.adaptive_marketing_cap_cents,
            max(
                self.parameters.adaptive_marketing_floor_cents,
                int(max(0.0, gross) * self.parameters.adaptive_marketing_fraction_of_gross),
            ),
        )
        marketing = min(marketing, cash // 120)
        cells = [
            (segment, channel)
            for segment, channels in company.marketing_budget_cents.items()
            for channel in channels
        ]
        decision = decision.model_copy(
            update={
                "products": tuple(products),
                "marketing": tuple(
                    MarketingAllocation(
                        segment_id=segment,
                        channel=channel,
                        daily_budget_cents=marketing // max(1, len(cells)),
                    )
                    for segment, channel in sorted(cells)
                ),
            }
        )
        quote = self._capacity_choice(observation, gross)
        return self._with_capacity(decision, quote) if quote else decision

    def _capacity_choice(
        self, observation: CompanyObservation, daily_gross: float
    ) -> CapacityActionQuote | None:
        history = observation.company.daily_history[-14:]
        if len(history) < 14:
            return None
        catalog = {item.tier: item for item in observation.capacity_catalog}
        current = catalog[observation.company.capacity_tier]
        sold = mean(item.fulfilled_units for item in history)
        utilization = sold / current.units_per_day
        backlog = sum(item.quantity for item in observation.company.backlog)
        for quote in observation.capacity_action_quotes:
            if (
                quote.action == "cancel_capacity_project"
                and quote.refunded_cents > 0
                and utilization < 0.45
            ):
                return quote
            if quote.target_tier is None or quote.charged_cents <= 0:
                continue
            target = catalog[quote.target_tier]
            usable_days = observation.total_days - (quote.effective_day or observation.day)
            if usable_days <= 45 or observation.company.cash_cents < quote.charged_cents * 4:
                continue
            extra_daily_cost = target.daily_cost_cents - current.daily_cost_cents
            if (
                quote.action == "downsize_capacity"
                and utilization < 0.4
                and target.units_per_day >= sold * 2
                and -extra_daily_cost * usable_days > quote.charged_cents
            ):
                return quote
            if (
                quote.action != "expand_capacity"
                or utilization < self.parameters.expansion_utilization_trigger
                or backlog < current.units_per_day // 2
                or sold <= 0
            ):
                continue
            extra_units = min(target.units_per_day - current.units_per_day, backlog / 14)
            expected_margin = extra_units * max(0.0, daily_gross) / sold * usable_days
            commitment = quote.charged_cents + extra_daily_cost * usable_days
            if expected_margin >= self.parameters.investment_return_buffer * commitment:
                return quote
        return None

    def _reckless(
        self, observation: CompanyObservation, decision: CompanyDecision
    ) -> CompanyDecision:
        count = max(1, len(decision.products))
        development = self.parameters.reckless_development_total_cents // count
        decision = decision.model_copy(
            update={
                "products": tuple(
                    item.model_copy(
                        update={
                            "development_budget_cents": development,
                            "reorder_target": max(
                                observation.company.reorder_targets[item.product_id],
                                observation.company.inventory[item.product_id] * 4,
                            ),
                        }
                    )
                    for item in decision.products
                ),
                "marketing": tuple(
                    item.model_copy(
                        update={
                            "daily_budget_cents": self.parameters.reckless_marketing_total_cents
                            // max(1, len(decision.marketing))
                        }
                    )
                    for item in decision.marketing
                ),
                "support_budget_cents": self.parameters.reckless_operations_daily_cents,
                "reliability_budget_cents": self.parameters.reckless_operations_daily_cents,
            }
        )
        if observation.day == 0:
            affordable = [
                item
                for item in observation.capacity_action_quotes
                if item.action == "expand_capacity"
                and 0 < item.charged_cents <= observation.company.cash_cents // 3
            ]
            if affordable:
                decision = self._with_capacity(
                    decision, max(affordable, key=lambda item: item.target_tier or 0)
                )
        return decision

    @staticmethod
    def _with_capacity(decision: CompanyDecision, quote: CapacityActionQuote) -> CompanyDecision:
        return decision.model_copy(
            update={
                "capacity_target_tier": quote.target_tier
                if quote.action == "expand_capacity"
                else None,
                "capacity_downsize_tier": quote.target_tier
                if quote.action == "downsize_capacity"
                else None,
                "cancel_capacity_project_id": quote.project_id
                if quote.action == "cancel_capacity_project"
                else None,
            }
        )


class CompanyDiagnostic(StrictModel):
    company_id: str
    status: CompanyStatus
    official_score_cents: int
    sales_revenue_cents: int
    cash_payments_cents: int
    refunds_cents: int
    cost_of_goods_sold_cents: int
    gross_profit_cents: int
    gross_margin: float | None
    terminal_cash_cents: int
    inventory_salvage_cents: int
    fulfilled_units: int
    expense_breakdown: ExpenseBreakdown
    cash_identity_holds: bool


class ValidationRun(StrictModel):
    seed: int
    focal_policy: PolicyName
    terminal_day: int
    scenario_sha256: str
    final_state_sha256: str
    system_score_cents: int
    companies: tuple[CompanyDiagnostic, ...]


class ValidationReport(StrictModel):
    scenario: ScenarioConfig
    scenario_sha256: str
    source_sha256: dict[str, str]
    policy_parameters: PolicyParameters
    policy_parameters_sha256: str
    rule_harness_id: str
    generated_at: str
    elapsed_seconds: float = Field(ge=0.0)
    company_count: int
    workers: int = Field(default=1, ge=1)
    seeds_requested: tuple[int, ...]
    policies_requested: tuple[PolicyName, ...]
    split: str
    scope: str
    runs: tuple[ValidationRun, ...]
    all_requested_runs_completed: bool
    model_api_calls: int = Field(default=0, ge=0, le=0)


class EconomyValidator:
    """Run fresh matched worlds; refuse mixed-source reports if implementation changes."""

    FOCAL_COMPANY = "company-1"
    SOURCE_PATHS = (
        "src/ceo_arena/config.py",
        "src/ceo_arena/models.py",
        "src/ceo_arena/protocol.py",
        "src/ceo_arena/hashing.py",
        "src/ceo_arena/economy/world.py",
        "src/ceo_arena/economy/market.py",
        "src/ceo_arena/economy/capacity.py",
        "src/ceo_arena/economy/randomness.py",
        "src/ceo_arena/evaluation/scoring.py",
        "src/ceo_arena/harness/rule_ceo.py",
        "src/ceo_arena/harness/draft.py",
        "src/ceo_arena/runtime/context.py",
        "scripts/validate_economy.py",
    )

    def __init__(
        self, scenario: ScenarioConfig, repository: Path, *, company_count: int = 9
    ) -> None:
        if company_count < 2:
            raise ValueError("diagnostics require at least two companies")
        self.scenario = scenario
        self.repository = repository
        self.company_count = company_count
        self.parameters = PolicyParameters()

    async def run(
        self,
        seeds: tuple[int, ...],
        policies: tuple[PolicyName, ...],
        *,
        split: str = "development",
        max_seconds: float = 900,
        workers: int = 1,
    ) -> ValidationReport:
        if max_seconds <= 0:
            raise ValueError("validation time budget must be positive")
        if workers < 1:
            raise ValueError("workers must be positive")
        if (
            not seeds
            or not policies
            or len(set(seeds)) != len(seeds)
            or len(set(policies)) != len(policies)
        ):
            raise ValueError("seeds and policies must be nonempty and unique")
        hashes = self._source_hashes()
        started_at = time.monotonic()
        deadline = started_at + max_seconds
        runs: list[ValidationRun] = []
        cases = [(seed, policy) for seed in seeds for policy in policies]
        if workers > 1:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(_run_case, self, seed, policy, deadline)
                    for seed, policy in cases
                ]
                for future in as_completed(futures):
                    try:
                        run = future.result()
                    except TimeoutError:
                        continue
                    runs.append(run)
                    self._print_run(run)
        else:
            for seed, policy in cases:
                try:
                    run = await self.run_one(seed, policy, deadline=deadline)
                except TimeoutError:
                    break
                runs.append(run)
                self._print_run(run)
        runs.sort(key=lambda run: cases.index((run.seed, run.focal_policy)))
        if hashes != self._source_hashes():
            raise RuntimeError("economic source changed during validation; discard and rerun")
        return ValidationReport(
            scenario=self.scenario,
            scenario_sha256=canonical_model_hash(self.scenario),
            source_sha256=hashes,
            policy_parameters=self.parameters,
            policy_parameters_sha256=canonical_model_hash(self.parameters),
            rule_harness_id=RuleCeoHarness().harness_id,
            generated_at=datetime.now(UTC).isoformat(),
            elapsed_seconds=time.monotonic() - started_at,
            company_count=self.company_count,
            workers=workers,
            seeds_requested=seeds,
            policies_requested=policies,
            split=split,
            scope=(
                "One focal scripted policy against unchanged Rule backgrounds; fresh closed-loop "
                "worlds with common seeds. Public CompanyObservation only. Development policies "
                "are illustrative, not optimal and not LLM results. Validation seeds 73/101 are "
                "reserved from v3 tuning, but appeared in historical v2 calibration; they are not "
                "globally unseen. No score-sign or model-ranking acceptance target."
            ),
            runs=tuple(runs),
            all_requested_runs_completed=len(runs) == len(seeds) * len(policies),
        )

    @classmethod
    def _print_run(cls, run: ValidationRun) -> None:
        focal = next(
            company for company in run.companies if company.company_id == cls.FOCAL_COMPANY
        )
        print(
            f"seed={run.seed} policy={run.focal_policy} day={run.terminal_day} "
            f"score={focal.official_score_cents / 100:.2f} status={focal.status}",
            flush=True,
        )

    async def run_one(
        self, seed: int, policy: PolicyName, *, deadline: float = math.inf
    ) -> ValidationRun:
        scenario = self.scenario.model_copy(update={"seed": seed})
        world = MarketWorld(scenario)
        state = world.create_initial_state(
            tuple(f"company-{index + 1}" for index in range(self.company_count))
        )
        focal = DiagnosticPolicy(policy, self.parameters)
        background = DiagnosticPolicy(PolicyName.RULE, self.parameters)
        observations = world.project_observations(state)
        started_at = time.monotonic()
        next_progress_day = 100
        while state.day < scenario.total_days:
            if time.monotonic() > deadline:
                raise TimeoutError("model-free validation time budget exhausted")
            decisions = {
                company_id: await (
                    focal if company_id == self.FOCAL_COMPANY else background
                ).decide(observation)
                for company_id, observation in observations.items()
                if observation.company.status is CompanyStatus.ACTIVE
            }
            if not decisions:
                break
            window = world.advance_window(state, decisions)
            state, observations = window.state, window.observations
            if state.day >= next_progress_day:
                print(
                    f"progress seed={seed} policy={policy} day={state.day}/{scenario.total_days} "
                    f"elapsed={time.monotonic() - started_at:.1f}s",
                    flush=True,
                )
                next_progress_day = (state.day // 100 + 1) * 100
        return ValidationRun(
            seed=seed,
            focal_policy=policy,
            terminal_day=state.day,
            scenario_sha256=canonical_model_hash(scenario),
            final_state_sha256=world.state_hash(state),
            system_score_cents=sum(
                score.official_score_cents for score in score_world(state, scenario).values()
            ),
            companies=tuple(
                self._company_result(company, scenario) for company in state.companies.values()
            ),
        )

    @staticmethod
    def _company_result(company: CompanyState, scenario: ScenarioConfig) -> CompanyDiagnostic:
        score = official_score(company, scenario)
        expenses = ExpenseBreakdown()
        for record in company.daily_history:
            for category in ExpenseCategory:
                expenses.add(category, getattr(record.expense_breakdown, category.value))
        cogs = sum(record.cogs_cents for record in company.daily_history)
        revenue, paid, refunds = (
            company.cumulative_revenue_cents,
            company.cumulative_cost_cents,
            company.cumulative_refunds_cents,
        )
        return CompanyDiagnostic(
            company_id=company.company_id,
            status=company.status,
            official_score_cents=score.official_score_cents,
            sales_revenue_cents=revenue,
            cash_payments_cents=paid,
            refunds_cents=refunds,
            cost_of_goods_sold_cents=cogs,
            gross_profit_cents=revenue - cogs,
            gross_margin=(revenue - cogs) / revenue if revenue else None,
            terminal_cash_cents=company.cash_cents,
            inventory_salvage_cents=score.diagnostics.inventory_salvage_cents,
            fulfilled_units=company.cumulative_fulfilled_units,
            expense_breakdown=expenses,
            cash_identity_holds=company.cash_cents
            == company.initial_cash_cents + revenue + refunds - paid,
        )

    def _source_hashes(self) -> dict[str, str]:
        return {
            name: hashlib.sha256((self.repository / name).read_bytes()).hexdigest()
            for name in self.SOURCE_PATHS
        }


def _run_case(
    validator: EconomyValidator, seed: int, policy: PolicyName, deadline: float
) -> ValidationRun:
    """Isolate each model-free world in a spawn-safe worker process."""
    return asyncio.run(validator.run_one(seed, policy, deadline=deadline))


def argument_parser() -> argparse.ArgumentParser:
    """Expose diagnostic defaults without starting worlds or writing reports."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path)
    parser.add_argument("--days", type=int, default=500)
    parser.add_argument("--companies", type=int, default=9)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--split", choices=("development", "validation", "all"), default="development"
    )
    parser.add_argument(
        "--seeds", nargs="+", type=int, help="Explicit diagnostic subset; recorded as custom"
    )
    parser.add_argument(
        "--policies",
        nargs="+",
        choices=tuple(PolicyName),
        default=["rule", "lean", "adaptive", "reckless"],
    )
    parser.add_argument("--max-seconds", type=float, default=900)
    parser.add_argument("--output", type=Path, default=Path(".tmp/economy-validation.json"))
    return parser


def main() -> None:
    parser = argument_parser()
    args = parser.parse_args()
    if args.days < 1 or args.max_seconds <= 0 or args.workers < 1:
        parser.error("days, workers, and max-seconds must be positive")
    scenario = load_scenario(args.scenario) if args.scenario else default_scenario()
    scenario = scenario.model_copy(update={"total_days": args.days})
    seeds = (
        DEVELOPMENT_SEEDS
        if args.split == "development"
        else VALIDATION_SEEDS
        if args.split == "validation"
        else DEVELOPMENT_SEEDS + VALIDATION_SEEDS
    )
    validator = EconomyValidator(
        scenario, Path(__file__).resolve().parents[1], company_count=args.companies
    )
    report = asyncio.run(
        validator.run(
            tuple(args.seeds or seeds),
            tuple(PolicyName(name) for name in args.policies),
            split="custom" if args.seeds else args.split,
            max_seconds=args.max_seconds,
            workers=args.workers,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if not report.all_requested_runs_completed:
        raise SystemExit("Time limit reached; report explicitly records partial completion.")


if __name__ == "__main__":
    main()
