"""Typed contracts shared by the economy, Harness, persistence, and evaluation modules."""

from __future__ import annotations

from enum import StrEnum
from itertools import pairwise
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCENARIO_VERSION = "ceos-market-v3"
OFFICIAL_SCORE_DESCRIPTION = (
    "terminal cash + terminal inventory liquidation value - initial enterprise value"
)


class StrictModel(BaseModel):
    """Reject unknown fields so protocol drift fails loudly."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    def detached_copy(self) -> Self:
        """Clone complete serializable snapshots without Python's recursive deepcopy overhead."""
        return type(self).model_validate_json(self.model_dump_json())


class CompanyStatus(StrEnum):
    ACTIVE = "active"
    BANKRUPT = "bankrupt"


class OrderSource(StrEnum):
    CONSUMER = "consumer"
    ENTERPRISE = "enterprise"


class JobStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CapacityChangeKind(StrEnum):
    EXPAND = "expand"
    DOWNSIZE = "downsize"


class ExpenseCategory(StrEnum):
    PROCUREMENT = "procurement"
    CAPACITY_OPERATIONS = "capacity_operations"
    MARKETING = "marketing"
    DEVELOPMENT = "development"
    RELIABILITY = "reliability"
    SUPPORT = "support"
    INNOVATION = "innovation"
    CAPACITY_INVESTMENT = "capacity_investment"
    CAPACITY_ADJUSTMENT = "capacity_adjustment"
    MARKET_RESEARCH = "market_research"
    HOLDING = "holding"
    REMEDIATION = "remediation"
    CONTRACT_PENALTIES = "contract_penalties"


class ExpenseBreakdown(StrictModel):
    """Cash payments in cents; refunds and diagnostic COGS are separate."""

    procurement: int = Field(default=0, ge=0)
    capacity_operations: int = Field(default=0, ge=0)
    marketing: int = Field(default=0, ge=0)
    development: int = Field(default=0, ge=0)
    reliability: int = Field(default=0, ge=0)
    support: int = Field(default=0, ge=0)
    innovation: int = Field(default=0, ge=0)
    capacity_investment: int = Field(default=0, ge=0)
    capacity_adjustment: int = Field(default=0, ge=0)
    market_research: int = Field(default=0, ge=0)
    holding: int = Field(default=0, ge=0)
    remediation: int = Field(default=0, ge=0)
    contract_penalties: int = Field(default=0, ge=0)

    def add(self, category: ExpenseCategory, cents: int) -> None:
        """Accumulate a non-negative payment without changing sales revenue."""
        if cents < 0:
            raise ValueError("cash payments must be non-negative")
        setattr(self, category.value, getattr(self, category.value) + cents)

    @property
    def total(self) -> int:
        return sum(getattr(self, category.value) for category in ExpenseCategory)


class ActionReceipt(StrictModel):
    """Environment-owned acknowledgement, distinct from a staged tool change."""

    day: int = Field(ge=0)
    action: str
    status: Literal["applied", "no_op", "completed", "cancelled", "rejected"]
    message: str
    charged_cents: int = Field(default=0, ge=0)
    refunded_cents: int = Field(default=0, ge=0)
    effective_day: int | None = Field(default=None, ge=0)
    project_id: str | None = None
    target_tier: int | None = Field(default=None, ge=0)


class CapacityActionQuote(StrictModel):
    """Authoritative company-local quote shared by both language gateways."""

    action: Literal["expand_capacity", "downsize_capacity", "cancel_capacity_project"]
    target_tier: int | None = Field(default=None, ge=0)
    project_id: str | None = None
    charged_cents: int = Field(default=0, ge=0)
    refunded_cents: int = Field(default=0, ge=0)
    effective_day: int | None = Field(default=None, ge=0)


class CapacityPolicyConfig(StrictModel):
    startup_cost_bps: int = Field(default=1_000, ge=0, le=10_000)
    cancellation_fee_bps: int = Field(default=500, ge=0, le=10_000)
    downsize_fee_per_tier_cents: int = Field(default=100_000, ge=0)
    downsize_lead_days: int = Field(default=7, ge=1)


