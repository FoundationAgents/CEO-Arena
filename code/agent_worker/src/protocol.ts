import { Schema } from "effect";

export const DECISION_PROTOCOL_VERSION = "ceos-decision-v9";

const StringNumberMap = Schema.Record(Schema.String, Schema.Number);
const NestedStringNumberMap = Schema.Record(Schema.String, StringNumberMap);
const SqlValue = Schema.Union([Schema.String, Schema.Number, Schema.Null]);

const Shipment = Schema.Struct({
  shipment_id: Schema.String,
  product_id: Schema.String,
  quantity: Schema.Number,
  arrival_day: Schema.Number,
});

const BacklogOrder = Schema.Struct({
  order_id: Schema.String,
  source: Schema.String,
  product_id: Schema.String,
  segment_id: Schema.optional(Schema.NullOr(Schema.String)),
  quantity: Schema.Number,
  unit_price_cents: Schema.Number,
  created_day: Schema.Number,
});

const ResearchJob = Schema.Struct({ segment_id: Schema.String });
const ResearchReport = Schema.Struct({ segment_id: Schema.String });
const CapacityProject = Schema.Struct({
  project_id: Schema.String,
  target_tier: Schema.Number,
  completion_day: Schema.Number,
  status: Schema.String,
});
const CapacityTier = Schema.Struct({
  tier: Schema.Number,
  units_per_day: Schema.Number,
  daily_cost_cents: Schema.Number,
  expansion_cost_cents: Schema.Number,
  expansion_lead_days: Schema.Number,
});
const CapacityActionQuote = Schema.Struct({
  action: Schema.Literals([
    "expand_capacity",
    "downsize_capacity",
    "cancel_capacity_project",
  ]),
  target_tier: Schema.optional(Schema.NullOr(Schema.Number)),
  project_id: Schema.optional(Schema.NullOr(Schema.String)),
  charged_cents: Schema.Number,
  refunded_cents: Schema.Number,
  effective_day: Schema.optional(Schema.NullOr(Schema.Number)),
});

const DailyRecord = Schema.Struct({
  day: Schema.Number,
  revenue_cents: Schema.Number,
  costs_cents: Schema.Number,
  refunds_cents: Schema.Number,
  cogs_cents: Schema.Number,
  consumer_orders: Schema.Number,
  fulfilled_units: Schema.Number,
  abandoned_units: Schema.Number,
});

const Company = Schema.Struct({
  company_id: Schema.String,
  cash_cents: Schema.Number,
  prices_cents: StringNumberMap,
  promotions_cents: StringNumberMap,
  reorder_targets: StringNumberMap,
  inventory: StringNumberMap,
  reserved_inventory: StringNumberMap,
  inbound_shipments: Schema.Array(Shipment),
  backlog: Schema.Array(BacklogOrder),
  marketing_budget_cents: NestedStringNumberMap,
  goodwill: StringNumberMap,
  awareness: StringNumberMap,
  satisfaction: StringNumberMap,
  loyalty: StringNumberMap,
  development_budget_cents: StringNumberMap,
  knowledge_capital_cents: StringNumberMap,
  reliability_budget_cents: Schema.Number,
  support_budget_cents: Schema.Number,
  support_backlog: Schema.Number,
  capacity_tier: Schema.Number,
  capacity_projects: Schema.Array(CapacityProject),
  research_jobs: Schema.Array(ResearchJob),
  research_reports: Schema.Array(ResearchReport),
  daily_history: Schema.Array(DailyRecord),
});

const Tender = Schema.Struct({
  tender_id: Schema.String,
  product_id: Schema.String,
  quantity: Schema.Number,
  minimum_quality: Schema.Number,
  reservation_price_cents: Schema.Number,
  opened_day: Schema.Number,
  deadline_day: Schema.Number,
  settled: Schema.Boolean,
});

const Observation = Schema.Struct({
  scenario_id: Schema.String,
  world_version: Schema.Number,
  day: Schema.Number,
  total_days: Schema.Number,
  days_remaining: Schema.Number,
  days_per_decision_window: Schema.Number,
  official_score_description: Schema.String,
  company: Company,
  public_prices_cents: NestedStringNumberMap,
  public_promotions_cents: NestedStringNumberMap,
  public_effective_prices_cents: NestedStringNumberMap,
  open_tenders: Schema.Array(Tender),
  published_macro_index: Schema.Number,
  capacity_catalog: Schema.Array(CapacityTier),
  capacity_action_quotes: Schema.Array(CapacityActionQuote),
  innovation_catalog: Schema.Array(
    Schema.Struct({
      tier: Schema.Number,
      cost_cents: Schema.Number,
    }),
  ),
  research_catalog: Schema.Array(
    Schema.Struct({
      level: Schema.Number,
      cost_cents: Schema.Number,
    }),
  ),
});

const Counter = Schema.Int.check(Schema.isGreaterThanOrEqualTo(0));
export const AgentUsageSchema = Schema.Struct({
  provider_turns: Counter,
  finalization_turns: Counter,
  tool_calls: Counter,
  input_tokens: Counter,
  output_tokens: Counter,
  reasoning_tokens: Counter,
  cache_read_tokens: Counter,
  cache_write_tokens: Counter,
  latency_ms: Counter,
});
export type AgentUsage = typeof AgentUsageSchema.Type;

