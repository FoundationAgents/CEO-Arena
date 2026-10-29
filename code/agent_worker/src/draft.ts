import type {
  CompanyDecision,
  DecisionPreview,
  DecisionRequest,
  EnterpriseBid,
  MarketResearchRequest,
  MarketingAllocation,
  ProductDecision,
} from "./protocol";

export class DraftError extends Error {}

export class DecisionDraft {
  readonly companyID: string;
  readonly worldVersion: number;
  private readonly products = new Map<string, ProductDecision>();
  private readonly marketing = new Map<string, MarketingAllocation>();
  private readonly enterpriseBids = new Map<string, EnterpriseBid>();
  private readonly marketResearch = new Map<string, MarketResearchRequest>();
  private reliabilityBudget?: number;
  private supportBudget?: number;
  private researchProjectTier?: number;
  private capacityTargetTier?: number;
  private capacityDownsizeTier?: number;
  private cancelCapacityProjectID?: string;
  private decision?: CompanyDecision;

  constructor(companyID: string, worldVersion: number) {
    this.companyID = companyID;
    this.worldVersion = worldVersion;
  }

  get sealed() {
    return this.decision !== undefined;
  }

  reset() {
    this.assertOpen();
    this.products.clear();
    this.marketing.clear();
    this.enterpriseBids.clear();
    this.marketResearch.clear();
    this.reliabilityBudget = undefined;
    this.supportBudget = undefined;
    this.researchProjectTier = undefined;
    this.clearCapacity();
  }

  product(productID: string) {
    return this.products.get(productID);
  }

  setProduct(next: ProductDecision) {
    this.assertOpen();
    this.products.set(next.product_id, {
      ...this.products.get(next.product_id),
      ...next,
    });
  }

  setMarketing(next: MarketingAllocation) {
    this.assertOpen();
    this.marketing.set(`${next.segment_id}\u0000${next.channel}`, next);
  }

  setOperations(reliabilityBudget: number, supportBudget: number) {
    this.assertOpen();
    nonNegative(reliabilityBudget, "reliability budget");
    nonNegative(supportBudget, "support budget");
    this.reliabilityBudget = reliabilityBudget;
    this.supportBudget = supportBudget;
  }

  startResearchProject(tier: number) {
    this.assertOpen();
    if (![1, 2, 3].includes(tier))
      throw new DraftError("research project tier must be 1, 2, or 3");
    this.researchProjectTier = tier;
  }

  expandCapacity(targetTier: number) {
    this.assertOpen();
    nonNegative(targetTier, "capacity target tier");
    this.clearCapacity();
    this.capacityTargetTier = targetTier;
  }

  downsizeCapacity(targetTier: number) {
    this.assertOpen();
    nonNegative(targetTier, "capacity target tier");
    this.clearCapacity();
    this.capacityDownsizeTier = targetTier;
  }

  cancelCapacityProject(projectID: string) {
    this.assertOpen();
    if (!projectID.trim())
      throw new DraftError("capacity project id must not be blank");
    this.clearCapacity();
    this.cancelCapacityProjectID = projectID;
  }

  private clearCapacity() {
    this.capacityTargetTier = undefined;
    this.capacityDownsizeTier = undefined;
    this.cancelCapacityProjectID = undefined;
  }

  submitEnterpriseBid(bid: EnterpriseBid) {
    this.assertOpen();
    this.enterpriseBids.set(bid.tender_id, bid);
  }

  requestMarketResearch(request: MarketResearchRequest) {
    this.assertOpen();
    this.marketResearch.set(request.segment_id, request);
  }

  seal(): CompanyDecision {
    this.decision ??= this.preview();
    return this.decision;
  }

  restore(decision: CompanyDecision): void {
    if (
      decision.company_id !== this.companyID ||
      decision.world_version !== this.worldVersion
    )
      throw new DraftError(
        "saved draft belongs to a different decision window",
      );
    this.reset();
    for (const product of decision.products) this.setProduct(product);
    for (const allocation of decision.marketing) this.setMarketing(allocation);
    for (const bid of decision.enterprise_bids) this.submitEnterpriseBid(bid);
    for (const research of decision.market_research)
      this.requestMarketResearch(research);
    this.reliabilityBudget = decision.reliability_budget_cents;
    this.supportBudget = decision.support_budget_cents;
    this.researchProjectTier = decision.research_project_tier;
    this.capacityTargetTier = decision.capacity_target_tier;
    this.capacityDownsizeTier = decision.capacity_downsize_tier;
    this.cancelCapacityProjectID = decision.cancel_capacity_project_id;
  }