class MarketPhaseConfig(StrictModel):
    phase_id: str = Field(min_length=1)
    demand_multiplier: float = Field(default=1.0, gt=0.0)
    segment_weights: dict[str, float] = Field(default_factory=dict)
    price_weight_multiplier: float = Field(default=1.0, gt=0.0)
    quality_weight_multiplier: float = Field(default=1.0, gt=0.0)
    service_weight_multiplier: float = Field(default=1.0, gt=0.0)
    quality_expectation_shift: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def positive_weights(self) -> MarketPhaseConfig:
        if any(weight <= 0 for weight in self.segment_weights.values()):
            raise ValueError("market segment weights must be positive")
        return self


class MarketDynamicsConfig(StrictModel):
    """Empty phases disable dynamics for controlled mechanism tests."""

    phases: tuple[MarketPhaseConfig, ...] = ()
    transitions: tuple[tuple[float, ...], ...] = ()
    initial_phase: int = Field(default=0, ge=0)
    publication_delay_days: int = Field(default=7, ge=0)
    daily_quality_drift: float = Field(default=0.0, ge=0.0)
    maximum_quality_drift: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def valid_transition_matrix(self) -> MarketDynamicsConfig:
        count = len(self.phases)
        if len({phase.phase_id for phase in self.phases}) != count:
            raise ValueError("market phase ids must be unique")
        if len(self.transitions) != count:
            raise ValueError("transition matrix must have one row per phase")
        if (count and self.initial_phase >= count) or (not count and self.initial_phase):
            raise ValueError("initial market phase is out of range")
        for row in self.transitions:
            if len(row) != count or any(value < 0 or value > 1 for value in row):
                raise ValueError("invalid transition probabilities")
            if abs(sum(row) - 1.0) > 1e-9:
                raise ValueError("transition probabilities must sum to one")
        return self


class MarketState(StrictModel):
    day: int = Field(default=0, ge=0)
    phase_index: int = Field(default=0, ge=0)
    phase_started_day: int = Field(default=0, ge=0)


class PublicMarketSignal(StrictModel):
    day: int = Field(default=0, ge=0)
    demand_index: float = Field(default=1.0, gt=0.0)
    quality_expectation_index: float = Field(default=1.0, gt=0.0)


class ProductConfig(StrictModel):
    product_id: str = Field(min_length=1)
    base_quality: float = Field(ge=0.0)
    unit_cost_cents: int = Field(gt=0)
    salvage_value_cents: int = Field(ge=0)
    holding_cost_cents: int = Field(ge=0)
    procurement_lead_days: int = Field(ge=1)
    initial_inventory: int = Field(ge=0)
    initial_price_cents: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_salvage(self) -> ProductConfig:
        if self.salvage_value_cents >= self.unit_cost_cents:
            raise ValueError("salvage value must be below procurement cost")
        return self


class SegmentConfig(StrictModel):
    segment_id: str = Field(min_length=1)
    base_daily_demand: float = Field(gt=0.0)
    price_reference_cents: int = Field(gt=0)
    price_weight: float = Field(gt=0.0)
    quality_weight: float = Field(ge=0.0)
    service_weight: float = Field(ge=0.0)
    awareness_weight: float = Field(ge=0.0)
    loyalty_weight: float = Field(ge=0.0)
    outside_utility: float = 0.0
    macro_sensitivity: float = Field(ge=0.0)
    seasonal_amplitude: float = Field(ge=0.0, le=1.0)
    seasonal_period_days: int = Field(gt=1)
    expected_quality: float = Field(ge=0.0)
    service_level_target: float = Field(gt=0.0)
    price_expectation_quality_slope: float = Field(default=0.12, ge=0.0)
    product_match: dict[str, float] = Field(default_factory=dict)
    channel_effectiveness: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_effectiveness(self) -> SegmentConfig:
        if any(value <= 0 for value in self.channel_effectiveness.values()):
            raise ValueError("channel effectiveness must be positive")
        return self


class CapacityTierConfig(StrictModel):
    tier: int = Field(ge=0)
    units_per_day: int = Field(gt=0)
    daily_cost_cents: int = Field(ge=0)
    expansion_cost_cents: int = Field(ge=0)
    expansion_lead_days: int = Field(ge=1)


class MarketingMechanismConfig(StrictModel):
    attention_power: float = Field(gt=0.0, le=1.0)
    outside_attention: float = Field(gt=0.0)
    goodwill_decay: float = Field(gt=0.0, lt=1.0)
    awareness_gain: float = Field(gt=0.0)
    spend_reference_cents: int = Field(gt=0)


