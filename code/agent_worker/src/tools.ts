import { Tool, ToolFailure } from "@opencode-ai/llm";
import { Effect, Schema } from "effect";

import { type CompanyBridge, ToolRejectedError } from "./bridge";
import { AgentToolGateway, DraftError } from "./draft";

const PositiveInt = Schema.Int.check(Schema.isGreaterThan(0));
const NonNegativeInt = Schema.Int.check(Schema.isGreaterThanOrEqualTo(0));
// Empty Struct projects to object|array in pinned Effect; these tools require {}.
const EmptyParameters = Schema.Record(Schema.String, Schema.Never);
const QueryRowLimit = Schema.Int.check(
  Schema.isGreaterThan(0),
  Schema.isLessThanOrEqualTo(10_000),
);
const MutationResult = Schema.Struct({
  staged: Schema.Boolean,
  executed: Schema.Boolean,
  message: Schema.String,
});
const DecisionPreviewSchema = Schema.Struct({
  upfront_charges_cents: Schema.Int,
  upfront_refunds_cents: Schema.Int,
  cash_after_upfront_cents: Schema.Int,
  daily_policy_cost_cents: Schema.Int,
  affordable: Schema.Boolean,
  note: Schema.String,
});
const QuerySuccess = Schema.Struct({
  columns: Schema.Array(Schema.String),
  rows: Schema.Array(
    Schema.Array(Schema.Union([Schema.String, Schema.Number, Schema.Null])),
  ),
  truncated: Schema.Boolean,
});
const ProductDecisionSchema = Schema.Struct({
  product_id: Schema.String,
  price_cents: Schema.optional(PositiveInt),
  promotion_cents: Schema.optional(NonNegativeInt),
  reorder_target: Schema.optional(NonNegativeInt),
  development_budget_cents: Schema.optional(NonNegativeInt),
});
const MarketingAllocationSchema = Schema.Struct({
  segment_id: Schema.String,
  channel: Schema.String,
  daily_budget_cents: NonNegativeInt,
});
const EnterpriseBidSchema = Schema.Struct({
  tender_id: Schema.String,
  unit_price_cents: PositiveInt,
});
const MarketResearchSchema = Schema.Struct({
  segment_id: Schema.String,
  level: Schema.Int.check(Schema.isBetween({ minimum: 1, maximum: 5 })),
});
export const CompanyDecisionSchema = Schema.Struct({
  company_id: Schema.String,
  world_version: NonNegativeInt,
  products: Schema.Array(ProductDecisionSchema),
  marketing: Schema.Array(MarketingAllocationSchema),
  reliability_budget_cents: Schema.optional(NonNegativeInt),
  support_budget_cents: Schema.optional(NonNegativeInt),
  research_project_tier: Schema.optional(
    Schema.Int.check(Schema.isBetween({ minimum: 1, maximum: 3 })),
  ),
  capacity_target_tier: Schema.optional(NonNegativeInt),
  capacity_downsize_tier: Schema.optional(NonNegativeInt),
  cancel_capacity_project_id: Schema.optional(Schema.String),
  enterprise_bids: Schema.Array(EnterpriseBidSchema),
  market_research: Schema.Array(MarketResearchSchema),
});

