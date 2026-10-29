"""Capacity lifecycle and refund invariants through its public interface."""

from __future__ import annotations

import pytest

from ceo_arena.economy.capacity import CapacityManager
from ceo_arena.models import (
    CapacityChangeKind,
    CapacityPolicyConfig,
    CapacityTierConfig,
    CompanyDecision,
    CompanyState,
    CompanyStatus,
    JobStatus,
    ScenarioConfig,
)


@pytest.fixture
def capacity_scenario(scenario: ScenarioConfig) -> ScenarioConfig:
    return scenario.model_copy(
        update={
            "capacity_tiers": tuple(
                CapacityTierConfig(
                    tier=tier,
                    units_per_day=units,
                    daily_cost_cents=daily,
                    expansion_cost_cents=capital,
                    expansion_lead_days=days,
                )
                for tier, units, daily, capital, days in (
                    (0, 90, 100, 0, 1),
                    (1, 120, 130, 20_000, 10),
                    (2, 160, 170, 60_000, 20),
                )
            ),
            "capacity_policy": CapacityPolicyConfig(
                startup_cost_bps=1000,
                cancellation_fee_bps=500,
                downsize_fee_per_tier_cents=2_000,
                downsize_lead_days=3,
            ),
        }
    )


@pytest.fixture
def manager(capacity_scenario: ScenarioConfig) -> CapacityManager:
    return CapacityManager(capacity_scenario)


@pytest.fixture
def company() -> CompanyState:
    return CompanyState(
        company_id="alpha",
        cash_cents=100_000,
        initial_cash_cents=100_000,
        initial_enterprise_value_cents=100_000,
        prices_cents={"standard": 1_000},
        promotions_cents={"standard": 0},
        reorder_targets={"standard": 120},
        inventory={"standard": 100},
        reserved_inventory={"standard": 15},
    )


def test_expansion_charges_increment_and_completes_once(
    manager: CapacityManager,
    company: CompanyState,
) -> None:
    company.capacity_tier = 1
    decision = CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=2)
    flow = manager.apply(company, decision, day=4)

    assert flow.charged_cents == 40_000
    assert flow.refunded_cents == 0
    assert flow.kind is CapacityChangeKind.EXPAND
    assert company.cash_cents == 100_000
    assert company.capacity_tier == 1
    assert company.action_receipts == []
    project = company.capacity_projects[0]
    assert (project.source_tier, project.target_tier, project.started_day) == (1, 2, 4)
    assert project.paid_cents == flow.charged_cents
    assert flow.receipts[0].effective_day == 24
    assert manager.complete(company, 23) == ()

    receipts = manager.complete(company, 24)

    assert company.capacity_tier == 2
    assert project.status is JobStatus.COMPLETED
    assert receipts[0].status == "completed"
    assert receipts[0].charged_cents == receipts[0].refunded_cents == 0
    assert manager.complete(company, 25) == ()


def test_downsize_is_delayed_permanent_and_does_not_refund_installed_capital(
    manager: CapacityManager,
    company: CompanyState,
) -> None:
    company.capacity_tier = 2
    decision = CompanyDecision(company_id="alpha", world_version=0, capacity_downsize_tier=0)
    before = company.model_dump()

    flow = manager.apply(company, decision, 7)

    assert flow.kind is CapacityChangeKind.DOWNSIZE
    assert flow.charged_cents == 4_000
    assert flow.refunded_cents == 0
    assert company.capacity_tier == 2
    assert manager.complete(company, 9) == ()
    manager.complete(company, 10)
    assert company.capacity_tier == 0
    assert company.inventory == before["inventory"]
    assert company.reserved_inventory == before["reserved_inventory"]
    assert company.cash_cents == before["cash_cents"]

    expansion = CompanyDecision(company_id="alpha", world_version=1, capacity_target_tier=2)
    assert manager.apply(company, expansion, 10).charged_cents == 60_000


