"""Fast model-free validation checks; no test prescribes who must earn money."""

from __future__ import annotations

from pathlib import Path

import pytest

from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.models import CompanyDecision
from scripts.validate_economy import (
    DiagnosticPolicy,
    EconomyValidator,
    PolicyName,
    argument_parser,
)


def test_no_action_requires_explicit_diagnostic_selection() -> None:
    parser = argument_parser()
    assert parser.parse_args([]).policies == ["rule", "lean", "adaptive", "reckless"]
    assert parser.parse_args(["--policies", "no_action"]).policies == ["no_action"]


def test_paper_validation_fingerprint_includes_extracted_v3_mechanisms() -> None:
    paper_validation = pytest.importorskip(
        "paper.scripts.validate_scenario",
        reason="The released supplement does not include the manuscript validation module.",
    )
    assert {
        "src/ceo_arena/economy/capacity.py",
        "src/ceo_arena/economy/market.py",
    } <= set(paper_validation.ScenarioValidator._SOURCE_PATHS)


@pytest.mark.asyncio
async def test_no_action_is_genuinely_empty_and_does_not_mutate_observation() -> None:
    world = MarketWorld(default_scenario(total_days=7))
    state = world.create_initial_state(("company-1", "company-2"))
    observation = world.project_observations(state)["company-1"]
    before = observation.model_dump()

    decision = await DiagnosticPolicy(PolicyName.NO_ACTION).decide(observation)

    assert decision == CompanyDecision(company_id="company-1", world_version=0)
    assert observation.model_dump() == before


@pytest.mark.asyncio
async def test_lean_controller_caps_ticket_spend_and_removes_rd_only() -> None:
    world = MarketWorld(default_scenario(total_days=7))
    state = world.create_initial_state(("company-1", "company-2"))
    observation = world.project_observations(state)["company-1"]
    observation.company.support_backlog = 4

    lean = await DiagnosticPolicy(PolicyName.LEAN).decide(observation)
    rule = await DiagnosticPolicy(PolicyName.RULE).decide(observation)

    assert lean.support_budget_cents == 2_000
    assert all(product.development_budget_cents == 0 for product in lean.products)
    assert lean.marketing == rule.marketing
    assert lean.enterprise_bids == rule.enterprise_bids
    assert lean.market_research == rule.market_research
    assert any(product.development_budget_cents for product in rule.products)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", tuple(PolicyName))
async def test_short_nine_company_diagnostics_reconcile_cash(policy: PolicyName) -> None:
    validator = EconomyValidator(default_scenario(total_days=7), Path(__file__).parents[2])

    run = await validator.run_one(7, policy)

    assert run.terminal_day == 7
    assert len(run.companies) == 9
    assert all(company.cash_identity_holds for company in run.companies)
    assert all(
        company.expense_breakdown.total == company.cash_payments_cents for company in run.companies
    )
    assert run.system_score_cents == sum(company.official_score_cents for company in run.companies)


@pytest.mark.asyncio
async def test_scripted_diagnostics_are_reproducible_and_do_not_change_rule_source() -> None:
    validator = EconomyValidator(
        default_scenario(total_days=3), Path(__file__).parents[2], company_count=2
    )

    first = await validator.run_one(7, PolicyName.ADAPTIVE)
    second = await validator.run_one(7, PolicyName.ADAPTIVE)

    assert first == second


@pytest.mark.asyncio
async def test_report_has_frozen_provenance_and_no_provider_calls() -> None:
    validator = EconomyValidator(
        default_scenario(total_days=2), Path(__file__).parents[2], company_count=2
    )

    report = await validator.run((7,), (PolicyName.LEAN,), max_seconds=30)

    assert report.model_api_calls == 0
    assert report.all_requested_runs_completed
    assert "scripts/validate_economy.py" in report.source_sha256
    assert "src/ceo_arena/harness/rule_ceo.py" in report.source_sha256
    assert len(report.scenario_sha256) == len(report.policy_parameters_sha256) == 64
    assert report.seeds_requested == (7,)


@pytest.mark.asyncio
async def test_expired_time_budget_returns_explicit_partial_report() -> None:
    validator = EconomyValidator(
        default_scenario(total_days=2), Path(__file__).parents[2], company_count=2
    )

    report = await validator.run((7,), (PolicyName.LEAN,), max_seconds=1e-9)

    assert report.runs == ()
    assert not report.all_requested_runs_completed


@pytest.mark.asyncio
async def test_process_parallelism_does_not_change_world_results() -> None:
    validator = EconomyValidator(
        default_scenario(total_days=2), Path(__file__).parents[2], company_count=2
    )
    policies = (PolicyName.RULE, PolicyName.LEAN)
    serial = await validator.run((7,), policies, max_seconds=30)
    parallel = await validator.run((7,), policies, max_seconds=30, workers=2)

    assert parallel.workers == 2
    assert serial.runs == parallel.runs


@pytest.mark.asyncio
async def test_report_refuses_mixed_source_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    validator = EconomyValidator(
        default_scenario(total_days=2), Path(__file__).parents[2], company_count=2
    )
    hashes = iter(({"source.py": "before"}, {"source.py": "after"}))
    monkeypatch.setattr(validator, "_source_hashes", lambda: next(hashes))

    with pytest.raises(RuntimeError, match="source changed"):
        await validator.run((7,), (PolicyName.LEAN,), max_seconds=1e-9)