export const TOOL_DESCRIPTIONS = {
  query_company: [
    "Run one read-only SQLite SELECT against this company's private projection.",
    "Identity and world version are already bound; never include them as parameters.",
    "Available tables: scenario, company, products, segments, marketing, public_prices,",
    "open_tenders, tender_history, inbound_shipments, backlog, research_reports, research_jobs,",
    "beliefs, innovation_pipeline, innovation_projects, capacity_projects,",
    "product_catalog, capacity_catalog, capacity_policy, capacity_action_quotes, research_catalog, innovation_catalog,",
    "action_receipts, market_signal, daily_financials, daily_cashflow, daily_expenses, daily_product_sales, daily_segment_service.",
  ].join(" "),
  set_product: [
    "Stage persistent settings for one product: price, promotion, reorder target, and/or daily development budget.",
    "The development budget is charged every simulated day.",
    "Omitted fields remain unchanged, including across decision windows; set the development budget to 0 to stop it.",
  ].join(" "),
  set_marketing:
    "Stage one persistent daily marketing budget for a segment and channel. It is charged every simulated day until explicitly changed; set it to 0 to stop it.",
  set_operations:
    "Stage persistent daily reliability and customer-support budgets. Both are charged every simulated day until explicitly changed; set them to 0 to stop them.",
  start_research_project:
    "Stage one company-wide product-quality innovation project of tier 1, 2, or 3. It charges a one-time upfront cost, completes after a stochastic delay, and adds product knowledge capital; it is not consumer market research.",
  expand_capacity:
    "Stage a quoted higher capacity tier. Construction is delayed; once active, its operating cost is charged daily. Inspect capacity_action_quotes; a lower tier is not an expansion. Replaces any other capacity action in this draft.",
  downsize_capacity:
    "Stage a quoted reduction of installed capacity, with an adjustment fee and delay. Past construction spending is not refunded; inventory, outstanding orders, and their obligations remain. Replaces any other capacity action in this draft.",
  cancel_capacity_project:
    "Stage cancellation of one quoted unfinished expansion by project_id. Refunds exclude sunk expenditure and cancellation costs; cancelled work never completes. Replaces any other capacity action in this draft.",
  submit_enterprise_bid:
    "Stage one confidential unit-price bid for an open enterprise tender.",
  request_market_research:
    "Stage one customer market-research request for a segment. It charges a one-time upfront cost and later returns a noisy private report about demand, preferences and quality requirements at its sampling date; it does not improve product quality.",
  preview_decision:
    "Inspect combined one-time charges, refunds, cash remaining and current daily policy costs without sealing. Excludes future procurement, holding and failure costs; affordability of upfront commitments is not a profit or solvency forecast.",
  reset_decision:
    "Discard all unsealed draft changes, including one-time commitments. File memory is immediately persistent and is not reset. Current world policies are unaffected. Consumed turns, tools, tokens and time are not restored; re-stage any desired changes before sealing.",
  seal_decision: [
    "Finalize and return the staged CompanyDecision.",
    "Call this alone in its provider turn. The draft is sealed only after economic validation succeeds; otherwise read the error, correct the draft, and retry within the same budget.",
    "This must be the final tool call; no economic action occurs before sealing.",
    "If no operating change is staged, existing recurring policies continue; one-time actions are not repeated.",
  ].join(" "),
} as const;

const mutation = (change: () => void) =>
  toolAction(() => {
    change();
    return {
      staged: true,
      executed: false,
      message:
        "Draft updated only. Execution occurs after sealing and market settlement; inspect action_receipts for actual outcome.",
    };
  });

const toolAction = <Value>(operation: () => Value | Promise<Value>) =>
  Effect.tryPromise({
    try: async () => operation(),
    catch: (error) => error,
  }).pipe(
    Effect.catch((error) =>
      error instanceof DraftError || error instanceof ToolRejectedError
        ? Effect.fail(
            new ToolFailure({
              message: error.message,
              error: { category: "tool" },
            }),
          )
        : Effect.die(error),
    ),
  );

