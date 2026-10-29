"""Cross-module contract boundaries: last-day settlement, funds and exact recovery."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from ceo_arena.economy.world import MarketWorld
from ceo_arena.harness.draft import AgentToolGateway
from ceo_arena.models import (
    BacklogOrder,
    CompanyDecision,
    EnterpriseBid,
    EnterpriseTender,
    MarketDynamicsConfig,
    MarketPhaseConfig,
    MarketResearchRequest,
    OrderSource,
    ProductDecision,
    WorldState,
)

from .conftest import build_scenario


def _world(
    *, days: int = 4, capacity: int = 5, inventory: int = 30
) -> tuple[MarketWorld, WorldState]:
    scenario = build_scenario(demand=1e-9, capacity=capacity, inventory=inventory).model_copy(
        update={"total_days": days, "tender_interval_days": 100}
    )
    world = MarketWorld(scenario)
    state = world.create_initial_state(("a",))
    state.tenders.clear()
    return world, state


def _step(
    world: MarketWorld, state: WorldState, decisions: Mapping[str, CompanyDecision] | None = None
) -> WorldState:
    return world.advance_window(
        state,
        decisions
        or {
            company_id: CompanyDecision(company_id=company_id, world_version=state.version)
            for company_id in state.companies
        },
    ).state


def _order(quantity: int, due: int, *, order_id: str = "old-order") -> BacklogOrder:
    return BacklogOrder(
        order_id=order_id,
        source=OrderSource.ENTERPRISE,
        product_id="standard",
        quantity=quantity,
        unit_price_cents=1000,
        created_day=0,
        delivery_due_day=due,
    )


def _tender(tender_id: str, quantity: int, due: int = 3) -> EnterpriseTender:
    return EnterpriseTender(
        tender_id=tender_id,
        product_id="standard",
        quantity=quantity,
        minimum_quality=0,
        reservation_price_cents=1000,
        opened_day=0,
        deadline_day=1,
        delivery_due_day=due,
    )


@pytest.mark.parametrize("quantity,expected_penalty", [(5, 0), (7, 200)])
def test_due_day_allows_delivery_before_expiring_only_remaining_units(
    quantity: int, expected_penalty: int
) -> None:
    world, state = _world(days=2)
    company = state.companies["a"]
    company.backlog = [_order(quantity, due=1)]
    company.reserved_inventory["standard"] = quantity
    settled = _step(world, state)
    outcome = settled.companies["a"].daily_history[-1]
    assert outcome.fulfilled_units == 5
    assert outcome.expense_breakdown.contract_penalties == expected_penalty
    assert outcome.abandoned_units == quantity - 5
    assert settled.companies["a"].backlog == []
    following = _step(world, settled)
    assert following.companies["a"].daily_history[-1].expense_breakdown.contract_penalties == 0


def test_terminal_day_unfulfilled_contract_is_penalized_without_day_501() -> None:
    world, state = _world(days=3)
    state.companies["a"].backlog = [_order(20, due=3)]
    state.companies["a"].reserved_inventory["standard"] = 20
    for _ in range(3):
        state = _step(world, state)
    company = state.companies["a"]
    assert company.cumulative_fulfilled_units == 15
    assert company.backlog == []
    assert sum(day.expense_breakdown.contract_penalties for day in company.daily_history) == 500
    defaults = [item for item in company.action_receipts if item.action == "enterprise_delivery"]
    assert len(defaults) == 1
    assert defaults[0].day == 3
    assert defaults[0].charged_cents == 500


def test_tender_qualification_uses_multiple_future_delivery_days() -> None:
    world, state = _world()
    state.tenders = [_tender("new", quantity=10)]
    decision = CompanyDecision(
        company_id="a",
        world_version=0,
        enterprise_bids=(EnterpriseBid(tender_id="new", unit_price_cents=900),),
    )
    state = _step(world, state, {"a": decision})
    assert state.tenders[0].winner_company_id == "a"
    state = _step(world, state)
    state = _step(world, state)
    assert state.companies["a"].cumulative_fulfilled_units == 10
    assert state.companies["a"].backlog == []


def test_existing_orders_are_deducted_from_contract_delivery_capacity() -> None:
    world, state = _world()
    state.companies["a"].backlog = [_order(12, due=4)]
    state.companies["a"].reserved_inventory["standard"] = 12
    state.tenders = [_tender("new", quantity=6)]
    state = _step(
        world,
        state,
        {
            "a": CompanyDecision(
                company_id="a",
                world_version=0,
                enterprise_bids=(EnterpriseBid(tender_id="new", unit_price_cents=900),),
            )
        },
    )
    assert state.tenders[0].winner_company_id is None


def test_same_day_awards_cannot_double_reserve_the_same_inventory() -> None:
    world, state = _world(capacity=100, inventory=10)
    state.tenders = [_tender("first", quantity=8), _tender("second", quantity=8)]
    state = _step(
        world,
        state,
        {
            "a": CompanyDecision(
                company_id="a",
                world_version=0,
                enterprise_bids=tuple(
                    EnterpriseBid(tender_id=item.tender_id, unit_price_cents=900)
                    for item in state.tenders
                ),
            )
        },
    )
    assert sum(tender.winner_company_id == "a" for tender in state.tenders) == 1
    assert state.companies["a"].reserved_inventory["standard"] == 8


@pytest.mark.parametrize("expanding,expected_winner", [(True, "a"), (False, None)])
def test_qualification_accounts_for_committed_capacity_changes(
    expanding: bool, expected_winner: str | None
) -> None:
    base, _ = _world()
    scenario = base.config.model_copy(
        update={
            "capacity_policy": base.config.capacity_policy.model_copy(
                update={
                    "downsize_lead_days": 2,
                }
            ),
        }
    )
    world = MarketWorld(scenario)
    state = world.create_initial_state(("a",))
    state.companies["a"].capacity_tier = 0 if expanding else 1
    state.tenders = [_tender("future", quantity=16)]
    decision = CompanyDecision(
        company_id="a",
        world_version=0,
        capacity_target_tier=1 if expanding else None,
        capacity_downsize_tier=None if expanding else 0,
        enterprise_bids=(EnterpriseBid(tender_id="future", unit_price_cents=900),),
    )
    state = _step(world, state, {"a": decision})
    assert state.tenders[0].winner_company_id == expected_winner


def test_cancel_refund_funds_innovation_without_becoming_sales_revenue() -> None:
    world, state = _world(inventory=120)
    state.companies["a"].cash_cents = 22_000
    state = _step(
        world,
        state,
        {
            "a": CompanyDecision(
                company_id="a",
                world_version=0,
                capacity_target_tier=1,
            )
        },
    )
    company = state.companies["a"]
    project_id = company.capacity_projects[0].project_id
    gateway = AgentToolGateway(world.project_observations(state)["a"])
    gateway.cancel_capacity_project(project_id)
    gateway.draft.start_research_project(1)
    preview = gateway.preview()
    assert preview.upfront_charges_cents > company.cash_cents
    assert preview.affordable
    decision = gateway.seal()
    settled = _step(world, state, {"a": decision})
    company = settled.companies["a"]
    day = company.daily_history[-1]
    assert day.revenue_cents == 0
    assert company.cumulative_revenue_cents == 0
    assert day.refunds_cents == preview.upfront_refunds_cents
    assert company.cumulative_refunds_cents == preview.upfront_refunds_cents
    assert day.costs_cents == day.expense_breakdown.total
    assert (
        company.cash_cents == state.companies["a"].cash_cents + day.refunds_cents - day.costs_cents
    )
    assert day.expense_breakdown.innovation == preview.upfront_charges_cents


def test_failed_multi_action_window_is_atomic_and_keeps_market_state_unchanged() -> None:
    world, state = _world()
    state.companies["a"].cash_cents = 100
    before = world.state_hash(state)
    decision = CompanyDecision(
        company_id="a",
        world_version=0,
        products=(ProductDecision(product_id="standard", price_cents=2000),),
        research_project_tier=3,
    )
    with pytest.raises(ValueError, match="exceed available cash"):
        world.advance_window(state, {"a": decision})
    assert world.state_hash(state) == before
    assert state.market.day == 0


def test_phase_and_pending_research_recover_exactly_without_leaking_snapshots() -> None:
    base, _ = _world(days=6)
    scenario = base.config.model_copy(
        update={
            "market_dynamics": MarketDynamicsConfig(
                phases=(
                    MarketPhaseConfig(phase_id="low"),
                    MarketPhaseConfig(
                        phase_id="high", demand_multiplier=1.5, quality_expectation_shift=0.1
                    ),
                ),
                transitions=((0.0, 1.0), (1.0, 0.0)),
                publication_delay_days=1,
            ),
            "research": base.config.research.model_copy(
                update={
                    "level_delay_days": {level: 3 for level in range(1, 6)},
                }
            ),
        }
    )
    world = MarketWorld(scenario)
    state = world.create_initial_state(("a",))
    state.tenders.clear()
    state = _step(
        world,
        state,
        {
            "a": CompanyDecision(
                company_id="a",
                world_version=0,
                market_research=(MarketResearchRequest(segment_id="mainstream", level=1),),
            )
        },
    )
    assert state.companies["a"].research_jobs[0].snapshot
    visible = world.project_observations(state)["a"]
    assert visible.company.research_jobs[0].snapshot == {}
    assert visible.published_market_signal.day == 0
    assert visible.published_market_signal.demand_index == 1.0
    restored = WorldState.model_validate_json(state.model_dump_json())
    recovered_world = MarketWorld(scenario)
    for _ in range(4):
        state = _step(world, state)
        restored = _step(recovered_world, restored)
        assert world.state_hash(state) == recovered_world.state_hash(restored)
        assert state.market.day == state.day
    report = state.companies["a"].research_reports[0]
    assert report.snapshot_day == 0
    assert report.completed_day == 3