class InnovationMechanismConfig(StrictModel):
    daily_depreciation: float = Field(ge=0.0, lt=1.0)
    development_delay_days: int = Field(ge=1)
    development_capital_multiplier: float = Field(default=1.0, gt=0.0)
    quality_scale: float = Field(gt=0.0)
    capital_reference_cents: int = Field(gt=0)
    project_cost_cents: dict[int, int]
    project_median_days: dict[int, int]
    project_capital_gain_cents: dict[int, int]
    project_duration_log_std: float = Field(default=0.25, ge=0.0)
    project_gain_log_std: float = Field(default=0.20, ge=0.0)

    @model_validator(mode="after")
    def validate_tiers(self) -> InnovationMechanismConfig:
        tiers = {1, 2, 3}
        if set(self.project_cost_cents) != tiers:
            raise ValueError("innovation costs must define tiers 1, 2, and 3")
        if set(self.project_median_days) != tiers:
            raise ValueError("innovation durations must define tiers 1, 2, and 3")
        if set(self.project_capital_gain_cents) != tiers:
            raise ValueError("innovation gains must define tiers 1, 2, and 3")
        if any(value < 0 for value in self.project_cost_cents.values()):
            raise ValueError("innovation costs must be non-negative")
        if any(value <= 0 for value in self.project_median_days.values()):
            raise ValueError("innovation durations must be positive")
        if any(value <= 0 for value in self.project_capital_gain_cents.values()):
            raise ValueError("innovation gains must be positive")
        return self


class OperationsMechanismConfig(StrictModel):
    base_failure_probability: float = Field(ge=0.0, le=1.0)
    failure_probability_floor: float = Field(ge=0.0, le=1.0)
    reliability_spend_scale_cents: int = Field(gt=0)
    support_agent_daily_cost_cents: int = Field(gt=0)
    tickets_per_agent_day: float = Field(gt=0.0)
    base_ticket_rate: float = Field(ge=0.0)
    failure_ticket_rate: float = Field(ge=0.0)
    satisfaction_alpha: float = Field(gt=0.0, le=1.0)
    negative_experience_weight: float = Field(ge=1.0)
    abandonment_daily_rate: float = Field(ge=0.0, le=1.0)
    stockout_switch_probability: float = Field(default=0.65, ge=0.0, le=1.0)
    wait_satisfaction_weight: float = Field(default=0.15, ge=0.0)
    failure_satisfaction_weight: float = Field(default=1.0, ge=0.0)
    issue_satisfaction_weight: float = Field(default=1.0, ge=0.0)
    remediation_cost_per_failure_cents: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_failure_floor(self) -> OperationsMechanismConfig:
        if self.failure_probability_floor > self.base_failure_probability:
            raise ValueError("failure probability floor cannot exceed the base probability")
        return self


class ResearchMechanismConfig(StrictModel):
    level_cost_cents: dict[int, int]
    level_delay_days: dict[int, int]
    level_signal_std: dict[int, float]
    prior_std: float = Field(gt=0.0)
    weekly_process_std: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_levels(self) -> ResearchMechanismConfig:
        levels = set(range(1, 6))
        if set(self.level_cost_cents) != levels:
            raise ValueError("research costs must define levels 1 through 5")
        if set(self.level_delay_days) != levels:
            raise ValueError("research delays must define levels 1 through 5")
        if set(self.level_signal_std) != levels:
            raise ValueError("research precision must define levels 1 through 5")
        if any(value < 0 for value in self.level_cost_cents.values()):
            raise ValueError("research costs must be non-negative")
        if any(value <= 0 for value in self.level_delay_days.values()):
            raise ValueError("research delays must be positive")
        if any(value <= 0 for value in self.level_signal_std.values()):
            raise ValueError("research signal standard deviations must be positive")
        if any(
            self.level_signal_std[level + 1] >= self.level_signal_std[level]
            for level in range(1, 5)
        ):
            raise ValueError("higher research levels must be strictly more precise")
        return self