export const DecisionDiagnosticsSchema = Schema.Struct({
  protocol_errors: Counter,
  tool_errors: Counter,
  recovered: Schema.Boolean,
  budget_usage_incomplete: Schema.optional(Schema.Boolean),
});
export type DecisionDiagnostics = typeof DecisionDiagnosticsSchema.Type;

const AgentBudget = Schema.Struct({
  max_provider_turns: Schema.Int.check(
    Schema.isBetween({ minimum: 1, maximum: 32 }),
  ),
  max_tool_calls: Schema.NullOr(Schema.Number),
  max_output_tokens: Schema.NullOr(Schema.Number),
  timeout_seconds: Schema.NullOr(Schema.Number),
});

const DecisionWindowRequest = Schema.Struct({
  run_id: Schema.String,
  company_id: Schema.String,
  world_version: Schema.Number,
  observation: Observation,
  budget: AgentBudget,
  prior_usage: Schema.optional(AgentUsageSchema),
  prior_diagnostics: Schema.optional(DecisionDiagnosticsSchema),
});

export const WorkerDecisionRequest = Schema.Struct({
  type: Schema.Literal("decide"),
  request_id: Schema.String,
  request: DecisionWindowRequest,
});

export const WorkerShutdownRequest = Schema.Struct({
  type: Schema.Literal("shutdown"),
});

export const WorkerQueryResult = Schema.Struct({
  type: Schema.Literal("query_result"),
  request_id: Schema.String,
  query_id: Schema.String,
  result: Schema.optional(
    Schema.NullOr(
      Schema.Struct({
        columns: Schema.Array(Schema.String),
        rows: Schema.Array(Schema.Array(SqlValue)),
        truncated: Schema.Boolean,
      }),
    ),
  ),
  error: Schema.optional(Schema.NullOr(Schema.String)),
});

export const WorkerValidationResult = Schema.Struct({
  type: Schema.Literal("validation_result"),
  request_id: Schema.String,
  validation_id: Schema.String,
  error: Schema.optional(Schema.NullOr(Schema.String)),
});

export const WorkerInput = Schema.Union([
  WorkerDecisionRequest,
  WorkerShutdownRequest,
  WorkerQueryResult,
  WorkerValidationResult,
]);

export type DecisionRequest = typeof WorkerDecisionRequest.Type;
export type QueryResult = NonNullable<
  (typeof WorkerQueryResult.Type)["result"]
>;

export interface ProductDecision {
  readonly product_id: string;
  readonly price_cents?: number;
  readonly promotion_cents?: number;
  readonly reorder_target?: number;
  readonly development_budget_cents?: number;
}

export interface MarketingAllocation {
  readonly segment_id: string;
  readonly channel: string;
  readonly daily_budget_cents: number;
}

export interface EnterpriseBid {
  readonly tender_id: string;
  readonly unit_price_cents: number;
}

export interface MarketResearchRequest {
  readonly segment_id: string;
  readonly level: number;
}

export interface CompanyDecision {
  readonly company_id: string;
  readonly world_version: number;
  readonly products: ReadonlyArray<ProductDecision>;
  readonly marketing: ReadonlyArray<MarketingAllocation>;
  readonly reliability_budget_cents?: number;
  readonly support_budget_cents?: number;
  readonly research_project_tier?: number;
  readonly capacity_target_tier?: number;
  readonly capacity_downsize_tier?: number;
  readonly cancel_capacity_project_id?: string;
  readonly enterprise_bids: ReadonlyArray<EnterpriseBid>;
  readonly market_research: ReadonlyArray<MarketResearchRequest>;
}

export interface DecisionPreview {
  readonly upfront_charges_cents: number;
  readonly upfront_refunds_cents: number;
  readonly cash_after_upfront_cents: number;
  readonly daily_policy_cost_cents: number;
  readonly affordable: boolean;
  readonly note: string;
}

export interface DecisionResult {
  readonly status:
    | "sealed"
    | "protocol_error"
    | "provider_error"
    | "budget_exhausted";
  readonly decision?: CompanyDecision;
  readonly usage: AgentUsage;
  readonly diagnostics: DecisionDiagnostics;
  readonly trace_path?: string;
  readonly trace_sha256?: string;
  readonly error?: string;
  readonly failure?: ProviderFailure;
}

export interface ProviderFailure {
  readonly category:
    | "transport"
    | "http"
    | "stream_decode"
    | "stream_read"
    | "accounting"
    | "worker"
    | "auth"
    | "identity"
    | "rate_limit"
    | "internal"
    | "unknown";
  readonly message: string;
  readonly retryable: boolean;
  readonly http_status?: number;
  readonly reason?: string;
  readonly retry_after_seconds?: number;
  readonly attempt?: number;
  readonly frame_sha256?: string;
  readonly frame_bytes?: number;
}

export const decodeWorkerInput = Schema.decodeUnknownSync(
  Schema.fromJsonString(WorkerInput),
);

export const encodeLine = (value: unknown) => `${JSON.stringify(value)}\n`;
