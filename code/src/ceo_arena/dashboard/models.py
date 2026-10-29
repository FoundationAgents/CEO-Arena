"""Typed browser-facing projections for live and completed benchmark runs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ceo_arena.models import (
    ActionReceipt,
    ExpenseBreakdown,
    ProviderFailure,
    PublicMarketSignal,
    StrictModel,
)
from ceo_arena.runtime.performance import PerformanceStatus

type DashboardRunStatus = Literal[
    "starting",
    "queued",
    "waiting_retry",
    "waiting_seed",
    "running",
    "interrupted",
    "failed",
    "complete",
    "invalid",
]
type DashboardJobStatus = Literal[
    "queued",
    "running",
    "interrupted",
    "stopped",
    "failed",
    "completed",
]
type DashboardRunControlAction = Literal["pause", "resume", "none"]
type DashboardResourceStatus = Literal[
    "healthy", "holding", "pressure", "critical", "unavailable", "disabled", "monitoring"
]

type DashboardDecisionStage = Literal["completed", "pending", "collecting"]
type DashboardDecisionStatus = Literal[
    "sealed",
    "budget_exhausted",
    "timeout",
    "protocol_error",
    "provider_error",
]
type DashboardActionCategory = Literal[
    "product",
    "marketing",
    "operations",
    "research",
    "capacity",
    "enterprise",
    "market_research",
]


class SeriesPoint(StrictModel):
    day: int = Field(ge=0)
    value: int


class DashboardAction(StrictModel):
    category: DashboardActionCategory
    label: str
    value: str


class DashboardDecision(StrictModel):
    window_index: int = Field(ge=0)
    start_day: int = Field(ge=0)
    end_day: int | None = Field(default=None, gt=0)
    stage: DashboardDecisionStage
    status: DashboardDecisionStatus
    summary: str
    actions: tuple[DashboardAction, ...]
    provider_turns: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    failure: ProviderFailure | None = None

    submitted_at: datetime | None = None
    settled_at: datetime | None = None
    decision_duration_ms: int | None = Field(default=None, ge=0)


class DashboardDailyCashflow(StrictModel):
    """Recent cash ledger: purchase payments are not cost of goods sold."""

    day: int = Field(ge=0)
    opening_cash_cents: int
    revenue_cents: int = Field(ge=0)
    refunds_cents: int = Field(ge=0)
    payments_cents: int = Field(ge=0)
    cogs_cents: int = Field(ge=0)
    gross_profit_cents: int
    net_cashflow_cents: int
    ending_cash_cents: int
    expenses: ExpenseBreakdown


class DashboardCompany(StrictModel):
    company_id: str
    model_id: str
    status: str
    score_cents: int
    detail_available: bool = True
    cash_cents: int | None
    revenue_cents: int | None
    cost_cents: int | None
    refunds_cents: int | None = None
    gross_profit_cents: int | None = None
    daily_cashflow: tuple[DashboardDailyCashflow, ...] = ()
    action_receipts: tuple[ActionReceipt, ...] = ()
    inventory_units: int | None = Field(ge=0)
    backlog_units: int | None = Field(ge=0)
    support_backlog: int | None = Field(ge=0)
    satisfaction: float | None
    last_agent_status: str
    last_decision: str
    provider_turns: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    cash_series: tuple[SeriesPoint, ...]
    trajectory: tuple[DashboardDecision, ...]


class DashboardEvent(StrictModel):
    sequence: int = Field(ge=0)
    kind: str
    label: str
    detail: str
    day: int | None = Field(default=None, ge=0)
    recorded_at: datetime | None = None
    tone: Literal["info", "success", "warning", "danger"] = "info"


class DashboardRunSummary(StrictModel):
    run_ref: str = Field(pattern=r"^[0-9a-f]{16}$")
    run_id: str
    campaign_id: str | None = None
    seed: int | None = None
    scenario_id: str
    model_ids: tuple[str, ...]
    job_status: DashboardJobStatus | None = None
    status: DashboardRunStatus
    phase: str
    day: int = Field(ge=0)
    total_days: int = Field(gt=0)
    progress: float = Field(ge=0.0, le=1.0)
    company_count: int = Field(ge=0)
    active_company_count: int = Field(ge=0)
    updated_at: datetime
    error: str | None = None
    revision: int | None = Field(default=None, ge=0)
    control_action: DashboardRunControlAction = "none"
    control_label: str = "Read-only"


class DashboardScheduler(StrictModel):
    """Safe live resource measurements without logs or private process data."""

    timestamp: datetime
    active_runs: int = Field(ge=0)
    run_limit: int = Field(ge=1)
    max_runs: int = Field(ge=1)
    active_decisions: int = Field(ge=0)
    resident_workers: int = Field(ge=0)
    waiting_decisions: int = Field(ge=0)
    decision_limit: int | None = Field(default=None, ge=1)
    max_decisions: int | None = Field(default=None, ge=1)
    available_memory_mb: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    resource_status: DashboardResourceStatus


class DashboardCampaignResources(StrictModel):
    campaign_id: str
    scheduler: DashboardScheduler | None = None


class DashboardRunList(StrictModel):
    runs: tuple[DashboardRunSummary, ...]
    resources: tuple[DashboardCampaignResources, ...] = ()


class DashboardRun(DashboardRunSummary):
    performance: PerformanceStatus | None = None
    economic_revision: str | None = None
    retry_not_before: datetime | None = None
    last_error: str | None = None
    attempt: int = Field(default=0, ge=0)
    attempt_limit: int | None = Field(default=None, ge=1)
    detail_available: bool = True
    macro_index: float | None
    market_signal: PublicMarketSignal | None = None
    total_score_cents: int
    total_cash_cents: int | None
    completed_windows: int = Field(ge=0)
    current_window: int = Field(ge=0)
    decision_status_counts: dict[str, int]
    companies: tuple[DashboardCompany, ...]
    recent_events: tuple[DashboardEvent, ...]
    artifacts: dict[str, str]


class RunControlRequest(StrictModel):
    action: Literal["pause", "resume"]
    revision: int = Field(ge=0)


class RunControlReceipt(StrictModel):
    run_ref: str = Field(pattern=r"^[0-9a-f]{16}$")
    action: Literal["pause", "resume"]
    revision: int = Field(ge=0)
    status: DashboardJobStatus
    control_action: DashboardRunControlAction
    control_label: str
    message: str


class DashboardUpdate(StrictModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