  preview(): CompanyDecision {
    return structuredClone(
      this.decision ?? {
        company_id: this.companyID,
        world_version: this.worldVersion,
        products: sorted(this.products),
        marketing: sorted(this.marketing),
        reliability_budget_cents: this.reliabilityBudget,
        support_budget_cents: this.supportBudget,
        research_project_tier: this.researchProjectTier,
        capacity_target_tier: this.capacityTargetTier,
        capacity_downsize_tier: this.capacityDownsizeTier,
        cancel_capacity_project_id: this.cancelCapacityProjectID,
        enterprise_bids: sorted(this.enterpriseBids),
        market_research: sorted(this.marketResearch),
      },
    );
  }

  private assertOpen() {
    if (this.sealed) throw new DraftError("decision is already sealed");
  }
}

export class AgentToolGateway {
  readonly draft: DecisionDraft;
  private readonly request: DecisionRequest["request"];
  private readonly marketingCells: ReadonlySet<string>;
  private readonly productIDs: ReadonlySet<string>;
  private readonly segmentIDs: ReadonlySet<string>;
  private readonly tenderIDs: ReadonlySet<string>;

  constructor(request: DecisionRequest["request"]) {
    if (request.company_id !== request.observation.company.company_id)
      throw new DraftError("request company does not match observation");
    if (request.world_version !== request.observation.world_version)
      throw new DraftError("request world version does not match observation");
    this.request = request;
    this.productIDs = new Set(
      Object.keys(request.observation.company.prices_cents),
    );
    this.marketingCells = new Set(
      Object.entries(
        request.observation.company.marketing_budget_cents,
      ).flatMap(([segment, channels]) =>
        Object.keys(channels).map((channel) => `${segment}\u0000${channel}`),
      ),
    );
    this.segmentIDs = new Set([
      ...Object.keys(request.observation.company.goodwill),
      ...Object.keys(request.observation.company.satisfaction),
      ...Object.keys(request.observation.company.marketing_budget_cents),
    ]);
    this.tenderIDs = new Set(
      request.observation.open_tenders
        .filter((tender) => !tender.settled)
        .map((tender) => tender.tender_id),
    );
    this.draft = new DecisionDraft(request.company_id, request.world_version);
  }

  setProduct(next: ProductDecision) {
    if (!this.productIDs.has(next.product_id))
      throw new DraftError(`unknown product: ${next.product_id}`);
    const current =
      this.request.observation.company.prices_cents[next.product_id];
    const price =
      next.price_cents ??
      this.draft.product(next.product_id)?.price_cents ??
      current;
    const promotion =
      next.promotion_cents ??
      this.draft.product(next.product_id)?.promotion_cents ??
      this.request.observation.company.promotions_cents[next.product_id];
    if (promotion > price)
      throw new DraftError("promotion cannot exceed price");
    this.draft.setProduct(next);
  }

  setMarketing(next: MarketingAllocation) {
    if (!this.marketingCells.has(`${next.segment_id}\u0000${next.channel}`))
      throw new DraftError("unknown segment-channel marketing cell");
    this.draft.setMarketing(next);
  }

  submitEnterpriseBid(next: EnterpriseBid) {
    if (!this.tenderIDs.has(next.tender_id))
      throw new DraftError(`unknown open tender: ${next.tender_id}`);
    this.draft.submitEnterpriseBid(next);
  }

  requestMarketResearch(next: MarketResearchRequest) {
    if (!this.segmentIDs.has(next.segment_id))
      throw new DraftError(`unknown market segment: ${next.segment_id}`);
    this.draft.requestMarketResearch(next);
  }

  expandCapacity(targetTier: number) {
    this.capacityQuote("expand_capacity", targetTier);
    this.draft.expandCapacity(targetTier);
  }