@pytest.mark.parametrize(("day", "refund"), [(0, 17_000), (5, 8_000), (9, 800)])
def test_cancellation_refunds_only_unspent_prepaid_capital(
    manager: CapacityManager,
    company: CompanyState,
    day: int,
    refund: int,
) -> None:
    manager.apply(
        company, CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=1), 0
    )
    project = company.capacity_projects[0]
    cancellation = CompanyDecision(
        company_id="alpha", world_version=1, cancel_capacity_project_id=project.project_id
    )

    flow = manager.apply(company, cancellation, day)

    assert flow.charged_cents == 0
    assert flow.refunded_cents == refund
    assert flow.receipts[0].status == "cancelled"
    assert project.status is JobStatus.CANCELLED
    assert project.refund_cents == refund
    assert project.cancelled_day == day
    assert company.capacity_tier == 0
    assert company.cash_cents == 100_000
    assert manager.complete(company, 100) == ()
    assert company.capacity_tier == 0

    repeated = manager.apply(company, cancellation, day + 1)
    assert repeated.refunded_cents == 0
    assert repeated.receipts[0].status == "no_op"
    assert project.refund_cents == refund


def test_fee_is_withheld_only_and_refund_cannot_be_negative(
    capacity_scenario: ScenarioConfig,
    company: CompanyState,
) -> None:
    scenario = capacity_scenario.model_copy(
        update={
            "capacity_policy": CapacityPolicyConfig(
                startup_cost_bps=9000, cancellation_fee_bps=5000
            )
        }
    )
    manager = CapacityManager(scenario)
    manager.apply(
        company, CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=1), 0
    )
    project = company.capacity_projects[0]

    flow = manager.apply(
        company,
        CompanyDecision(
            company_id="alpha", world_version=1, cancel_capacity_project_id=project.project_id
        ),
        9,
    )

    assert flow.charged_cents == flow.refunded_cents == 0
    assert project.status is JobStatus.CANCELLED


@pytest.mark.parametrize("complete_first", [False, True])
def test_due_or_completed_expansion_cannot_be_cancelled(
    manager: CapacityManager,
    company: CompanyState,
    complete_first: bool,
) -> None:
    manager.apply(
        company, CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=1), 0
    )
    if complete_first:
        manager.complete(company, 10)
    before = company.model_dump()
    decision = CompanyDecision(
        company_id="alpha",
        world_version=1,
        cancel_capacity_project_id=company.capacity_projects[0].project_id,
    )

    with pytest.raises(ValueError, match="completed or due"):
        manager.apply(company, decision, 10)
    assert company.model_dump() == before


def test_downsize_cannot_be_cancelled(manager: CapacityManager, company: CompanyState) -> None:
    company.capacity_tier = 1
    manager.apply(
        company, CompanyDecision(company_id="alpha", world_version=0, capacity_downsize_tier=0), 0
    )
    decision = CompanyDecision(
        company_id="alpha",
        world_version=1,
        cancel_capacity_project_id=company.capacity_projects[0].project_id,
    )
    before = company.model_dump()

    with pytest.raises(ValueError, match="only unfinished expansion"):
        manager.apply(company, decision, 1)
    assert company.model_dump() == before


def test_one_adjustment_pending_and_duplicate_has_explicit_no_op(
    manager: CapacityManager,
    company: CompanyState,
) -> None:
    first = CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=1)
    manager.apply(company, first, 0)
    before = company.model_dump()
    repeated = manager.apply(company, first, 1)
    assert repeated.charged_cents == 0
    assert repeated.receipts[0].status == "no_op"
    assert company.model_dump() == before

    second = CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=2)
    with pytest.raises(ValueError, match="another capacity adjustment"):
        manager.apply(company, second, 1)
    assert company.model_dump() == before


@pytest.mark.parametrize(
    ("current", "decision", "message"),
    [
        (
            1,
            CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=0),
            "use downsize_capacity",
        ),
        (
            0,
            CompanyDecision(company_id="alpha", world_version=0, capacity_downsize_tier=1),
            "use expand_capacity",
        ),
        (
            0,
            CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=99),
            "unknown capacity tier",
        ),
        (
            1,
            CompanyDecision(company_id="alpha", world_version=0, capacity_downsize_tier=99),
            "unknown capacity tier",
        ),
        (
            0,
            CompanyDecision(
                company_id="alpha", world_version=0, cancel_capacity_project_id="missing"
            ),
            "unknown capacity project",
        ),
        (
            0,
            CompanyDecision(company_id="another", world_version=0, capacity_target_tier=1),
            "another company",
        ),
    ],
)
def test_invalid_request_is_mutation_free(
    manager: CapacityManager,
    company: CompanyState,
    current: int,
    decision: CompanyDecision,
    message: str,
) -> None:
    company.capacity_tier = current
    before = company.model_dump()
    with pytest.raises(ValueError, match=message):
        manager.apply(company, decision, 0)
    assert company.model_dump() == before