export const createTools = (
  gateway: AgentToolGateway,
  queries: CompanyBridge,
) => ({
  query_company: Tool.make({
    description: TOOL_DESCRIPTIONS.query_company,
    parameters: Schema.Struct({
      sql: Schema.String.check(Schema.isMinLength(1)),
      parameters: Schema.optional(
        Schema.Array(Schema.Union([Schema.String, Schema.Number, Schema.Null])),
      ),
      row_limit: Schema.optional(QueryRowLimit),
    }),
    success: QuerySuccess,
    execute: ({ sql, parameters, row_limit }) =>
      toolAction(() =>
        queries.query({
          sql,
          parameters: parameters ?? [],
          rowLimit: row_limit ?? 1_000,
        }),
      ),
  }),
  set_product: Tool.make({
    description: TOOL_DESCRIPTIONS.set_product,
    parameters: ProductDecisionSchema,
    success: MutationResult,
    execute: (decision) => mutation(() => gateway.setProduct(decision)),
  }),
  set_marketing: Tool.make({
    description: TOOL_DESCRIPTIONS.set_marketing,
    parameters: MarketingAllocationSchema,
    success: MutationResult,
    execute: (allocation) => mutation(() => gateway.setMarketing(allocation)),
  }),
  set_operations: Tool.make({
    description: TOOL_DESCRIPTIONS.set_operations,
    parameters: Schema.Struct({
      reliability_budget_cents: NonNegativeInt,
      support_budget_cents: NonNegativeInt,
    }),
    success: MutationResult,
    execute: (input) =>
      mutation(() =>
        gateway.draft.setOperations(
          input.reliability_budget_cents,
          input.support_budget_cents,
        ),
      ),
  }),
  start_research_project: Tool.make({
    description: TOOL_DESCRIPTIONS.start_research_project,
    parameters: Schema.Struct({
      tier: Schema.Int.check(Schema.isBetween({ minimum: 1, maximum: 3 })),
    }),
    success: MutationResult,
    execute: ({ tier }) =>
      mutation(() => gateway.draft.startResearchProject(tier)),
  }),
  expand_capacity: Tool.make({
    description: TOOL_DESCRIPTIONS.expand_capacity,
    parameters: Schema.Struct({ target_tier: NonNegativeInt }),
    success: MutationResult,
    execute: ({ target_tier }) =>
      mutation(() => gateway.expandCapacity(target_tier)),
  }),
  downsize_capacity: Tool.make({
    description: TOOL_DESCRIPTIONS.downsize_capacity,
    parameters: Schema.Struct({ target_tier: NonNegativeInt }),
    success: MutationResult,
    execute: ({ target_tier }) =>
      mutation(() => gateway.downsizeCapacity(target_tier)),
  }),
  cancel_capacity_project: Tool.make({
    description: TOOL_DESCRIPTIONS.cancel_capacity_project,
    parameters: Schema.Struct({ project_id: Schema.String }),
    success: MutationResult,
    execute: ({ project_id }) =>
      mutation(() => gateway.cancelCapacityProject(project_id)),
  }),
  submit_enterprise_bid: Tool.make({
    description: TOOL_DESCRIPTIONS.submit_enterprise_bid,
    parameters: EnterpriseBidSchema,
    success: MutationResult,
    execute: (bid) => mutation(() => gateway.submitEnterpriseBid(bid)),
  }),
  request_market_research: Tool.make({
    description: TOOL_DESCRIPTIONS.request_market_research,
    parameters: MarketResearchSchema,
    success: MutationResult,
    execute: (request) =>
      mutation(() => gateway.requestMarketResearch(request)),
  }),
  preview_decision: Tool.make({
    description: TOOL_DESCRIPTIONS.preview_decision,
    parameters: EmptyParameters,
    success: DecisionPreviewSchema,
    execute: () => toolAction(() => gateway.preview()),
  }),
  reset_decision: Tool.make({
    description: TOOL_DESCRIPTIONS.reset_decision,
    parameters: EmptyParameters,
    success: MutationResult,
    execute: () => mutation(() => gateway.draft.reset()),
  }),
  seal_decision: Tool.make({
    description: TOOL_DESCRIPTIONS.seal_decision,
    parameters: EmptyParameters,
    success: CompanyDecisionSchema,
    execute: () =>
      toolAction(async () => {
        await queries.validate(gateway.draft.preview());
        return gateway.seal();
      }),
  }),
});

export type AgentTools = ReturnType<typeof createTools>;
