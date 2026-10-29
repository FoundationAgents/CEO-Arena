from __future__ import annotations

from pathlib import Path

import pytest

from ceo_arena.config import default_scenario
from ceo_arena.dashboard.catalog import RunDashboard
from ceo_arena.economy import MarketWorld
from ceo_arena.models import (
    ActionReceipt,
    CapacityActionQuote,
    CompanyDailyRecord,
    CompanyDecision,
    CompanyObservation,
    ExpenseBreakdown,
    ExpenseCategory,
    MarketResearchJob,
)
from ceo_arena.runtime.context import QueryRejectedError, _issue_company_context


@pytest.fixture
def observation() -> CompanyObservation:
    world = MarketWorld(default_scenario(total_days=7))
    state = world.create_initial_state(("own", "other"))
    state.companies["other"].action_receipts.append(
        ActionReceipt(day=0, action="other-secret", status="applied", message="PRIVATE_OTHER")
    )
    observation = world.project_observations(state)["own"]
    company = observation.company
    record = CompanyDailyRecord(
        day=1,
        revenue_cents=1_000,
        refunds_cents=240,
        costs_cents=800,
        cogs_cents=600,
        ending_cash_cents=company.initial_cash_cents + 440,
        consumer_orders=1,
        fulfilled_units=1,
        abandoned_units=0,
        support_backlog=0,
        fulfillment_backlog_units=0,
        capacity_tier=0,
        expense_breakdown=ExpenseBreakdown(procurement=700, capacity_operations=100),
    )
    company.daily_history.append(record)
    company.cash_cents = record.ending_cash_cents
    company.cumulative_revenue_cents = record.revenue_cents
    company.cumulative_cost_cents = record.costs_cents
    company.cumulative_refunds_cents = record.refunds_cents
    company.action_receipts.append(
        ActionReceipt(
            day=0,
            action="cancel_capacity_project",
            status="cancelled",
            message="Construction cancelled; only unspent capital recovered.",
            refunded_cents=240,
            project_id="project-1",
            target_tier=1,
        )
    )
    company.research_jobs.append(
        MarketResearchJob(
            job_id="research-1",
            segment_id="mainstream",
            level=1,
            snapshot_day=0,
            completion_day=2,
            snapshot={"hidden_truth": 99.0},
        )
    )
    return observation.model_copy(
        update={
            "day": 1,
            "capacity_action_quotes": (
                CapacityActionQuote(
                    action="expand_capacity",
                    target_tier=1,
                    charged_cents=123,
                    effective_day=31,
                ),
            ),
        }
    )


def test_company_ledger_separates_refunds_procurement_and_cogs(
    observation: CompanyObservation,
) -> None:
    with _issue_company_context(observation) as context:
        assert context.query(
            "SELECT revenue_cents, refunds_cents, payments_cents, cogs_cents, "
            "gross_profit_cents, net_cashflow_cents, ending_cash_cents-opening_cash_cents "
            "FROM daily_cashflow"
        ).rows == ((1_000, 240, 800, 600, 400, 440, 440),)
        assert context.query("SELECT SUM(cents) FROM daily_expenses").rows == ((800,),)
        assert context.query("SELECT COUNT(*) FROM daily_expenses").rows == (
            (len(ExpenseCategory),),
        )
        assert context.query(
            "SELECT cents FROM daily_expenses WHERE category='procurement'"
        ).rows == ((700,),)
        assert context.query(
            "SELECT action, status, refunded_cents, project_id FROM action_receipts"
        ).rows == (("cancel_capacity_project", "cancelled", 240, "project-1"),)
        assert (
            "PRIVATE_OTHER" not in context.query("SELECT * FROM action_receipts").model_dump_json()
        )
        assert context.query(
            "SELECT charged_cents, effective_day FROM capacity_action_quotes"
        ).rows == ((123, 31),)
        assert context.query("SELECT COUNT(*) FROM capacity_policy").rows == ((1,),)
        assert context.query("SELECT day FROM market_signal").rows == (
            (observation.published_market_signal.day,),
        )
        with pytest.raises(QueryRejectedError):
            context.query("SELECT regime_id FROM market_signal")
        assert context.query("SELECT snapshot_day FROM research_jobs").rows == ((0,),)
        with pytest.raises(QueryRejectedError):
            context.query("SELECT snapshot FROM research_jobs")
        with pytest.raises(QueryRejectedError):
            context.query("DELETE FROM action_receipts")


def test_dashboard_ledger_reports_real_cash_not_refunds_as_sales(
    observation: CompanyObservation,
) -> None:
    rows = RunDashboard._cashflow_view(observation.company)
    assert len(rows) == 1
    assert rows[0].revenue_cents == 1_000
    assert rows[0].refunds_cents == 240
    assert rows[0].gross_profit_cents == 400
    assert rows[0].net_cashflow_cents == 440
    assert rows[0].expenses.procurement == 700


def test_dashboard_exposes_environment_receipts_without_private_memory(
    observation: CompanyObservation,
    tmp_path: Path,
) -> None:
    private = tmp_path / "agent-memory" / "owner" / "files"
    private.mkdir(parents=True)
    (private / "MEMORY.md").write_text("PRIVATE_REASONING", encoding="utf-8")
    view = RunDashboard(tmp_path)._company_view(
        "own",
        observation.company,
        "test-model",
        440,
        (),
        None,
    )
    assert view.refunds_cents == 240
    assert view.gross_profit_cents == 400
    assert view.revenue_cents == 1_000
    assert view.action_receipts[0].refunded_cents == 240
    assert "PRIVATE_REASONING" not in view.model_dump_json()


def test_dashboard_recent_ledger_keeps_correct_opening_balance(
    observation: CompanyObservation,
) -> None:
    company = observation.company
    template = company.daily_history[0]
    company.daily_history = [
        template.model_copy(
            update={"day": day, "ending_cash_cents": company.initial_cash_cents + day * 440}
        )
        for day in range(1, 36)
    ]
    rows = RunDashboard._cashflow_view(company)
    assert len(rows) == 30
    assert rows[0].day == 6
    assert rows[0].opening_cash_cents == company.initial_cash_cents + 5 * 440
    assert all(row.ending_cash_cents - row.opening_cash_cents == 440 for row in rows)


def test_dashboard_describes_cancellation_as_request_not_guaranteed_refund() -> None:
    decision = CompanyDecision(
        company_id="own",
        world_version=0,
        cancel_capacity_project_id="project-123",
    )
    actions = RunDashboard._decision_actions(decision)
    assert len(actions) == 1
    assert actions[0].category == "capacity"
    assert "See execution receipt" in actions[0].value
    assert "Cancel" in RunDashboard._decision_summary(decision)