class ScenarioConfig(StrictModel):
    scenario_id: str = SCENARIO_VERSION
    seed: int = 42
    total_days: int = Field(default=500, gt=0)
    days_per_decision_window: int = Field(default=7, gt=0)
    initial_cash_cents: int = Field(default=50_000_000, gt=0)
    initial_capacity_tier: int = Field(default=0, ge=0)
    channels: tuple[str, ...] = ("search", "display", "affiliate")
    products: tuple[ProductConfig, ...]
    segments: tuple[SegmentConfig, ...]
    capacity_tiers: tuple[CapacityTierConfig, ...]
    marketing: MarketingMechanismConfig
    innovation: InnovationMechanismConfig
    operations: OperationsMechanismConfig
    research: ResearchMechanismConfig
    capacity_policy: CapacityPolicyConfig = Field(default_factory=CapacityPolicyConfig)
    market_dynamics: MarketDynamicsConfig = Field(default_factory=MarketDynamicsConfig)
    macro_mean: float = 1.0
    macro_reversion: float = Field(default=0.02, gt=0.0, le=1.0)
    macro_volatility: float = Field(default=0.0016, ge=0.0)
    macro_publication_delay_days: int = Field(default=7, ge=0)
    tender_interval_days: int = Field(default=14, gt=0)
    tender_bid_window_days: int = Field(default=7, gt=0)
    tender_delivery_days: int = Field(default=7, gt=0)
    tender_default_penalty_bps: int = Field(default=1_000, ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_identifiers(self) -> ScenarioConfig:
        if not self.products or not self.segments or not self.capacity_tiers:
            raise ValueError("scenario requires products, segments, and capacity tiers")
        if not self.channels or len(self.channels) != len(set(self.channels)):
            raise ValueError("marketing channels must be non-empty and unique")
        for name, values in (
            ("product", [item.product_id for item in self.products]),
            ("segment", [item.segment_id for item in self.segments]),
            ("capacity tier", [str(item.tier) for item in self.capacity_tiers]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {name} identifier")
        if self.initial_capacity_tier not in {tier.tier for tier in self.capacity_tiers}:
            raise ValueError("initial capacity tier is not configured")
        product_ids = {product.product_id for product in self.products}
        channels = set(self.channels)
        for segment in self.segments:
            if not set(segment.product_match) <= product_ids:
                raise ValueError(f"segment {segment.segment_id} references an unknown product")
            if not set(segment.channel_effectiveness) <= channels:
                raise ValueError(f"segment {segment.segment_id} references an unknown channel")
        segment_ids = {segment.segment_id for segment in self.segments}
        for phase in self.market_dynamics.phases:
            if not set(phase.segment_weights) <= segment_ids:
                raise ValueError("market phase references an unknown customer group")
        tiers = sorted(self.capacity_tiers, key=lambda item: item.tier)
        if any(right.units_per_day <= left.units_per_day for left, right in pairwise(tiers)):
            raise ValueError("capacity units must increase with tier")
        if any(right.daily_cost_cents < left.daily_cost_cents for left, right in pairwise(tiers)):
            raise ValueError("capacity daily cost cannot decrease with tier")
        if any(
            right.expansion_cost_cents < left.expansion_cost_cents
            for left, right in pairwise(tiers)
        ):
            raise ValueError("capacity capital cost cannot decrease with tier")
        return self


class ProductDecision(StrictModel):
    product_id: str
    price_cents: int | None = Field(default=None, gt=0)
    promotion_cents: int | None = Field(default=None, ge=0)
    reorder_target: int | None = Field(default=None, ge=0)
    development_budget_cents: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_promotion(self) -> ProductDecision:
        if (
            self.price_cents is not None
            and self.promotion_cents is not None
            and self.promotion_cents > self.price_cents
        ):
            raise ValueError("promotion cannot exceed price")
        return self


class MarketingAllocation(StrictModel):
    segment_id: str
    channel: str
    daily_budget_cents: int = Field(ge=0)


class EnterpriseBid(StrictModel):
    tender_id: str
    unit_price_cents: int = Field(gt=0)


class MarketResearchRequest(StrictModel):
    segment_id: str
    level: int = Field(ge=1, le=5)


class CompanyDecision(StrictModel):
    company_id: str
    world_version: int = Field(ge=0)
    products: tuple[ProductDecision, ...] = ()
    marketing: tuple[MarketingAllocation, ...] = ()
    reliability_budget_cents: int | None = Field(default=None, ge=0)
    support_budget_cents: int | None = Field(default=None, ge=0)
    research_project_tier: int | None = Field(default=None, ge=1, le=3)
    capacity_target_tier: int | None = Field(default=None, ge=0)
    capacity_downsize_tier: int | None = Field(default=None, ge=0)
    cancel_capacity_project_id: str | None = Field(default=None, min_length=1)
    enterprise_bids: tuple[EnterpriseBid, ...] = ()
    market_research: tuple[MarketResearchRequest, ...] = ()

    @model_validator(mode="after")
    def exclusive_capacity_action(self) -> CompanyDecision:
        if (
            sum(
                value is not None
                for value in (
                    self.capacity_target_tier,
                    self.capacity_downsize_tier,
                    self.cancel_capacity_project_id,
                )
            )
            > 1
        ):
            raise ValueError("only one capacity action is allowed per decision")
        return self


class InboundShipment(StrictModel):
    shipment_id: str
    product_id: str
    quantity: int = Field(gt=0)
    arrival_day: int = Field(gt=0)


class BacklogOrder(StrictModel):
    order_id: str
    source: OrderSource
    product_id: str
    segment_id: str | None = None
    quantity: int = Field(gt=0)
    unit_price_cents: int = Field(ge=0)
    created_day: int = Field(ge=0)
    delivery_due_day: int | None = Field(default=None, gt=0)


class InnovationContribution(StrictModel):
    contribution_id: str
    product_id: str
    capital_cents: int = Field(gt=0)
    maturity_day: int = Field(gt=0)


class InnovationProject(StrictModel):
    project_id: str
    tier: int = Field(ge=1, le=3)
    started_day: int = Field(ge=0)
    completion_day: int = Field(gt=0)
    capital_gain_cents: int = Field(gt=0)
    status: JobStatus = JobStatus.PENDING


class CapacityProject(StrictModel):
    project_id: str
    target_tier: int = Field(ge=0)
    completion_day: int = Field(gt=0)
    status: JobStatus = JobStatus.PENDING
    kind: CapacityChangeKind = CapacityChangeKind.EXPAND
    source_tier: int = Field(default=0, ge=0)
    started_day: int = Field(default=0, ge=0)
    paid_cents: int = Field(default=0, ge=0)
    refund_cents: int = Field(default=0, ge=0)
    cancelled_day: int | None = Field(default=None, ge=0)


class NormalBelief(StrictModel):
    mean: float
    variance: float = Field(gt=0.0)
    updated_day: int = Field(ge=0)


class ResearchReport(StrictModel):
    report_id: str
    segment_id: str
    level: int = Field(ge=1, le=5)
    snapshot_day: int = Field(ge=0)
    completed_day: int = Field(ge=0)
    posterior: dict[str, NormalBelief]


class MarketResearchJob(StrictModel):
    job_id: str
    segment_id: str
    level: int = Field(ge=1, le=5)
    completion_day: int = Field(gt=0)
    status: JobStatus = JobStatus.PENDING
    snapshot_day: int = Field(default=0, ge=0)
    snapshot: dict[str, float] = Field(default_factory=dict)


class CompanyDailyRecord(StrictModel):
    day: int = Field(gt=0)
    revenue_cents: int = Field(ge=0)
    costs_cents: int = Field(ge=0)
    expense_breakdown: ExpenseBreakdown = Field(default_factory=ExpenseBreakdown)
    refunds_cents: int = Field(default=0, ge=0)
    cogs_cents: int = Field(default=0, ge=0)
    ending_cash_cents: int
    consumer_orders: int = Field(ge=0)
    fulfilled_units: int = Field(ge=0)
    abandoned_units: int = Field(ge=0)
    support_backlog: int = Field(ge=0)
    fulfillment_backlog_units: int = Field(ge=0)
    capacity_tier: int = Field(ge=0)
    fulfilled_by_product: dict[str, int] = Field(default_factory=dict)
    fulfilled_by_segment: dict[str, int] = Field(default_factory=dict)
    failures_by_segment: dict[str, int] = Field(default_factory=dict)
    ending_inventory: dict[str, int] = Field(default_factory=dict)
    satisfaction: dict[str, float] = Field(default_factory=dict)

    @property
    def gross_profit_cents(self) -> int:
        return self.revenue_cents - self.cogs_cents

    @property
    def net_cashflow_cents(self) -> int:
        return self.revenue_cents + self.refunds_cents - self.costs_cents


class CompanyState(StrictModel):
    company_id: str
    status: CompanyStatus = CompanyStatus.ACTIVE
    cash_cents: int
    initial_cash_cents: int
    initial_enterprise_value_cents: int = Field(ge=0)
    prices_cents: dict[str, int]
    promotions_cents: dict[str, int]
    reorder_targets: dict[str, int]
    inventory: dict[str, int]
    reserved_inventory: dict[str, int]
    inbound_shipments: list[InboundShipment] = Field(default_factory=list)
    backlog: list[BacklogOrder] = Field(default_factory=list)
    marketing_budget_cents: dict[str, dict[str, int]] = Field(default_factory=dict)
    goodwill: dict[str, float] = Field(default_factory=dict)
    awareness: dict[str, float] = Field(default_factory=dict)
    satisfaction: dict[str, float] = Field(default_factory=dict)
    loyalty: dict[str, float] = Field(default_factory=dict)
    development_budget_cents: dict[str, int] = Field(default_factory=dict)
    knowledge_capital_cents: dict[str, float] = Field(default_factory=dict)
    innovation_pipeline: list[InnovationContribution] = Field(default_factory=list)
    innovation_projects: list[InnovationProject] = Field(default_factory=list)
    reliability_budget_cents: int = Field(default=0, ge=0)
    support_budget_cents: int = Field(default=0, ge=0)
    support_backlog: int = Field(default=0, ge=0)
    capacity_tier: int = Field(default=0, ge=0)
    capacity_projects: list[CapacityProject] = Field(default_factory=list)
    research_jobs: list[MarketResearchJob] = Field(default_factory=list)
    research_reports: list[ResearchReport] = Field(default_factory=list)
    beliefs: dict[str, dict[str, NormalBelief]] = Field(default_factory=dict)
    daily_history: list[CompanyDailyRecord] = Field(default_factory=list)
    cumulative_revenue_cents: int = 0
    cumulative_cost_cents: int = 0
    cumulative_refunds_cents: int = Field(default=0, ge=0)
    action_receipts: list[ActionReceipt] = Field(default_factory=list)
    cumulative_consumer_orders: int = 0
    cumulative_fulfilled_units: int = 0
    cumulative_abandoned_units: int = 0


class EnterpriseTender(StrictModel):
    tender_id: str
    product_id: str
    quantity: int = Field(gt=0)
    minimum_quality: float = Field(ge=0.0)
    reservation_price_cents: int = Field(gt=0)
    opened_day: int = Field(ge=0)
    deadline_day: int = Field(gt=0)
    delivery_due_day: int | None = Field(default=None, gt=0)
    bids: dict[str, int] = Field(default_factory=dict)
    winner_company_id: str | None = None
    winning_unit_price_cents: int | None = None
    settled: bool = False


class WorldState(StrictModel):
    scenario_id: str = SCENARIO_VERSION
    version: int = Field(default=0, ge=0)
    day: int = Field(default=0, ge=0)
    macro_index: float = 1.0
    macro_history: dict[int, float] = Field(default_factory=dict)
    market: MarketState = Field(default_factory=MarketState)
    market_signals: dict[int, PublicMarketSignal] = Field(default_factory=dict)
    initial_company_count: int = Field(default=0, ge=0)
    companies: dict[str, CompanyState]
    tenders: list[EnterpriseTender] = Field(default_factory=list)


class ResearchLevelInfo(StrictModel):
    level: int = Field(ge=1, le=5)
    cost_cents: int = Field(ge=0)
    delay_days: int = Field(ge=1)
    signal_std: float = Field(gt=0.0)


class InnovationTierInfo(StrictModel):
    tier: int = Field(ge=1, le=3)
    cost_cents: int = Field(ge=0)
    median_days: int = Field(ge=1)
    expected_capital_gain_cents: int = Field(gt=0)


class InnovationProjectInfo(StrictModel):
    """Agent-visible R&D status without unrevealed random outcomes."""

    project_id: str
    tier: int = Field(ge=1, le=3)
    started_day: int = Field(ge=0)
    median_completion_day: int = Field(gt=0)
    expected_capital_gain_cents: int = Field(gt=0)
    status: JobStatus
    completed_day: int | None = Field(default=None, gt=0)
    realized_capital_gain_cents: int | None = Field(default=None, gt=0)


class CompanyTenderInfo(StrictModel):
    """One company's own bid and eventual outcome without rival bid leakage."""

    tender_id: str
    product_id: str
    quantity: int = Field(gt=0)
    opened_day: int = Field(ge=0)
    deadline_day: int = Field(gt=0)
    delivery_due_day: int | None = Field(default=None, gt=0)
    own_bid_cents: int | None = Field(default=None, gt=0)
    status: Literal["open", "submitted", "won", "lost", "no_bid"]
    winning_unit_price_cents: int | None = Field(default=None, gt=0)


class CompanyObservation(StrictModel):
    scenario_id: str
    world_version: int
    day: int
    total_days: int = Field(gt=0)
    days_remaining: int = Field(ge=0)
    days_per_decision_window: int = Field(gt=0)
    official_score_description: str = OFFICIAL_SCORE_DESCRIPTION
    company: CompanyState
    public_prices_cents: dict[str, dict[str, int]]
    public_promotions_cents: dict[str, dict[str, int]]
    public_effective_prices_cents: dict[str, dict[str, int]]
    open_tenders: tuple[EnterpriseTender, ...]
    published_macro_index: float
    published_market_signal: PublicMarketSignal = Field(default_factory=PublicMarketSignal)
    capacity_policy: CapacityPolicyConfig = Field(default_factory=CapacityPolicyConfig)
    capacity_action_quotes: tuple[CapacityActionQuote, ...] = ()
    product_catalog: tuple[ProductConfig, ...] = ()
    capacity_catalog: tuple[CapacityTierConfig, ...] = ()
    research_catalog: tuple[ResearchLevelInfo, ...] = ()
    innovation_catalog: tuple[InnovationTierInfo, ...] = ()
    innovation_projects: tuple[InnovationProjectInfo, ...] = ()
    tender_history: tuple[CompanyTenderInfo, ...] = ()


class CompanyDayOutcome(StrictModel):
    company_id: str
    consumer_orders: int = Field(ge=0)
    fulfilled_units: int = Field(ge=0)
    abandoned_units: int = Field(ge=0)
    revenue_cents: int = Field(ge=0)
    costs_cents: int = Field(ge=0)
    expense_breakdown: ExpenseBreakdown = Field(default_factory=ExpenseBreakdown)
    refunds_cents: int = Field(default=0, ge=0)
    cogs_cents: int = Field(default=0, ge=0)
    ending_cash_cents: int
    fulfilled_by_product: dict[str, int] = Field(default_factory=dict)
    fulfilled_by_segment: dict[str, int] = Field(default_factory=dict)
    failures_by_segment: dict[str, int] = Field(default_factory=dict)

    @property
    def gross_profit_cents(self) -> int:
        return self.revenue_cents - self.cogs_cents

    @property
    def net_cashflow_cents(self) -> int:
        return self.revenue_cents + self.refunds_cents - self.costs_cents


class DayResult(StrictModel):
    day: int = Field(gt=0)
    world_version: int = Field(gt=0)
    demand_by_segment: dict[str, int]
    company_outcomes: dict[str, CompanyDayOutcome]
    state_hash: str


class WindowResult(StrictModel):
    start_day: int = Field(ge=0)
    end_day: int = Field(gt=0)
    world_version: int = Field(gt=0)
    days: tuple[DayResult, ...]
    observations: dict[str, CompanyObservation]
    state: WorldState


class AgentBudget(StrictModel):
    max_provider_turns: int = Field(default=32, gt=0, le=32)
    max_tool_calls: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=131_072, gt=0)
    timeout_seconds: float | None = Field(default=3_600, gt=0.0)

    def remaining_seconds(self, usage: AgentUsage) -> float | None:
        return (
            None
            if self.timeout_seconds is None
            else max(0.0, self.timeout_seconds - usage.latency_ms / 1000)
        )

    def exhausted(self, usage: AgentUsage) -> bool:
        return (
            usage.provider_turns >= self.max_provider_turns
            or usage.finalization_turns >= 4
            or (
                self.max_output_tokens is not None and usage.output_tokens >= self.max_output_tokens
            )
            or (self.max_tool_calls is not None and usage.tool_calls >= self.max_tool_calls)
            or self.remaining_seconds(usage) == 0
        )


class AgentUsage(StrictModel):
    provider_turns: int = Field(default=0, ge=0)
    finalization_turns: int = Field(default=0, ge=0, le=4)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_finalization_turns(self) -> Self:
        if self.finalization_turns > self.provider_turns:
            raise ValueError("finalization replies cannot exceed decision replies")
        return self

    def plus(self, other: AgentUsage) -> AgentUsage:
        """Accumulate available usage from one decision attempt."""
        return AgentUsage.model_validate(
            {name: getattr(self, name) + getattr(other, name) for name in type(self).model_fields}
        )


class DecisionDiagnostics(StrictModel):
    """Recoverable model failures, separate from the window's final outcome."""

    protocol_errors: int = Field(default=0, ge=0)
    tool_errors: int = Field(default=0, ge=0)
    recovered: bool = False
    budget_usage_incomplete: bool = False

    def plus(self, other: DecisionDiagnostics) -> DecisionDiagnostics:
        return DecisionDiagnostics(
            protocol_errors=self.protocol_errors + other.protocol_errors,
            tool_errors=self.tool_errors + other.tool_errors,
            recovered=other.recovered,
            budget_usage_incomplete=self.budget_usage_incomplete or other.budget_usage_incomplete,
        )


class DecisionWindowRequest(StrictModel):
    run_id: str
    company_id: str
    world_version: int = Field(ge=0)
    observation: CompanyObservation
    budget: AgentBudget = Field(default_factory=AgentBudget)
    prior_usage: AgentUsage = Field(default_factory=AgentUsage)
    prior_diagnostics: DecisionDiagnostics = Field(default_factory=DecisionDiagnostics)


class RecoverySummary(StrictModel):
    """Non-scoring recovery totals; a zero denominator has no recovery rate."""

    protocol_errors: int = Field(default=0, ge=0)
    tool_errors: int = Field(default=0, ge=0)
    windows_with_errors: int = Field(default=0, ge=0)
    recovered_windows: int = Field(default=0, ge=0)
    infrastructure_timeout_count: int = Field(default=0, ge=0)

    @property
    def recovery_rate(self) -> float | None:
        return (
            self.recovered_windows / self.windows_with_errors if self.windows_with_errors else None
        )

    def include(
        self, diagnostics: DecisionDiagnostics, *, infrastructure_timeout: bool = False
    ) -> None:
        """Accumulate one finalized decision's model-error diagnostics."""

        self.protocol_errors += diagnostics.protocol_errors
        self.tool_errors += diagnostics.tool_errors
        self.windows_with_errors += int(diagnostics.protocol_errors + diagnostics.tool_errors > 0)
        self.recovered_windows += int(diagnostics.recovered)
        self.infrastructure_timeout_count += int(infrastructure_timeout)

    def merge(self, other: RecoverySummary) -> None:
        """Accumulate another run's already grouped diagnostics."""

        self.protocol_errors += other.protocol_errors
        self.tool_errors += other.tool_errors
        self.windows_with_errors += other.windows_with_errors
        self.recovered_windows += other.recovered_windows
        self.infrastructure_timeout_count += other.infrastructure_timeout_count


class ProviderFailure(StrictModel):
    """Structured transport diagnostics, never model prompts or raw response frames."""

    category: Literal[
        "transport",
        "http",
        "stream_decode",
        "stream_read",
        "accounting",
        "worker",
        "auth",
        "identity",
        "rate_limit",
        "internal",
        "unknown",
    ] = "unknown"
    message: str
    retryable: bool = True
    http_status: int | None = Field(default=None, ge=100, le=599)
    reason: str | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)
    attempt: int | None = Field(default=None, ge=1)
    frame_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    frame_bytes: int | None = Field(default=None, ge=0)


class DecisionWindowResult(StrictModel):
    status: Literal["sealed", "timeout", "protocol_error", "provider_error", "budget_exhausted"]
    decision: CompanyDecision | None = None
    usage: AgentUsage = Field(default_factory=AgentUsage)
    diagnostics: DecisionDiagnostics = Field(default_factory=DecisionDiagnostics)
    failure: ProviderFailure | None = None
    trace_path: str | None = None
    trace_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error: str | None = None
