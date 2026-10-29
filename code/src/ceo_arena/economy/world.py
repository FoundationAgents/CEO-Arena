"""Shared, deterministic retail economy for Multi-CEO evaluation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import (
    ActionReceipt,
    BacklogOrder,
    CapacityChangeKind,
    CompanyDailyRecord,
    CompanyDayOutcome,
    CompanyDecision,
    CompanyObservation,
    CompanyState,
    CompanyStatus,
    CompanyTenderInfo,
    DayResult,
    EnterpriseTender,
    ExpenseBreakdown,
    ExpenseCategory,
    InboundShipment,
    InnovationContribution,
    InnovationProject,
    InnovationProjectInfo,
    InnovationTierInfo,
    JobStatus,
    MarketResearchJob,
    NormalBelief,
    OrderSource,
    ProductConfig,
    ResearchLevelInfo,
    ResearchReport,
    ScenarioConfig,
    SegmentConfig,
    WindowResult,
    WorldState,
)

from .capacity import CapacityManager
from .market import MarketDynamics
from .randomness import NamedRandom

_LEGACY_TENDER_QUANTITY_FRACTIONS = (0.5, 1.5)
_REFERENCE_TENDER_QUANTITY_FRACTIONS = (0.15, 0.45)


@dataclass(frozen=True, order=True)
class ProductChoice:
    """One company-product alternative in the shared choice set."""

    company_id: str
    product_id: str


@dataclass(frozen=True)
class ChoiceShares:
    """Expected multinomial-logit shares, including the outside option."""

    product: dict[ProductChoice, float]
    outside: float

    @property
    def total(self) -> float:
        """Return the probability mass represented by this choice set."""
        return self.outside + sum(self.product.values())


@dataclass
class _OutcomeAccumulator:
    consumer_orders: int = 0
    fulfilled_units: int = 0
    abandoned_units: int = 0
    revenue_cents: int = 0
    costs_cents: int = 0
    expense_breakdown: ExpenseBreakdown = field(default_factory=ExpenseBreakdown)
    refunds_cents: int = 0
    cogs_cents: int = 0
    fulfilled_by_product: dict[str, int] = field(default_factory=dict)

    @classmethod
    def create(cls, product_ids: Sequence[str]) -> _OutcomeAccumulator:
        return cls(fulfilled_by_product={product_id: 0 for product_id in product_ids})


@dataclass
class _ServiceAccumulator:
    fulfilled_by_segment: dict[str, int]
    failures_by_segment: dict[str, int]
    quality_sum_by_segment: dict[str, float]
    price_sum_by_segment: dict[str, int]
    wait_days_sum_by_segment: dict[str, float]
    tickets: int = 0

    @classmethod
    def create(cls, segment_ids: Sequence[str]) -> _ServiceAccumulator:
        """Create zeroed service measurements for one company-day."""
        return cls(
            fulfilled_by_segment={segment_id: 0 for segment_id in segment_ids},
            failures_by_segment={segment_id: 0 for segment_id in segment_ids},
            quality_sum_by_segment={segment_id: 0.0 for segment_id in segment_ids},
            price_sum_by_segment={segment_id: 0 for segment_id in segment_ids},
            wait_days_sum_by_segment={segment_id: 0.0 for segment_id in segment_ids},
        )


class MarketWorld:
    """Own every mutation of the shared market and expose a small simulation interface."""

    _INITIAL_AWARENESS = 0.20
    _MIN_UTILITY_EXPONENT = -700.0

    def __init__(self, scenario: ScenarioConfig) -> None:
        self.config = scenario
        self._random = NamedRandom(scenario.seed)
        self._products = {product.product_id: product for product in scenario.products}
        self._segments = {segment.segment_id: segment for segment in scenario.segments}
        self._capacity_tiers = {tier.tier: tier for tier in scenario.capacity_tiers}
        self._capacity = CapacityManager(scenario)
        self._market = MarketDynamics(scenario)

    def create_initial_state(self, company_ids: Sequence[str]) -> WorldState:
        """Create a symmetric world; company labels do not affect initial endowments."""
        ordered_ids = sorted(company_ids)
        if not ordered_ids:
            raise ValueError("at least one company is required")
        if len(ordered_ids) != len(set(ordered_ids)):
            raise ValueError("company identifiers must be unique")

        companies = {company_id: self._initial_company(company_id) for company_id in ordered_ids}
        state = WorldState(
            scenario_id=self.config.scenario_id,
            macro_index=self.config.macro_mean,
            macro_history={0: self.config.macro_mean},
            companies=companies,
            initial_company_count=len(ordered_ids),
        )
        self._market.initialize(state)
        self._open_tender(state, opened_day=0)
        return state

    def project_observations(self, state: WorldState) -> dict[str, CompanyObservation]:
        """Project private company state plus public prices and sanitized open tenders."""
        self._validate_state(state)
        public_prices = {
            company_id: dict(state.companies[company_id].prices_cents)
            for company_id in sorted(state.companies)
            if state.companies[company_id].status is CompanyStatus.ACTIVE
        }
        public_promotions = {
            company_id: dict(state.companies[company_id].promotions_cents)
            for company_id in sorted(state.companies)
            if state.companies[company_id].status is CompanyStatus.ACTIVE
        }
        public_effective_prices = {
            company_id: {
                product_id: max(
                    0,
                    price - state.companies[company_id].promotions_cents[product_id],
                )
                for product_id, price in sorted(state.companies[company_id].prices_cents.items())
            }
            for company_id in sorted(state.companies)
            if state.companies[company_id].status is CompanyStatus.ACTIVE
        }
        open_tenders = tuple(
            tender.model_copy(update={"bids": {}}, deep=True)
            for tender in sorted(state.tenders, key=lambda item: item.tender_id)
            if not tender.settled and tender.deadline_day > state.day
        )
        published_day = max(0, state.day - self.config.macro_publication_delay_days)
        published_macro = state.macro_history.get(published_day, self.config.macro_mean)
        return {
            company_id: CompanyObservation(
                scenario_id=state.scenario_id,
                world_version=state.version,
                day=state.day,
                total_days=self.config.total_days,
                days_remaining=max(0, self.config.total_days - state.day),
                days_per_decision_window=self.config.days_per_decision_window,
                company=state.companies[company_id]
                .model_copy(
                    update={
                        "innovation_projects": [],
                        "research_jobs": [
                            job.model_copy(update={"snapshot": {}})
                            for job in state.companies[company_id].research_jobs
                        ],
                    },
                )
                .detached_copy(),
                public_prices_cents={key: dict(value) for key, value in public_prices.items()},
                public_promotions_cents={
                    key: dict(value) for key, value in public_promotions.items()
                },
                public_effective_prices_cents={
                    key: dict(value) for key, value in public_effective_prices.items()
                },
                open_tenders=open_tenders,
                published_macro_index=published_macro,
                published_market_signal=self._market.public_signal(state),
                capacity_policy=self.config.capacity_policy.model_copy(),
                capacity_action_quotes=self._capacity.quotes(
                    state.companies[company_id], state.day
                ),
                product_catalog=tuple(
                    self._products[key].model_copy() for key in sorted(self._products)
                ),
                capacity_catalog=tuple(
                    self._capacity_tiers[key].model_copy() for key in sorted(self._capacity_tiers)
                ),
                research_catalog=tuple(
                    ResearchLevelInfo(
                        level=level,
                        cost_cents=self.config.research.level_cost_cents[level],
                        delay_days=self.config.research.level_delay_days[level],
                        signal_std=self.config.research.level_signal_std[level],
                    )
                    for level in sorted(self.config.research.level_cost_cents)
                ),
                innovation_catalog=tuple(
                    InnovationTierInfo(
                        tier=tier,
                        cost_cents=self.config.innovation.project_cost_cents[tier],
                        median_days=self.config.innovation.project_median_days[tier],
                        expected_capital_gain_cents=(
                            self.config.innovation.project_capital_gain_cents[tier]
                        ),
                    )
                    for tier in sorted(self.config.innovation.project_cost_cents)
                ),
                innovation_projects=tuple(
                    InnovationProjectInfo(
                        project_id=project.project_id,
                        tier=project.tier,
                        started_day=project.started_day,
                        median_completion_day=(
                            project.started_day
                            + self.config.innovation.project_median_days[project.tier]
                        ),
                        expected_capital_gain_cents=(
                            self.config.innovation.project_capital_gain_cents[project.tier]
                        ),
                        status=project.status,
                        completed_day=(
                            project.completion_day
                            if project.status is JobStatus.COMPLETED
                            else None
                        ),
                        realized_capital_gain_cents=(
                            project.capital_gain_cents
                            if project.status is JobStatus.COMPLETED
                            else None
                        ),
                    )
                    for project in sorted(
                        state.companies[company_id].innovation_projects,
                        key=lambda item: item.project_id,
                    )
                ),
                tender_history=tuple(
                    self._company_tender_info(tender, company_id)
                    for tender in sorted(state.tenders, key=lambda item: item.tender_id)
                ),
            )
            for company_id in sorted(state.companies)
        }

    @staticmethod
    def _company_tender_info(
        tender: EnterpriseTender,
        company_id: str,
    ) -> CompanyTenderInfo:
        own_bid = tender.bids.get(company_id)
        if not tender.settled:
            status = "submitted" if own_bid is not None else "open"
        elif tender.winner_company_id == company_id:
            status = "won"
        elif own_bid is not None:
            status = "lost"
        else:
            status = "no_bid"
        return CompanyTenderInfo(
            tender_id=tender.tender_id,
            product_id=tender.product_id,
            quantity=tender.quantity,
            opened_day=tender.opened_day,
            deadline_day=tender.deadline_day,
            delivery_due_day=tender.delivery_due_day,
            own_bid_cents=own_bid,
            status=status,
            winning_unit_price_cents=(tender.winning_unit_price_cents if tender.settled else None),
        )

    def expected_choice_shares(
        self,
        state: WorldState,
        segment_id: str,
        *,
        excluded: frozenset[tuple[str, str]] = frozenset(),
    ) -> ChoiceShares:
        """Return finite-market MNL shares for all currently available offers."""
        self._validate_state(state)
        self._require_segment(segment_id)
        segment = self._market.segment(state, segment_id)
        utilities: dict[ProductChoice, float] = {}
        for company_id in sorted(state.companies):
            company = state.companies[company_id]
            if company.status is not CompanyStatus.ACTIVE:
                continue
            for product_id in sorted(self._products):
                if (company_id, product_id) in excluded or company.inventory[product_id] <= 0:
                    continue
                utilities[ProductChoice(company_id, product_id)] = self._choice_utility(
                    company,
                    self._products[product_id],
                    segment,
                )
        return self._softmax_shares(utilities, segment.outside_utility)

    def advance_window(
        self,
        state: WorldState,
        decisions: Mapping[str, CompanyDecision] | Sequence[CompanyDecision],
    ) -> WindowResult:
        """Apply one sealed decision set and atomically return the next world snapshot."""
        self._validate_state(state)
        if state.day >= self.config.total_days:
            raise ValueError("the scenario has already reached its terminal day")

        next_state = state.detached_copy()
        sealed = self._normalize_decisions(next_state, decisions)
        upfront_costs = self._apply_decisions(next_state, sealed)
        start_day = next_state.day
        stop_day = min(self.config.total_days, start_day + self.config.days_per_decision_window)
        days: list[DayResult] = []

        while next_state.day < stop_day:
            day_costs = upfront_costs if next_state.day == start_day else {}
            days.append(self._advance_day(next_state, day_costs))

        observations = self.project_observations(next_state)
        return WindowResult(
            start_day=start_day,
            end_day=next_state.day,
            world_version=next_state.version,
            days=tuple(days),
            observations=observations,
            state=next_state,
        )

    def state_hash(self, state: WorldState) -> str:
        """Hash a canonical JSON representation for replay and permutation checks."""
        return canonical_model_hash(state)

    def validate_decision(self, state: WorldState, decision: CompanyDecision) -> None:
        """Validate one decision against a frozen world without mutating it."""

        company = state.companies.get(decision.company_id)
        if company is None or company.status is not CompanyStatus.ACTIVE:
            raise ValueError("decision company is not active in this world")
        if decision.world_version != state.version:
            raise ValueError("decision targets a stale world version")
        trial = state.model_copy(
            update={
                "companies": {decision.company_id: company.detached_copy()},
                "tenders": [tender.model_copy(deep=True) for tender in state.tenders],
            }
        )
        self._apply_decisions(trial, {decision.company_id: decision.model_copy(deep=True)})

    def _initial_company(self, company_id: str) -> CompanyState:
        product_ids = sorted(self._products)
        segment_ids = sorted(self._segments)
        goodwill = -math.log1p(-self._INITIAL_AWARENESS)
        demand_prior = math.exp(
            sum(math.log(self._segments[key].base_daily_demand) for key in segment_ids)
            / len(segment_ids)
        )
        price_weight_prior = math.exp(
            sum(math.log(self._segments[key].price_weight) for key in segment_ids)
            / len(segment_ids)
        )
        initial_enterprise_value = self.config.initial_cash_cents + sum(
            self._products[product_id].initial_inventory
            * self._products[product_id].salvage_value_cents
            for product_id in product_ids
        )
        beliefs = {
            segment_id: {
                "base_daily_demand": NormalBelief(
                    mean=demand_prior,
                    variance=(demand_prior * self.config.research.prior_std) ** 2,
                    updated_day=0,
                ),
                "price_weight": NormalBelief(
                    mean=price_weight_prior,
                    variance=(price_weight_prior * self.config.research.prior_std) ** 2,
                    updated_day=0,
                ),
                "expected_quality": NormalBelief(mean=0.7, variance=0.16, updated_day=0),
            }
            for segment_id in segment_ids
        }
        return CompanyState(
            company_id=company_id,
            cash_cents=self.config.initial_cash_cents,
            initial_cash_cents=self.config.initial_cash_cents,
            initial_enterprise_value_cents=initial_enterprise_value,
            prices_cents={
                product_id: self._products[product_id].initial_price_cents
                for product_id in product_ids
            },
            promotions_cents={product_id: 0 for product_id in product_ids},
            reorder_targets={
                product_id: self._products[product_id].initial_inventory
                for product_id in product_ids
            },
            inventory={
                product_id: self._products[product_id].initial_inventory
                for product_id in product_ids
            },
            reserved_inventory={product_id: 0 for product_id in product_ids},
            marketing_budget_cents={
                segment_id: {channel: 0 for channel in self.config.channels}
                for segment_id in segment_ids
            },
            goodwill={segment_id: goodwill for segment_id in segment_ids},
            awareness={segment_id: self._INITIAL_AWARENESS for segment_id in segment_ids},
            satisfaction={segment_id: 0.0 for segment_id in segment_ids},
            loyalty={segment_id: 0.0 for segment_id in segment_ids},
            development_budget_cents={product_id: 0 for product_id in product_ids},
            knowledge_capital_cents={product_id: 0.0 for product_id in product_ids},
            capacity_tier=self.config.initial_capacity_tier,
            beliefs=beliefs,
        )

    def _validate_state(self, state: WorldState) -> None:
        if state.scenario_id != self.config.scenario_id:
            raise ValueError("world scenario does not match MarketWorld configuration")
        for company_id, company in state.companies.items():
            if company.company_id != company_id:
                raise ValueError("company dictionary key does not match company state")

    def _normalize_decisions(
        self,
        state: WorldState,
        decisions: Mapping[str, CompanyDecision] | Sequence[CompanyDecision],
    ) -> dict[str, CompanyDecision]:
        if isinstance(decisions, Mapping):
            sealed = dict(decisions)
        else:
            sealed = {}
            for decision in decisions:
                if decision.company_id in sealed:
                    raise ValueError(f"duplicate decision for {decision.company_id}")
                sealed[decision.company_id] = decision

        active = {
            company_id
            for company_id, company in state.companies.items()
            if company.status is CompanyStatus.ACTIVE
        }
        if set(sealed) != active:
            missing = sorted(active - set(sealed))
            extra = sorted(set(sealed) - active)
            raise ValueError(
                f"decision set must match active companies; missing={missing}, extra={extra}"
            )
        for company_id, decision in sealed.items():
            if decision.company_id != company_id:
                raise ValueError("decision dictionary key does not match company_id")
            if decision.world_version != state.version:
                raise ValueError(
                    f"stale decision for {company_id}: expected world version {state.version}"
                )
        return sealed

    def _apply_decisions(
        self,
        state: WorldState,
        decisions: Mapping[str, CompanyDecision],
    ) -> dict[str, _OutcomeAccumulator]:
        upfront_costs: dict[str, _OutcomeAccumulator] = {}
        for company_id in sorted(decisions):
            company = state.companies[company_id]
            decision = decisions[company_id]
            self._apply_product_decisions(company, decision)
            self._apply_marketing_decisions(company, decision)
            if decision.reliability_budget_cents is not None:
                company.reliability_budget_cents = decision.reliability_budget_cents
            if decision.support_budget_cents is not None:
                company.support_budget_cents = decision.support_budget_cents
            costs = _OutcomeAccumulator.create(sorted(self._products))
            upfront_costs[company_id] = costs
            innovation_cost = self._start_innovation_project(state, company, decision)
            capacity_flow = self._capacity.apply(company, decision, state.day)
            research_cost = self._start_research_jobs(state, company, decision)
            total = innovation_cost + capacity_flow.charged_cents + research_cost
            if total > company.cash_cents + capacity_flow.refunded_cents:
                raise ValueError(
                    "one-time commitments exceed available cash; no credit is provided"
                )
            for category, amount in (
                (ExpenseCategory.INNOVATION, innovation_cost),
                (
                    ExpenseCategory.CAPACITY_ADJUSTMENT
                    if capacity_flow.kind is CapacityChangeKind.DOWNSIZE
                    else ExpenseCategory.CAPACITY_INVESTMENT,
                    capacity_flow.charged_cents,
                ),
                (ExpenseCategory.MARKET_RESEARCH, research_cost),
            ):
                self._charge(company, costs, amount, category)
            if capacity_flow.refunded_cents:
                company.cash_cents += capacity_flow.refunded_cents
                company.cumulative_refunds_cents += capacity_flow.refunded_cents
                costs.refunds_cents = capacity_flow.refunded_cents
            company.action_receipts.extend(capacity_flow.receipts)
            self._record_tender_bids(state, company, decision)
            self._decision_receipts(company, decision, state.day, innovation_cost, research_cost)
        return upfront_costs

    @staticmethod
    def _decision_receipts(
        company: CompanyState,
        decision: CompanyDecision,
        day: int,
        innovation_cost: int,
        research_cost: int,
    ) -> None:
        actions = (
            ("set_product", bool(decision.products), 0),
            ("set_marketing", bool(decision.marketing), 0),
            (
                "set_operations",
                decision.reliability_budget_cents is not None
                or decision.support_budget_cents is not None,
                0,
            ),
            ("start_research_project", decision.research_project_tier is not None, innovation_cost),
            ("request_market_research", bool(decision.market_research), research_cost),
            ("submit_enterprise_bid", bool(decision.enterprise_bids), 0),
        )
        for action, enabled, cents in actions:
            if enabled:
                company.action_receipts.append(
                    ActionReceipt(
                        day=day,
                        action=action,
                        status="applied",
                        charged_cents=cents,
                        message="Sealed request applied; recurring budgets continue until changed.",
                        effective_day=day + 1,
                    )
                )
        if not any(enabled for _, enabled, _ in actions) and all(
            value is None
            for value in (
                decision.capacity_target_tier,
                decision.capacity_downsize_tier,
                decision.cancel_capacity_project_id,
            )
        ):
            company.action_receipts.append(
                ActionReceipt(
                    day=day,
                    action="seal_decision",
                    status="no_op",
                    message="No operating changes; existing recurring policies remain active.",
                )
            )

    def _apply_product_decisions(self, company: CompanyState, decision: CompanyDecision) -> None:
        seen: set[str] = set()
        for product in decision.products:
            config = self._products.get(product.product_id)
            if config is None:
                raise ValueError(f"unknown product {product.product_id}")
            if product.product_id in seen:
                raise ValueError(f"duplicate product decision for {product.product_id}")
            seen.add(product.product_id)
            price = (
                product.price_cents
                if product.price_cents is not None
                else company.prices_cents[product.product_id]
            )
            promotion = (
                product.promotion_cents
                if product.promotion_cents is not None
                else company.promotions_cents[product.product_id]
            )
            if promotion > price:
                raise ValueError(f"promotion exceeds price for {product.product_id}")
            company.prices_cents[product.product_id] = price
            company.promotions_cents[product.product_id] = promotion
            if product.reorder_target is not None:
                company.reorder_targets[product.product_id] = product.reorder_target
            if product.development_budget_cents is not None:
                company.development_budget_cents[product.product_id] = (
                    product.development_budget_cents
                )

    def _apply_marketing_decisions(self, company: CompanyState, decision: CompanyDecision) -> None:
        seen: set[tuple[str, str]] = set()
        for allocation in decision.marketing:
            self._require_segment(allocation.segment_id)
            if allocation.channel not in self.config.channels:
                raise ValueError(f"unknown marketing channel {allocation.channel}")
            key = (allocation.segment_id, allocation.channel)
            if key in seen:
                raise ValueError(f"duplicate marketing allocation for {key}")
            seen.add(key)
            company.marketing_budget_cents[allocation.segment_id][allocation.channel] = (
                allocation.daily_budget_cents
            )

    def _start_innovation_project(
        self,
        state: WorldState,
        company: CompanyState,
        decision: CompanyDecision,
    ) -> int:
        tier = decision.research_project_tier
        if tier is None:
            return 0
        config = self.config.innovation
        rng = self._rng("innovation-project", company.company_id, state.day, tier)
        duration = max(
            1,
            round(
                config.project_median_days[tier]
                * float(rng.lognormal(0.0, config.project_duration_log_std))
            ),
        )
        gain_multiplier = float(
            rng.lognormal(
                -0.5 * config.project_gain_log_std**2,
                config.project_gain_log_std,
            )
        )
        project = InnovationProject(
            project_id=self._id("innovation-project", company.company_id, state.day, tier),
            tier=tier,
            started_day=state.day,
            completion_day=state.day + duration,
            capital_gain_cents=max(
                1, round(config.project_capital_gain_cents[tier] * gain_multiplier)
            ),
        )
        company.innovation_projects.append(project)
        return config.project_cost_cents[tier]

    def _start_research_jobs(
        self,
        state: WorldState,
        company: CompanyState,
        decision: CompanyDecision,
    ) -> int:
        total = 0
        seen: set[tuple[str, int]] = set()
        for request in decision.market_research:
            self._require_segment(request.segment_id)
            segment = self._market.segment(state, request.segment_id)
            key = (request.segment_id, request.level)
            if key in seen:
                raise ValueError(f"duplicate market research request for {key}")
            seen.add(key)
            company.research_jobs.append(
                MarketResearchJob(
                    job_id=self._id(
                        "research-job",
                        company.company_id,
                        state.day,
                        request.segment_id,
                        request.level,
                    ),
                    segment_id=request.segment_id,
                    level=request.level,
                    completion_day=state.day + self.config.research.level_delay_days[request.level],
                    snapshot_day=state.day,
                    snapshot={
                        "base_daily_demand": segment.base_daily_demand,
                        "price_weight": segment.price_weight,
                        "expected_quality": segment.expected_quality,
                    },
                )
            )
            total += self.config.research.level_cost_cents[request.level]
        return total

    def _record_tender_bids(
        self,
        state: WorldState,
        company: CompanyState,
        decision: CompanyDecision,
    ) -> None:
        tenders = {tender.tender_id: tender for tender in state.tenders}
        seen: set[str] = set()
        for bid in decision.enterprise_bids:
            if bid.tender_id in seen:
                raise ValueError(f"duplicate bid for tender {bid.tender_id}")
            seen.add(bid.tender_id)
            tender = tenders.get(bid.tender_id)
            if tender is None or tender.settled or tender.deadline_day <= state.day:
                raise ValueError(f"tender is not open: {bid.tender_id}")
            tender.bids[company.company_id] = bid.unit_price_cents

    def _advance_day(
        self, state: WorldState, upfront_costs: Mapping[str, _OutcomeAccumulator]
    ) -> DayResult:
        day = state.day + 1
        self._advance_macro(state, day)
        self._market.advance(state, day)
        self._complete_delayed_work(state, day)
        self._depreciate_knowledge(state)
        self._advance_marketing(state)

        outcomes = {
            company_id: upfront_costs.get(
                company_id, _OutcomeAccumulator.create(sorted(self._products))
            )
            for company_id in sorted(state.companies)
        }
        services = {
            company_id: _ServiceAccumulator.create(sorted(self._segments))
            for company_id in sorted(state.companies)
        }
        capacity_remaining = {
            company_id: self._capacity_tiers[company.capacity_tier].units_per_day
            if company.status is CompanyStatus.ACTIVE
            else 0
            for company_id, company in state.companies.items()
        }
        self._charge_daily_policies(state, day, outcomes)
        self._expire_contracts(state, day - 1, outcomes)
        self._fulfill_existing_backlog(state, day, outcomes, services, capacity_remaining)
        demand = self._serve_consumer_market(state, day, outcomes, services, capacity_remaining)
        self._settle_tenders(state, day)
        self._refresh_reserved_inventory(state)
        self._operate_support(state, day, outcomes, services)
        self._expire_contracts(state, day, outcomes)
        self._order_to_base_stock(state, day, outcomes)
        self._charge_holding_costs(state, outcomes)
        self._mark_bankruptcies(state)
        self._inflate_research_uncertainty(state, day)

        state.day = day
        state.version += 1
        if day % self.config.tender_interval_days == 0 and day < self.config.total_days:
            self._open_tender(state, opened_day=day)

        company_outcomes = {
            company_id: CompanyDayOutcome(
                company_id=company_id,
                consumer_orders=outcome.consumer_orders,
                fulfilled_units=outcome.fulfilled_units,
                abandoned_units=outcome.abandoned_units,
                revenue_cents=outcome.revenue_cents,
                costs_cents=outcome.costs_cents,
                expense_breakdown=outcome.expense_breakdown.model_copy(),
                refunds_cents=outcome.refunds_cents,
                cogs_cents=outcome.cogs_cents,
                ending_cash_cents=state.companies[company_id].cash_cents,
                fulfilled_by_product=dict(outcome.fulfilled_by_product),
                fulfilled_by_segment=dict(services[company_id].fulfilled_by_segment),
                failures_by_segment=dict(services[company_id].failures_by_segment),
            )
            for company_id, outcome in outcomes.items()
        }
        for company_id, outcome in outcomes.items():
            company = state.companies[company_id]
            company.daily_history.append(
                CompanyDailyRecord(
                    day=day,
                    revenue_cents=outcome.revenue_cents,
                    costs_cents=outcome.costs_cents,
                    expense_breakdown=outcome.expense_breakdown.model_copy(),
                    refunds_cents=outcome.refunds_cents,
                    cogs_cents=outcome.cogs_cents,
                    ending_cash_cents=company.cash_cents,
                    consumer_orders=outcome.consumer_orders,
                    fulfilled_units=outcome.fulfilled_units,
                    abandoned_units=outcome.abandoned_units,
                    support_backlog=company.support_backlog,
                    fulfillment_backlog_units=sum(order.quantity for order in company.backlog),
                    capacity_tier=company.capacity_tier,
                    fulfilled_by_product=dict(outcome.fulfilled_by_product),
                    fulfilled_by_segment=dict(services[company_id].fulfilled_by_segment),
                    failures_by_segment=dict(services[company_id].failures_by_segment),
                    ending_inventory=dict(company.inventory),
                    satisfaction=dict(company.satisfaction),
                )
            )
        return DayResult(
            day=day,
            world_version=state.version,
            demand_by_segment=demand,
            company_outcomes=company_outcomes,
            state_hash=self.state_hash(state),
        )

    def _advance_macro(self, state: WorldState, day: int) -> None:
        shock = float(self._rng("macro", day).normal(0.0, self.config.macro_volatility))
        state.macro_index = max(
            0.05,
            state.macro_index
            + self.config.macro_reversion * (self.config.macro_mean - state.macro_index)
            + shock,
        )
        state.macro_history[day] = state.macro_index

    def _complete_delayed_work(self, state: WorldState, day: int) -> None:
        for company_id in sorted(state.companies):
            company = state.companies[company_id]
            arrived = [
                shipment for shipment in company.inbound_shipments if shipment.arrival_day <= day
            ]
            company.inbound_shipments = [
                shipment for shipment in company.inbound_shipments if shipment.arrival_day > day
            ]
            for shipment in sorted(arrived, key=lambda item: item.shipment_id):
                company.inventory[shipment.product_id] += shipment.quantity

            self._complete_innovation(company, day)
            self._complete_capacity(company, day)
            self._complete_research(company, day)

    def _complete_innovation(self, company: CompanyState, day: int) -> None:
        matured = [item for item in company.innovation_pipeline if item.maturity_day <= day]
        company.innovation_pipeline = [
            item for item in company.innovation_pipeline if item.maturity_day > day
        ]
        for item in sorted(matured, key=lambda contribution: contribution.contribution_id):
            company.knowledge_capital_cents[item.product_id] += item.capital_cents

        product_count = max(1, len(self._products))
        for project in company.innovation_projects:
            if project.status is not JobStatus.PENDING or project.completion_day > day:
                continue
            project.status = JobStatus.COMPLETED
            gain = project.capital_gain_cents / product_count
            for product_id in sorted(self._products):
                company.knowledge_capital_cents[product_id] += gain

    def _complete_capacity(self, company: CompanyState, day: int) -> None:
        company.action_receipts.extend(self._capacity.complete(company, day))

    def _complete_research(self, company: CompanyState, day: int) -> None:
        for job in sorted(company.research_jobs, key=lambda item: item.job_id):
            if job.status is not JobStatus.PENDING or job.completion_day > day:
                continue
            posterior = {
                metric: self._bayesian_measurement(
                    company, job, metric=metric, truth=truth, day=day
                )
                for metric, truth in sorted(job.snapshot.items())
            }
            company.beliefs[job.segment_id].update(posterior)
            company.research_reports.append(
                ResearchReport(
                    report_id=self._id("research-report", company.company_id, job.job_id),
                    segment_id=job.segment_id,
                    level=job.level,
                    snapshot_day=job.snapshot_day,
                    completed_day=day,
                    posterior=posterior,
                )
            )
            job.status = JobStatus.COMPLETED

    def _bayesian_measurement(
        self,
        company: CompanyState,
        job: MarketResearchJob,
        *,
        metric: str,
        truth: float,
        day: int,
    ) -> NormalBelief:
        prior = company.beliefs[job.segment_id][metric]
        signal_std = self.config.research.level_signal_std[job.level]
        prior_mean = max(prior.mean, 1e-12)
        prior_log_variance = math.log1p(prior.variance / prior_mean**2)
        prior_log_mean = math.log(prior_mean) - 0.5 * prior_log_variance
        signal_variance = max(signal_std**2, 1e-12)
        signal = math.log(max(truth, 1e-12)) + float(
            self._rng("research", job.job_id, metric).normal(0.0, signal_std)
        )
        posterior_log_variance = 1.0 / (
            1.0 / max(prior_log_variance, 1e-12) + 1.0 / signal_variance
        )
        posterior_log_mean = posterior_log_variance * (
            prior_log_mean / max(prior_log_variance, 1e-12) + signal / signal_variance
        )
        posterior_mean = math.exp(posterior_log_mean + 0.5 * posterior_log_variance)
        posterior_variance = (math.exp(posterior_log_variance) - 1.0) * math.exp(
            2.0 * posterior_log_mean + posterior_log_variance
        )
        return NormalBelief(
            mean=posterior_mean,
            variance=max(posterior_variance, 1e-12),
            updated_day=day,
        )

    def _depreciate_knowledge(self, state: WorldState) -> None:
        survival = 1.0 - self.config.innovation.daily_depreciation
        for company in state.companies.values():
            for product_id in self._products:
                company.knowledge_capital_cents[product_id] = max(
                    0.0,
                    company.knowledge_capital_cents[product_id] * survival,
                )

    def _advance_marketing(self, state: WorldState) -> None:
        config = self.config.marketing
        for segment_id in sorted(self._segments):
            impacts: dict[str, float] = {}
            for company_id in sorted(state.companies):
                company = state.companies[company_id]
                spend = (
                    sum(
                        budget * self._segments[segment_id].channel_effectiveness.get(channel, 1.0)
                        for channel, budget in company.marketing_budget_cents[segment_id].items()
                    )
                    if company.status is CompanyStatus.ACTIVE
                    else 0
                )
                impacts[company_id] = (
                    spend / config.spend_reference_cents
                ) ** config.attention_power
            denominator = config.outside_attention + sum(impacts.values())
            for company_id in sorted(state.companies):
                company = state.companies[company_id]
                attention = impacts[company_id] / denominator
                goodwill = max(
                    0.0,
                    (1.0 - config.goodwill_decay) * company.goodwill[segment_id]
                    + config.awareness_gain * attention,
                )
                company.goodwill[segment_id] = goodwill
                company.awareness[segment_id] = 1.0 - math.exp(-goodwill)

    def _charge_daily_policies(
        self,
        state: WorldState,
        day: int,
        outcomes: Mapping[str, _OutcomeAccumulator],
    ) -> None:
        for company_id in sorted(state.companies):
            company = state.companies[company_id]
            if company.status is not CompanyStatus.ACTIVE:
                continue
            marketing = sum(
                sum(channels.values()) for channels in company.marketing_budget_cents.values()
            )
            development = sum(company.development_budget_cents.values())
            capacity = self._capacity_tiers[company.capacity_tier].daily_cost_cents
            for category, amount in (
                (ExpenseCategory.MARKETING, marketing),
                (ExpenseCategory.DEVELOPMENT, development),
                (ExpenseCategory.RELIABILITY, company.reliability_budget_cents),
                (ExpenseCategory.SUPPORT, company.support_budget_cents),
                (ExpenseCategory.CAPACITY_OPERATIONS, capacity),
            ):
                self._charge(company, outcomes[company_id], amount, category)
            for product_id, budget in sorted(company.development_budget_cents.items()):
                if budget <= 0:
                    continue
                company.innovation_pipeline.append(
                    InnovationContribution(
                        contribution_id=self._id("development", company_id, product_id, day),
                        product_id=product_id,
                        capital_cents=max(
                            1, round(budget * self.config.innovation.development_capital_multiplier)
                        ),
                        maturity_day=day + self.config.innovation.development_delay_days,
                    )
                )

    def _expire_contracts(
        self, state: WorldState, due_day: int, outcomes: Mapping[str, _OutcomeAccumulator]
    ) -> None:
        """Settle unfulfilled contracts once, after their last permitted fulfillment day."""
        for company_id, company in sorted(state.companies.items()):
            if company.status is not CompanyStatus.ACTIVE:
                continue
            expired = [
                order
                for order in company.backlog
                if order.source is OrderSource.ENTERPRISE
                and order.delivery_due_day is not None
                and order.delivery_due_day <= due_day
            ]
            for order in expired:
                penalty = (
                    order.quantity
                    * order.unit_price_cents
                    * self.config.tender_default_penalty_bps
                    // 10_000
                )
                self._charge(
                    company, outcomes[company_id], penalty, ExpenseCategory.CONTRACT_PENALTIES
                )
                company.cumulative_abandoned_units += order.quantity
                outcomes[company_id].abandoned_units += order.quantity
                company.reserved_inventory[order.product_id] = max(
                    0, company.reserved_inventory[order.product_id] - order.quantity
                )
                company.action_receipts.append(
                    ActionReceipt(
                        day=max(state.day + 1, due_day),
                        action="enterprise_delivery",
                        status="rejected",
                        message="Unfulfilled units expired; contractual penalty charged.",
                        charged_cents=penalty,
                        project_id=order.order_id,
                    )
                )
            expired_ids = {order.order_id for order in expired}
            company.backlog = [
                order for order in company.backlog if order.order_id not in expired_ids
            ]

    def _fulfill_existing_backlog(
        self,
        state: WorldState,
        day: int,
        outcomes: Mapping[str, _OutcomeAccumulator],
        services: Mapping[str, _ServiceAccumulator],
        capacity_remaining: dict[str, int],
    ) -> None:
        abandonment = self.config.operations.abandonment_daily_rate
        for company_id in sorted(state.companies):
            company = state.companies[company_id]
            if company.status is not CompanyStatus.ACTIVE:
                continue
            remaining: list[BacklogOrder] = []
            for order in sorted(
                company.backlog, key=lambda item: (item.created_day, item.order_id)
            ):
                abandoned = 0
                if order.source is OrderSource.CONSUMER and abandonment > 0:
                    abandoned = int(
                        self._rng("backlog-abandon", day, company_id, order.order_id).binomial(
                            order.quantity,
                            abandonment,
                        )
                    )
                quantity = max(0, order.quantity - abandoned)
                outcomes[company_id].abandoned_units += abandoned
                company.cumulative_abandoned_units += abandoned
                available_inventory = company.inventory[order.product_id]
                if order.source is OrderSource.CONSUMER:
                    available_inventory = max(
                        0,
                        available_inventory - company.reserved_inventory[order.product_id],
                    )
                fulfilled = min(quantity, available_inventory, capacity_remaining[company_id])
                if fulfilled > 0:
                    self._book_fulfillment(
                        company,
                        outcomes[company_id],
                        order.product_id,
                        fulfilled,
                        order.unit_price_cents,
                    )
                    capacity_remaining[company_id] -= fulfilled
                    if order.source is OrderSource.ENTERPRISE:
                        company.reserved_inventory[order.product_id] = max(
                            0,
                            company.reserved_inventory[order.product_id] - fulfilled,
                        )
                    if order.segment_id:
                        services[company_id].fulfilled_by_segment[order.segment_id] += fulfilled
                        services[company_id].quality_sum_by_segment[order.segment_id] += (
                            fulfilled * self._effective_quality(company, order.product_id)
                        )
                        services[company_id].price_sum_by_segment[order.segment_id] += (
                            fulfilled * order.unit_price_cents
                        )
                        services[company_id].wait_days_sum_by_segment[order.segment_id] += (
                            fulfilled * max(0, day - order.created_day)
                        )
                left = max(0, quantity - fulfilled)
                if left > 0:
                    remaining.append(order.model_copy(update={"quantity": left}))
            company.backlog = remaining

    def _serve_consumer_market(
        self,
        state: WorldState,
        day: int,
        outcomes: Mapping[str, _OutcomeAccumulator],
        services: Mapping[str, _ServiceAccumulator],
        capacity_remaining: dict[str, int],
    ) -> dict[str, int]:
        demand_by_segment: dict[str, int] = {}
        for segment_id in sorted(self._segments):
            demand = self._daily_demand(state, self._segments[segment_id], day)
            demand_by_segment[segment_id] = demand
            remaining = demand
            excluded = {
                (company_id, product_id)
                for company_id, company in state.companies.items()
                for product_id in self._products
                if company.status is not CompanyStatus.ACTIVE
                or company.inventory[product_id] - company.reserved_inventory[product_id] <= 0
                or capacity_remaining[company_id] <= 0
            }
            round_index = 0
            while remaining > 0:
                shares = self.expected_choice_shares(
                    state, segment_id, excluded=frozenset(excluded)
                )
                choices = sorted(shares.product)
                probabilities = [shares.product[choice] for choice in choices] + [shares.outside]
                assigned = self._rng("consumer-choice", day, segment_id, round_index).multinomial(
                    remaining,
                    probabilities,
                )
                reroute = 0
                for choice, desired_raw in zip(choices, assigned[:-1], strict=True):
                    desired = int(desired_raw)
                    if desired <= 0:
                        continue
                    company = state.companies[choice.company_id]
                    available = min(
                        max(
                            0,
                            company.inventory[choice.product_id]
                            - company.reserved_inventory[choice.product_id],
                        ),
                        capacity_remaining[choice.company_id],
                    )
                    fulfilled = min(desired, available)
                    if fulfilled > 0:
                        price = max(
                            0,
                            company.prices_cents[choice.product_id]
                            - company.promotions_cents[choice.product_id],
                        )
                        self._book_fulfillment(
                            company,
                            outcomes[choice.company_id],
                            choice.product_id,
                            fulfilled,
                            price,
                        )
                        outcomes[choice.company_id].consumer_orders += fulfilled
                        company.cumulative_consumer_orders += fulfilled
                        services[choice.company_id].fulfilled_by_segment[segment_id] += fulfilled
                        services[choice.company_id].quality_sum_by_segment[segment_id] += (
                            fulfilled * self._effective_quality(company, choice.product_id)
                        )
                        services[choice.company_id].price_sum_by_segment[segment_id] += (
                            fulfilled * price
                        )
                        capacity_remaining[choice.company_id] -= fulfilled

                    gap = max(0, desired - fulfilled)
                    if gap <= 0:
                        continue
                    switchers = int(
                        self._rng(
                            "stockout-switch",
                            day,
                            segment_id,
                            round_index,
                            choice.company_id,
                            choice.product_id,
                        ).binomial(gap, self.config.operations.stockout_switch_probability)
                    )
                    backlog = max(0, gap - switchers)
                    if backlog > 0:
                        price = max(
                            0,
                            company.prices_cents[choice.product_id]
                            - company.promotions_cents[choice.product_id],
                        )
                        company.backlog.append(
                            BacklogOrder(
                                order_id=self._id(
                                    "consumer-backlog",
                                    day,
                                    segment_id,
                                    round_index,
                                    choice.company_id,
                                    choice.product_id,
                                ),
                                source=OrderSource.CONSUMER,
                                product_id=choice.product_id,
                                segment_id=segment_id,
                                quantity=backlog,
                                unit_price_cents=price,
                                created_day=day,
                            )
                        )
                        outcomes[choice.company_id].consumer_orders += backlog
                        company.cumulative_consumer_orders += backlog
                    reroute += switchers
                    excluded.add((choice.company_id, choice.product_id))

                remaining = reroute
                round_index += 1
                if not choices or round_index > len(state.companies) * len(self._products):
                    break
        return demand_by_segment

    def _daily_demand(self, state: WorldState, segment: SegmentConfig, day: int) -> int:
        segment = self._market.segment(state, segment.segment_id)
        seasonal = 1.0 + segment.seasonal_amplitude * math.sin(
            2.0 * math.pi * day / segment.seasonal_period_days
        )
        macro = max(0.05, 1.0 + segment.macro_sensitivity * (state.macro_index - 1.0))
        expected = max(0.0, segment.base_daily_demand * seasonal * macro)
        return int(self._rng("demand", day, segment.segment_id).poisson(expected))

    def _settle_tenders(self, state: WorldState, day: int) -> None:
        for tender in sorted(state.tenders, key=lambda item: item.tender_id):
            if tender.settled or tender.deadline_day > day:
                continue
            qualified: list[tuple[int, str]] = []
            for company_id, price in tender.bids.items():
                company = state.companies.get(company_id)
                if company is None or company.status is not CompanyStatus.ACTIVE:
                    continue
                if price > tender.reservation_price_cents:
                    continue
                if self._effective_quality(company, tender.product_id) < tender.minimum_quality:
                    continue
                available = (
                    company.inventory[tender.product_id]
                    - company.reserved_inventory[tender.product_id]
                )
                due_day = tender.delivery_due_day or (day + self.config.tender_delivery_days)
                capacity = self._delivery_capacity(company, day, due_day)
                committed = sum(order.quantity for order in company.backlog)
                if available < tender.quantity or capacity - committed < tender.quantity:
                    continue
                qualified.append((price, company_id))
            if qualified:
                minimum = min(price for price, _ in qualified)
                tied = sorted(company_id for price, company_id in qualified if price == minimum)
                winner = min(
                    tied,
                    key=lambda company_id: self._id("tender-tie", tender.tender_id, company_id),
                )
                tender.winner_company_id = winner
                tender.winning_unit_price_cents = minimum
                state.companies[winner].backlog.append(
                    BacklogOrder(
                        order_id=self._id("enterprise-order", tender.tender_id, winner),
                        source=OrderSource.ENTERPRISE,
                        product_id=tender.product_id,
                        quantity=tender.quantity,
                        unit_price_cents=minimum,
                        created_day=day,
                        delivery_due_day=tender.delivery_due_day
                        or (day + self.config.tender_delivery_days),
                    )
                )
                state.companies[winner].reserved_inventory[tender.product_id] += tender.quantity
            tender.settled = True

    def _delivery_capacity(self, company: CompanyState, award_day: int, due_day: int) -> int:
        base = self._capacity_tiers[company.capacity_tier].units_per_day
        capacity = base * max(0, due_day - award_day)
        for project in company.capacity_projects:
            if project.status is JobStatus.PENDING:
                days = max(0, due_day - max(award_day + 1, project.completion_day) + 1)
                capacity += days * (self._capacity_tiers[project.target_tier].units_per_day - base)
        return max(0, capacity)

    def _operate_support(
        self,
        state: WorldState,
        day: int,
        outcomes: Mapping[str, _OutcomeAccumulator],
        services: Mapping[str, _ServiceAccumulator],
    ) -> None:
        config = self.config.operations
        for company_id in sorted(state.companies):
            company = state.companies[company_id]
            if company.status is not CompanyStatus.ACTIVE:
                continue
            service = services[company_id]
            daily_capacity = self._capacity_tiers[company.capacity_tier].units_per_day
            queued_units = sum(order.quantity for order in company.backlog)
            utilization = (outcomes[company_id].fulfilled_units + queued_units) / max(
                1, daily_capacity
            )
            congestion = 1.0 + 2.0 * max(0.0, utilization - 0.75) ** 2
            failure_probability = min(
                1.0,
                max(
                    config.failure_probability_floor,
                    config.failure_probability_floor
                    + (config.base_failure_probability - config.failure_probability_floor)
                    * math.exp(
                        -company.reliability_budget_cents / config.reliability_spend_scale_cents
                    )
                    * congestion,
                ),
            )
            for segment_id in sorted(self._segments):
                fulfilled = service.fulfilled_by_segment[segment_id]
                failures = int(
                    self._rng("service-failures", day, company_id, segment_id).binomial(
                        fulfilled,
                        min(1.0, failure_probability),
                    )
                )
                service.failures_by_segment[segment_id] = failures
                expected_tickets = (
                    config.base_ticket_rate * fulfilled + config.failure_ticket_rate * failures
                )
                service.tickets += int(
                    self._rng("support-tickets", day, company_id, segment_id).poisson(
                        max(0.0, expected_tickets)
                    )
                )

            pending = company.support_backlog + service.tickets
            agents = company.support_budget_cents / config.support_agent_daily_cost_cents
            resolution_mean = max(0.0, agents * config.tickets_per_agent_day)
            resolved = min(
                pending,
                int(self._rng("support-resolution", day, company_id).poisson(resolution_mean)),
            )
            company.support_backlog = max(0, pending - resolved)
            remediation = (
                sum(service.failures_by_segment.values())
                * config.remediation_cost_per_failure_cents
            )
            self._charge(company, outcomes[company_id], remediation, ExpenseCategory.REMEDIATION)
            support_pressure = company.support_backlog / max(
                1, pending + outcomes[company_id].fulfilled_units
            )
            for segment_id in sorted(self._segments):
                fulfilled = service.fulfilled_by_segment[segment_id]
                failure_rate = service.failures_by_segment[segment_id] / max(1, fulfilled)
                segment = self._market.segment(state, segment_id)
                average_quality = (
                    service.quality_sum_by_segment[segment_id] / fulfilled
                    if fulfilled > 0
                    else segment.expected_quality
                )
                average_price = service.price_sum_by_segment[segment_id] / max(1, fulfilled)
                expected_quality = segment.expected_quality + (
                    segment.price_expectation_quality_slope
                    * max(0.0, average_price / segment.price_reference_cents - 1.0)
                )
                observed_wait = service.wait_days_sum_by_segment[segment_id] / max(1, fulfilled)
                projected_wait = queued_units / max(1, daily_capacity)
                wait_penalty = math.log1p(
                    max(observed_wait, projected_wait) / segment.service_level_target
                )
                instantaneous = (
                    average_quality
                    - expected_quality
                    - config.wait_satisfaction_weight * wait_penalty
                    - config.failure_satisfaction_weight * failure_rate
                    - config.issue_satisfaction_weight * support_pressure
                )
                if instantaneous < 0:
                    instantaneous *= config.negative_experience_weight
                instantaneous = self._clamp(instantaneous, -1.0, 1.0)
                old = company.satisfaction[segment_id]
                satisfaction = (
                    1.0 - config.satisfaction_alpha
                ) * old + config.satisfaction_alpha * instantaneous
                company.satisfaction[segment_id] = self._clamp(satisfaction, -1.0, 1.0)
                loyalty_target = (
                    max(0.0, company.satisfaction[segment_id])
                    if fulfilled > 0
                    else company.loyalty[segment_id]
                )
                company.loyalty[segment_id] = self._clamp(
                    0.9 * company.loyalty[segment_id] + 0.1 * loyalty_target,
                    0.0,
                    1.0,
                )

    def _order_to_base_stock(
        self,
        state: WorldState,
        day: int,
        outcomes: Mapping[str, _OutcomeAccumulator],
    ) -> None:
        for company_id in sorted(state.companies):
            company = state.companies[company_id]
            if company.status is not CompanyStatus.ACTIVE:
                continue
            for product_id in sorted(self._products):
                product = self._products[product_id]
                inbound = sum(
                    shipment.quantity
                    for shipment in company.inbound_shipments
                    if shipment.product_id == product_id
                )
                position = (
                    company.inventory[product_id] + inbound - company.reserved_inventory[product_id]
                )
                desired = max(0, company.reorder_targets[product_id] - position)
                affordable = max(0, company.cash_cents // product.unit_cost_cents)
                quantity = min(desired, affordable)
                if quantity <= 0:
                    continue
                cost = quantity * product.unit_cost_cents
                self._charge(company, outcomes[company_id], cost, ExpenseCategory.PROCUREMENT)
                company.inbound_shipments.append(
                    InboundShipment(
                        shipment_id=self._id("shipment", company_id, product_id, day),
                        product_id=product_id,
                        quantity=quantity,
                        arrival_day=day + product.procurement_lead_days,
                    )
                )

    def _charge_holding_costs(
        self,
        state: WorldState,
        outcomes: Mapping[str, _OutcomeAccumulator],
    ) -> None:
        for company_id, company in state.companies.items():
            if company.status is not CompanyStatus.ACTIVE:
                continue
            cost = sum(
                company.inventory[product_id] * self._products[product_id].holding_cost_cents
                for product_id in self._products
            )
            self._charge(company, outcomes[company_id], cost, ExpenseCategory.HOLDING)

    def _refresh_reserved_inventory(self, state: WorldState) -> None:
        for company in state.companies.values():
            company.reserved_inventory = {
                product_id: sum(
                    order.quantity
                    for order in company.backlog
                    if order.product_id == product_id and order.source is OrderSource.ENTERPRISE
                )
                for product_id in self._products
            }

    def _mark_bankruptcies(self, state: WorldState) -> None:
        for company in state.companies.values():
            if company.cash_cents < 0:
                company.status = CompanyStatus.BANKRUPT

    def _inflate_research_uncertainty(self, state: WorldState, day: int) -> None:
        if day % self.config.days_per_decision_window != 0:
            return
        process_std = self.config.research.weekly_process_std
        if process_std <= 0:
            return
        for company in state.companies.values():
            for metrics in company.beliefs.values():
                for key, belief in metrics.items():
                    metrics[key] = belief.model_copy(
                        update={"variance": belief.variance + (belief.mean * process_std) ** 2}
                    )

    def _open_tender(self, state: WorldState, opened_day: int) -> None:
        if opened_day + self.config.tender_bid_window_days >= self.config.total_days:
            return
        if any(tender.opened_day == opened_day for tender in state.tenders):
            return
        product_ids = sorted(self._products)
        rng = self._rng("tender", opened_day)
        product_id = product_ids[int(rng.integers(0, len(product_ids)))]
        product = self._products[product_id]
        typical_demand = sum(segment.base_daily_demand for segment in self._segments.values())
        quantity_fractions = (
            _LEGACY_TENDER_QUANTITY_FRACTIONS
            if self.config.scenario_id == "ceos-retail-v1"
            else _REFERENCE_TENDER_QUANTITY_FRACTIONS
        )
        quantity = max(1, round(typical_demand * float(rng.uniform(*quantity_fractions))))
        state.tenders.append(
            EnterpriseTender(
                tender_id=self._id("tender", opened_day, product_id),
                product_id=product_id,
                quantity=quantity,
                minimum_quality=product.base_quality + float(rng.uniform(0.0, 0.05)),
                reservation_price_cents=max(
                    product.unit_cost_cents + 1,
                    round(product.initial_price_cents * 1.25),
                ),
                opened_day=opened_day,
                deadline_day=opened_day + self.config.tender_bid_window_days,
                delivery_due_day=min(
                    self.config.total_days,
                    opened_day
                    + self.config.tender_bid_window_days
                    + self.config.tender_delivery_days,
                ),
            )
        )

    def _choice_utility(
        self,
        company: CompanyState,
        product: ProductConfig,
        segment: SegmentConfig,
    ) -> float:
        effective_price = max(
            0,
            company.prices_cents[product.product_id] - company.promotions_cents[product.product_id],
        )
        quality = self._effective_quality(company, product.product_id)
        awareness = max(company.awareness[segment.segment_id], 1e-9)
        return (
            segment.quality_weight * (quality - segment.expected_quality)
            + segment.service_weight * company.satisfaction[segment.segment_id]
            + segment.awareness_weight * math.log(awareness)
            + segment.loyalty_weight * company.loyalty[segment.segment_id]
            - segment.price_weight * effective_price / segment.price_reference_cents
            + segment.product_match.get(product.product_id, 0.0)
        )

    def _effective_quality(self, company: CompanyState, product_id: str) -> float:
        innovation = self.config.innovation
        capital = max(0.0, company.knowledge_capital_cents[product_id])
        return self._products[product_id].base_quality + innovation.quality_scale * math.log1p(
            capital / innovation.capital_reference_cents
        )

    def _softmax_shares(
        self,
        utilities: Mapping[ProductChoice, float],
        outside_utility: float,
    ) -> ChoiceShares:
        maximum = max([outside_utility, *utilities.values()])
        outside_weight = math.exp(max(self._MIN_UTILITY_EXPONENT, outside_utility - maximum))
        weights = {
            choice: math.exp(max(self._MIN_UTILITY_EXPONENT, utility - maximum))
            for choice, utility in utilities.items()
        }
        denominator = outside_weight + sum(weights.values())
        return ChoiceShares(
            product={choice: weight / denominator for choice, weight in weights.items()},
            outside=outside_weight / denominator,
        )

    def _book_fulfillment(
        self,
        company: CompanyState,
        outcome: _OutcomeAccumulator,
        product_id: str,
        quantity: int,
        unit_price_cents: int,
    ) -> None:
        company.inventory[product_id] = max(0, company.inventory[product_id] - quantity)
        revenue = quantity * unit_price_cents
        company.cash_cents += revenue
        company.cumulative_revenue_cents += revenue
        company.cumulative_fulfilled_units += quantity
        outcome.fulfilled_units += quantity
        outcome.revenue_cents += revenue
        outcome.cogs_cents += quantity * self._products[product_id].unit_cost_cents
        outcome.fulfilled_by_product[product_id] = (
            outcome.fulfilled_by_product.get(product_id, 0) + quantity
        )

    @staticmethod
    def _charge(
        company: CompanyState,
        outcome: _OutcomeAccumulator,
        amount: int,
        category: ExpenseCategory,
    ) -> None:
        if amount <= 0:
            return
        company.cash_cents -= amount
        company.cumulative_cost_cents += amount
        outcome.costs_cents += amount
        outcome.expense_breakdown.add(category, amount)

    def _require_segment(self, segment_id: str) -> SegmentConfig:
        segment = self._segments.get(segment_id)
        if segment is None:
            raise ValueError(f"unknown segment {segment_id}")
        return segment

    def _rng(self, *path: object) -> np.random.Generator:
        return self._random.generator(self.config.scenario_id, *path)

    def _id(self, *path: object) -> str:
        return self._random.digest(self.config.scenario_id, *path)

    @staticmethod
    def _clamp(value: float, minimum: float, maximum: float) -> float:
        return min(maximum, max(minimum, value))
