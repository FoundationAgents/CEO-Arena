"""Small typed fixtures for Harness tests."""

from ceo_arena.models import (
    SCENARIO_VERSION,
    AgentBudget,
    BacklogOrder,
    CapacityTierConfig,
    CompanyObservation,
    CompanyState,
    DecisionWindowRequest,
    EnterpriseTender,
    OrderSource,
    ResearchLevelInfo,
)


def observation(company_id: str = "acme", world_version: int = 7) -> CompanyObservation:
    company = CompanyState(
        company_id=company_id,
        cash_cents=100_000_000,
        initial_cash_cents=100_000_000,
        initial_enterprise_value_cents=100_000_000,
        prices_cents={"basic": 1_000},
        promotions_cents={"basic": 50},
        reorder_targets={"basic": 20},
        inventory={"basic": 4},
        reserved_inventory={"basic": 1},
        backlog=[
            BacklogOrder(
                order_id="order-1",
                source=OrderSource.CONSUMER,
                product_id="basic",
                segment_id="value",
                quantity=6,
                unit_price_cents=950,
                created_day=3,
            )
        ],
        marketing_budget_cents={"value": {"search": 100}},
        goodwill={"value": 0.2},
        awareness={"value": 0.3},
        satisfaction={"value": 0.5},
        loyalty={"value": 0.1},
        development_budget_cents={"basic": 200},
        knowledge_capital_cents={"basic": 1_000.0},
        support_backlog=3,
    )
    return CompanyObservation(
        scenario_id=SCENARIO_VERSION,
        world_version=world_version,
        day=14,
        total_days=500,
        days_remaining=486,
        days_per_decision_window=7,
        company=company,
        public_prices_cents={company_id: {"basic": 1_000}, "rival": {"basic": 1_200}},
        public_promotions_cents={company_id: {"basic": 50}, "rival": {"basic": 100}},
        public_effective_prices_cents={company_id: {"basic": 950}, "rival": {"basic": 1_100}},
        open_tenders=(
            EnterpriseTender(
                tender_id="tender-1",
                product_id="basic",
                quantity=2,
                minimum_quality=0.1,
                reservation_price_cents=1_300,
                opened_day=10,
                deadline_day=17,
            ),
        ),
        published_macro_index=1.0,
        capacity_catalog=(
            CapacityTierConfig(
                tier=0,
                units_per_day=90,
                daily_cost_cents=40_000,
                expansion_cost_cents=0,
                expansion_lead_days=1,
            ),
        ),
        research_catalog=(
            ResearchLevelInfo(level=1, cost_cents=100_000, delay_days=2, signal_std=0.65),
        ),
    )


def request(company_id: str = "acme", world_version: int = 7) -> DecisionWindowRequest:
    return DecisionWindowRequest(
        run_id="run-1",
        company_id=company_id,
        world_version=world_version,
        observation=observation(company_id, world_version),
        budget=AgentBudget(timeout_seconds=5),
    )
