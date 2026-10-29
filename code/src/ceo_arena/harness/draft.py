"""Company-bound staging area exposed to agent tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from ceo_arena.models import (
    CapacityActionQuote,
    CompanyDecision,
    CompanyObservation,
    EnterpriseBid,
    MarketingAllocation,
    MarketResearchRequest,
    ProductDecision,
)


class DecisionDraftError(ValueError):
    """Raised when an agent attempts an invalid draft mutation."""


class DecisionSealedError(DecisionDraftError):
    """Raised when an agent mutates an already sealed decision."""


@dataclass(frozen=True, slots=True)
class DecisionBudgetPreview:
    """Observable commitments, not an economic forecast or execution receipt."""

    upfront_charges_cents: int
    upfront_refunds_cents: int
    cash_after_upfront_cents: int
    daily_policy_cost_cents: int

    @property
    def affordable(self) -> bool:
        return self.cash_after_upfront_cents >= 0


@dataclass(slots=True)
class DecisionDraft:
    """Accumulate tool writes before producing one immutable decision."""

    company_id: str
    world_version: int
    _products: dict[str, ProductDecision] = field(default_factory=dict, init=False)
    _marketing: dict[tuple[str, str], MarketingAllocation] = field(default_factory=dict, init=False)
    _enterprise_bids: dict[str, EnterpriseBid] = field(default_factory=dict, init=False)
    _market_research: dict[str, MarketResearchRequest] = field(default_factory=dict, init=False)
    _reliability_budget_cents: int | None = field(default=None, init=False)
    _support_budget_cents: int | None = field(default=None, init=False)
    _research_project_tier: int | None = field(default=None, init=False)
    _capacity_target_tier: int | None = field(default=None, init=False)
    _capacity_downsize_tier: int | None = field(default=None, init=False)
    _cancel_capacity_project_id: str | None = field(default=None, init=False)
    _decision: CompanyDecision | None = field(default=None, init=False)

    @property
    def sealed(self) -> bool:
        return self._decision is not None

    def reset(self) -> None:
        """Discard unsealed changes without touching the world or decision budget."""

        self._ensure_open()
        self._products.clear()
        self._marketing.clear()
        self._enterprise_bids.clear()
        self._market_research.clear()
        self._reliability_budget_cents = None
        self._support_budget_cents = None
        self._research_project_tier = None
        self._clear_capacity()

    def product(self, product_id: str) -> ProductDecision | None:
        """Return the current staged product patch without sealing."""

        return self._products.get(product_id)

    def set_product(self, decision: ProductDecision) -> None:
        """Merge one product patch, replacing only supplied fields."""

        self._ensure_open()
        current = self._products.get(
            decision.product_id, ProductDecision(product_id=decision.product_id)
        )
        update = {
            name: value
            for name in (
                "price_cents",
                "promotion_cents",
                "reorder_target",
                "development_budget_cents",
            )
            if (value := getattr(decision, name)) is not None
        }
        self._products[decision.product_id] = current.model_copy(update=update)

    def set_marketing(self, allocation: MarketingAllocation) -> None:
        """Set one segment-channel budget with last-write-wins semantics."""

        self._ensure_open()
        self._marketing[(allocation.segment_id, allocation.channel)] = allocation

    def set_operations(self, reliability_budget_cents: int, support_budget_cents: int) -> None:
        """Stage daily reliability and customer-support budgets."""

        self._ensure_open()
        if reliability_budget_cents < 0 or support_budget_cents < 0:
            raise DecisionDraftError("operations budgets must be non-negative")
        self._reliability_budget_cents = reliability_budget_cents
        self._support_budget_cents = support_budget_cents

    def start_research_project(self, tier: int) -> None:
        """Stage one product-quality research project."""

        self._ensure_open()
        if tier not in {1, 2, 3}:
            raise DecisionDraftError("research project tier must be 1, 2, or 3")
        self._research_project_tier = tier

    def expand_capacity(self, target_tier: int) -> None:
        """Stage a non-negative capacity tier target."""

        self._ensure_open()
        if target_tier < 0:
            raise DecisionDraftError("capacity target tier must be non-negative")
        self._clear_capacity()
        self._capacity_target_tier = target_tier

    def downsize_capacity(self, target_tier: int) -> None:
        """Replace the capacity draft with one installed-capacity reduction."""

        self._ensure_open()
        if target_tier < 0:
            raise DecisionDraftError("capacity target tier must be non-negative")
        self._clear_capacity()
        self._capacity_downsize_tier = target_tier

    def cancel_capacity_project(self, project_id: str) -> None:
        """Replace the capacity draft with one unfinished-project cancellation."""

        self._ensure_open()
        if not project_id.strip():
            raise DecisionDraftError("capacity project id must not be blank")
        self._clear_capacity()
        self._cancel_capacity_project_id = project_id

    def _clear_capacity(self) -> None:
        self._capacity_target_tier = None
        self._capacity_downsize_tier = None
        self._cancel_capacity_project_id = None

    def submit_enterprise_bid(self, bid: EnterpriseBid) -> None:
        """Stage one bid per tender with last-write-wins semantics."""

        self._ensure_open()
        self._enterprise_bids[bid.tender_id] = bid

    def request_market_research(self, request: MarketResearchRequest) -> None:
        """Stage one research request per segment."""

        self._ensure_open()
        self._market_research[request.segment_id] = request

    def preview(self) -> CompanyDecision:
        """Build the current decision without freezing the draft."""

        return CompanyDecision(
            company_id=self.company_id,
            world_version=self.world_version,
            products=tuple(self._products[key] for key in sorted(self._products)),
            marketing=tuple(self._marketing[key] for key in sorted(self._marketing)),
            reliability_budget_cents=self._reliability_budget_cents,
            support_budget_cents=self._support_budget_cents,
            research_project_tier=self._research_project_tier,
            capacity_target_tier=self._capacity_target_tier,
            capacity_downsize_tier=self._capacity_downsize_tier,
            cancel_capacity_project_id=self._cancel_capacity_project_id,
            enterprise_bids=tuple(
                self._enterprise_bids[key] for key in sorted(self._enterprise_bids)
            ),
            market_research=tuple(
                self._market_research[key] for key in sorted(self._market_research)
            ),
        )

    def seal(self) -> CompanyDecision:
        """Return the bound immutable decision; repeated seals are idempotent."""

        if self._decision is None:
            self._decision = self.preview()
        return self._decision

    def _ensure_open(self) -> None:
        if self.sealed:
            raise DecisionSealedError("decision is already sealed")


class AgentToolGateway:
    """Validate tool writes against one company-scoped observation."""

    def __init__(self, observation: CompanyObservation) -> None:
        if observation.company.company_id not in observation.public_prices_cents:
            raise DecisionDraftError("public price table does not contain the observed company")
        self._observation = observation
        self._product_ids = frozenset(observation.company.prices_cents)
        self._marketing_cells = frozenset(
            (segment_id, channel)
            for segment_id, channels in observation.company.marketing_budget_cents.items()
            for channel in channels
        )
        self._segment_ids = frozenset(
            set(observation.company.goodwill)
            | set(observation.company.satisfaction)
            | set(observation.company.marketing_budget_cents)
        )
        self._tender_ids = frozenset(
            tender.tender_id for tender in observation.open_tenders if not tender.settled
        )
        self._draft = DecisionDraft(observation.company.company_id, observation.world_version)

    @property
    def draft(self) -> DecisionDraft:
        return self._draft

    def set_product(self, decision: ProductDecision) -> None:
        """Validate a product patch before staging it."""

        if decision.product_id not in self._product_ids:
            raise DecisionDraftError(f"unknown product: {decision.product_id}")
        staged = self._draft.product(decision.product_id)
        price = (
            decision.price_cents
            or (staged.price_cents if staged else None)
            or self._observation.company.prices_cents[decision.product_id]
        )
        promotion = (
            decision.promotion_cents
            if decision.promotion_cents is not None
            else (
                staged.promotion_cents
                if staged and staged.promotion_cents is not None
                else self._observation.company.promotions_cents[decision.product_id]
            )
        )
        if promotion > price:
            raise DecisionDraftError("promotion cannot exceed effective staged price")
        self._draft.set_product(decision)

    def set_marketing(self, allocation: MarketingAllocation) -> None:
        """Validate a visible segment-channel pair before staging it."""

        if (allocation.segment_id, allocation.channel) not in self._marketing_cells:
            raise DecisionDraftError("unknown segment-channel marketing cell")
        self._draft.set_marketing(allocation)

    def submit_enterprise_bid(self, bid: EnterpriseBid) -> None:
        """Validate that a bid targets a currently open tender."""

        if bid.tender_id not in self._tender_ids:
            raise DecisionDraftError(f"unknown open tender: {bid.tender_id}")
        self._draft.submit_enterprise_bid(bid)

    def request_market_research(self, request: MarketResearchRequest) -> None:
        """Validate a visible segment before staging market research."""

        if request.segment_id not in self._segment_ids:
            raise DecisionDraftError(f"unknown market segment: {request.segment_id}")
        self._draft.request_market_research(request)

    def expand_capacity(self, target_tier: int) -> None:
        """Stage only a legal expansion quoted by the authoritative environment."""

        self._capacity_quote("expand_capacity", target_tier=target_tier)
        self._draft.expand_capacity(target_tier)

    def downsize_capacity(self, target_tier: int) -> None:
        """Stage only a legal delayed reduction quoted by the environment."""

        self._capacity_quote("downsize_capacity", target_tier=target_tier)
        self._draft.downsize_capacity(target_tier)

    def cancel_capacity_project(self, project_id: str) -> None:
        """Stage only a cancellable unfinished project quoted by the environment."""

        self._capacity_quote("cancel_capacity_project", project_id=project_id)
        self._draft.cancel_capacity_project(project_id)

    def _capacity_quote(
        self, action: str, *, target_tier: int | None = None, project_id: str | None = None
    ) -> CapacityActionQuote:
        for quote in self._observation.capacity_action_quotes:
            if quote.action == action and (
                quote.project_id == project_id
                if project_id is not None
                else quote.target_tier == target_tier
            ):
                return quote
        raise DecisionDraftError(
            "capacity action is not currently available; inspect capacity_action_quotes"
        )

    def preview(self) -> DecisionBudgetPreview:
        """Quote upfront commitments and current daily policies without sealing."""

        decision = self._draft.preview()
        charges = refunds = 0
        capacity_actions = (
            ("expand_capacity", decision.capacity_target_tier, None),
            ("downsize_capacity", decision.capacity_downsize_tier, None),
            ("cancel_capacity_project", None, decision.cancel_capacity_project_id),
        )
        for action, tier, project_id in capacity_actions:
            if tier is not None or project_id is not None:
                quote = self._capacity_quote(action, target_tier=tier, project_id=project_id)
                charges += quote.charged_cents
                refunds += quote.refunded_cents
        innovation = {item.tier: item.cost_cents for item in self._observation.innovation_catalog}
        if decision.research_project_tier is not None:
            if decision.research_project_tier not in innovation:
                raise DecisionDraftError("unknown innovation tier; inspect innovation_catalog")
            charges += innovation[decision.research_project_tier]
        research = {item.level: item.cost_cents for item in self._observation.research_catalog}
        for request in decision.market_research:
            if request.level not in research:
                raise DecisionDraftError("unknown research level; inspect research_catalog")
            charges += research[request.level]
        company = self._observation.company
        development = dict(company.development_budget_cents)
        development.update(
            (item.product_id, item.development_budget_cents)
            for item in decision.products
            if item.development_budget_cents is not None
        )
        marketing = {
            (segment, channel): value
            for segment, channels in company.marketing_budget_cents.items()
            for channel, value in channels.items()
        }
        marketing.update(
            ((item.segment_id, item.channel), item.daily_budget_cents)
            for item in decision.marketing
        )
        active_capacity = next(
            (
                item
                for item in self._observation.capacity_catalog
                if item.tier == company.capacity_tier
            ),
            None,
        )
        if active_capacity is None:
            raise DecisionDraftError("active capacity tier missing from capacity_catalog")
        daily = (
            sum(development.values()) + sum(marketing.values()) + active_capacity.daily_cost_cents
        )
        for staged, current in (
            (decision.reliability_budget_cents, company.reliability_budget_cents),
            (decision.support_budget_cents, company.support_budget_cents),
        ):
            daily += current if staged is None else staged
        return DecisionBudgetPreview(
            charges, refunds, company.cash_cents + refunds - charges, daily
        )

    def seal(self) -> CompanyDecision:
        """Seal a decision whose identity is fixed by the connection context."""

        if not self.preview().affordable:
            raise DecisionDraftError(
                "combined upfront commitments exceed available cash; "
                "revise the draft before sealing"
            )
        return self._draft.seal()