def test_active_tier_request_is_explicit_no_op(
    manager: CapacityManager,
    company: CompanyState,
) -> None:
    flow = manager.apply(
        company, CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=0), 0
    )
    assert flow.receipts[0].status == "no_op"
    assert flow.charged_cents == 0
    assert company.capacity_projects == []


def test_bankrupt_company_cannot_receive_capacity_or_refund(
    manager: CapacityManager,
    company: CompanyState,
) -> None:
    manager.apply(
        company, CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=1), 0
    )
    company.status = CompanyStatus.BANKRUPT
    before = company.model_dump()
    assert manager.complete(company, 100) == ()
    decision = CompanyDecision(
        company_id="alpha",
        world_version=1,
        cancel_capacity_project_id=company.capacity_projects[0].project_id,
    )
    with pytest.raises(ValueError, match="bankrupt"):
        manager.apply(company, decision, 1)
    assert company.model_dump() == before
    assert manager.quotes(company, 1) == ()


def test_project_ids_are_reproducible_but_cancelled_restarts_are_distinct(
    manager: CapacityManager,
    company: CompanyState,
) -> None:
    replica = company.model_copy(deep=True)
    expansion = CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=1)
    manager.apply(company, expansion, 0)
    manager.apply(replica, expansion, 0)
    first = company.capacity_projects[0]
    assert first.project_id == replica.capacity_projects[0].project_id
    manager.apply(
        company,
        CompanyDecision(
            company_id="alpha", world_version=0, cancel_capacity_project_id=first.project_id
        ),
        0,
    )
    manager.apply(company, expansion, 0)
    assert first.project_id != company.capacity_projects[1].project_id


def test_quotes_are_exact_and_do_not_copy_or_mutate_unrelated_state(
    manager: CapacityManager,
    company: CompanyState,
) -> None:
    before = company.model_dump()
    quotes = manager.quotes(company, 4)
    assert company.model_dump() == before
    assert {item.action for item in quotes} == {"expand_capacity", "downsize_capacity"}
    assert not any(item.action == "downsize_capacity" and item.target_tier == 1 for item in quotes)
    expansion = next(
        item for item in quotes if item.action == "expand_capacity" and item.target_tier == 1
    )
    flow = manager.apply(
        company, CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=1), 4
    )
    assert expansion.charged_cents == flow.charged_cents == 20_000
    assert expansion.project_id == flow.receipts[0].project_id
    assert expansion.effective_day == flow.receipts[0].effective_day == 14
    pending_before = company.model_dump()
    cancellation = next(
        item for item in manager.quotes(company, 9) if item.action == "cancel_capacity_project"
    )
    assert cancellation.refunded_cents == 8_000
    assert company.model_dump() == pending_before


def test_cancellation_never_precedes_start(manager: CapacityManager, company: CompanyState) -> None:
    manager.apply(
        company, CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=1), 5
    )
    decision = CompanyDecision(
        company_id="alpha",
        world_version=1,
        cancel_capacity_project_id=company.capacity_projects[0].project_id,
    )
    before = company.model_dump()
    with pytest.raises(ValueError, match="cannot precede"):
        manager.apply(company, decision, 4)
    assert company.model_dump() == before


def test_refund_is_nonincreasing_and_always_below_original_payment(
    manager: CapacityManager,
    company: CompanyState,
) -> None:
    manager.apply(
        company, CompanyDecision(company_id="alpha", world_version=0, capacity_target_tier=1), 0
    )
    project = company.capacity_projects[0]
    decision = CompanyDecision(
        company_id="alpha", world_version=1, cancel_capacity_project_id=project.project_id
    )
    refunds = [
        manager.apply(company.model_copy(deep=True), decision, day).refunded_cents
        for day in range(project.completion_day)
    ]

    assert refunds == sorted(refunds, reverse=True)
    assert all(0 <= refund < project.paid_cents for refund in refunds)
    assert project.status is JobStatus.PENDING


def test_empty_decision_leaves_everything_untouched(
    manager: CapacityManager,
    company: CompanyState,
) -> None:
    before = company.model_dump()
    flow = manager.apply(company, CompanyDecision(company_id="alpha", world_version=0), 0)
    assert flow.charged_cents == flow.refunded_cents == 0
    assert flow.receipts == ()
    assert flow.kind is None
    assert company.model_dump() == before
