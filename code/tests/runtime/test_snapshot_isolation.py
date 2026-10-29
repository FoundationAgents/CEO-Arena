"""Fast typed snapshot clones and SQL projection retain full mutation isolation."""

from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.models import CompanyDecision, CompanyStatus
from ceo_arena.runtime.context import _issue_company_context


def test_detached_world_copy_preserves_hash_types_and_nested_isolation() -> None:
    world = MarketWorld(default_scenario(total_days=14))
    initial = world.create_initial_state(("a", "b"))
    initial_hash = world.state_hash(initial)
    result = world.advance_window(
        initial,
        {name: CompanyDecision(company_id=name, world_version=0) for name in initial.companies},
    )
    state = result.state
    copied = state.detached_copy()
    assert world.state_hash(initial) == initial_hash
    assert world.state_hash(copied) == world.state_hash(state)
    assert copied.companies["a"].status is CompanyStatus.ACTIVE
    assert all(isinstance(day, int) for day in copied.market_signals)
    assert copied.companies["a"].daily_history is not state.companies["a"].daily_history
    copied.companies["a"].daily_history[0].expense_breakdown.procurement += 1
    copied.companies["a"].inventory["A"] += 1
    assert world.state_hash(copied) != world.state_hash(state)


def test_synchronous_sql_snapshot_retains_no_observation_or_catalog_references() -> None:
    scenario = default_scenario()
    world = MarketWorld(scenario)
    state = world.create_initial_state(("a", "b"))
    observations = world.project_observations(state)
    own, other = observations["a"], observations["b"]
    cash, cost = own.company.cash_cents, own.product_catalog[0].unit_cost_cents
    capacity_cost = own.capacity_catalog[0].daily_cost_cents
    with _issue_company_context(own) as context:
        own.company.cash_cents = -123
        own.product_catalog[0].unit_cost_cents = 1
        own.capacity_catalog[0].daily_cost_cents = 1
        own.public_effective_prices_cents["b"]["A"] = 1
        assert context.query("SELECT cash_cents FROM company").rows == ((cash,),)
        assert context.query(
            "SELECT unit_cost_cents FROM product_catalog WHERE product_id='A'"
        ).rows == ((cost,),)
    assert scenario.products[0].unit_cost_cents == cost
    assert other.product_catalog[0].unit_cost_cents == cost
    assert scenario.capacity_tiers[0].daily_cost_cents == capacity_cost
    assert state.companies["a"].cash_cents == cash
    assert other.public_effective_prices_cents["b"]["A"] != 1