  downsizeCapacity(targetTier: number) {
    this.capacityQuote("downsize_capacity", targetTier);
    this.draft.downsizeCapacity(targetTier);
  }

  cancelCapacityProject(projectID: string) {
    this.capacityQuote("cancel_capacity_project", undefined, projectID);
    this.draft.cancelCapacityProject(projectID);
  }

  preview(): DecisionPreview {
    const observation = this.request.observation;
    const company = observation.company;
    const decision = this.draft.preview();
    let charges = 0;
    let refunds = 0;
    const capacityActions = [
      ["expand_capacity", decision.capacity_target_tier, undefined],
      ["downsize_capacity", decision.capacity_downsize_tier, undefined],
      [
        "cancel_capacity_project",
        undefined,
        decision.cancel_capacity_project_id,
      ],
    ] as const;
    for (const [action, tier, projectID] of capacityActions) {
      if (tier === undefined && projectID === undefined) continue;
      const quote = this.capacityQuote(action, tier, projectID);
      charges += quote.charged_cents;
      refunds += quote.refunded_cents;
    }
    if (decision.research_project_tier !== undefined) {
      const tier = observation.innovation_catalog.find(
        (item) => item.tier === decision.research_project_tier,
      );
      if (!tier)
        throw new DraftError(
          "unknown innovation tier; inspect innovation_catalog",
        );
      charges += tier.cost_cents;
    }
    for (const request of decision.market_research) {
      const level = observation.research_catalog.find(
        (item) => item.level === request.level,
      );
      if (!level)
        throw new DraftError(
          "unknown research level; inspect research_catalog",
        );
      charges += level.cost_cents;
    }
    const development = { ...company.development_budget_cents };
    for (const product of decision.products)
      if (product.development_budget_cents !== undefined)
        development[product.product_id] = product.development_budget_cents;
    const marketing = new Map(
      Object.entries(company.marketing_budget_cents).flatMap(
        ([segment, channels]) =>
          Object.entries(channels).map(
            ([channel, amount]) =>
              [`${segment}\u0000${channel}`, amount] as const,
          ),
      ),
    );
    for (const cell of decision.marketing)
      marketing.set(
        `${cell.segment_id}\u0000${cell.channel}`,
        cell.daily_budget_cents,
      );
    const capacity = observation.capacity_catalog.find(
      (item) => item.tier === company.capacity_tier,
    );
    if (!capacity)
      throw new DraftError(
        "active capacity tier missing from capacity_catalog",
      );
    const daily =
      sum(Object.values(development)) +
      sum([...marketing.values()]) +
      (decision.reliability_budget_cents ?? company.reliability_budget_cents) +
      (decision.support_budget_cents ?? company.support_budget_cents) +
      capacity.daily_cost_cents;
    const cash = company.cash_cents + refunds - charges;
    return {
      upfront_charges_cents: charges,
      upfront_refunds_cents: refunds,
      cash_after_upfront_cents: cash,
      daily_policy_cost_cents: daily,
      affordable: cash >= 0,
      note: "Draft estimate, not execution. Daily cost uses currently installed capacity; excludes procurement, holding, failures and future capacity changes. No future revenue is assumed. Inspect actual action_receipts after settlement.",
    };
  }

  seal(): CompanyDecision {
    if (!this.preview().affordable)
      throw new DraftError(
        "combined upfront commitments exceed available cash; revise the draft before sealing",
      );
    return this.draft.seal();
  }

  private capacityQuote(
    action: string,
    targetTier?: number,
    projectID?: string,
  ) {
    const quote = this.request.observation.capacity_action_quotes.find(
      (item) =>
        item.action === action &&
        (projectID !== undefined
          ? item.project_id === projectID
          : item.target_tier === targetTier),
    );
    if (!quote)
      throw new DraftError(
        "capacity action is not currently available; inspect capacity_action_quotes",
      );
    return quote;
  }
}

const sum = (values: readonly number[]) =>
  values.reduce((total, value) => total + value, 0);

const sorted = <Value>(values: ReadonlyMap<string, Value>): Value[] =>
  [...values.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([, value]) => value);

const nonNegative = (value: number, label: string) => {
  if (!Number.isInteger(value) || value < 0)
    throw new DraftError(`${label} must be a non-negative integer`);
};
