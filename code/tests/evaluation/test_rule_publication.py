"""Publication validates completeness and generates numbers from the frozen report."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ceo_arena.evaluation.rule_search import (
    CompanyResult,
    RuleSearchPlan,
    RuleSearchReport,
    SearchCaseResult,
    select_winner,
)
from scripts.publish_rule_search import RuleSearchPublication

ROOT = Path(__file__).resolve().parents[2]


def _report() -> RuleSearchReport:
    plan = RuleSearchPlan.official(ROOT)
    starting_salvage = sum(
        p.initial_inventory * p.salvage_value_cents for p in plan.scenario.products
    )
    companies = tuple(
        CompanyResult(
            company_id=f"company-{index}",
            score_cents=100,
            status="active",
            cash_cents=plan.scenario.initial_cash_cents + 100,
            inventory_salvage_cents=starting_salvage,
            revenue_cents=100,
            payments_cents=0,
            refunds_cents=0,
        )
        for index in range(1, 10)
    )
    development = tuple(
        SearchCaseResult(
            plan_hash=plan.plan_hash,
            split="development",
            candidate_id=candidate.candidate_id,
            config=candidate.config,
            seed=seed,
            run_id="unit-test-fixture",
            run_config_hash="fixture",
            terminal_day=500,
            focal_score_cents=100,
            companies=companies,
            final_state_hash="fixture",
            replay_verified_days=500,
            decision_count=648,
            elapsed_seconds=0,
            artifacts="fixture",
        )
        for candidate in plan.candidates
        for seed in plan.development_seeds
    )
    validation = tuple(
        development[0].model_copy(update={"split": "validation", "seed": seed})
        for seed in plan.validation_seeds
    )
    return RuleSearchReport(
        plan=plan,
        selection=select_winner(plan, development),
        development=development,
        validation=validation,
        generated_at=datetime.now(UTC),
        elapsed_seconds=0,
    )


def test_bilingual_tables_and_receipts_use_complete_report(tmp_path: Path) -> None:
    source = tmp_path / "fixture.json"
    report = _report()
    source.write_text(report.model_dump_json(), encoding="utf-8")
    publication = RuleSearchPublication(ROOT, source)
    publication.root = tmp_path / "publication"
    publication.publish()
    en = (publication.root / "paper/appendix/rule_search_results.tex").read_text()
    zh = (publication.root / "paper/zh/appendix/rule_search_results.tex").read_text("utf-8")
    assert r"\textbf{01}" in en and r"\textbf{01}" in zh
    assert "211 & 1.00" in en and "211 & 1.00" in zh
    assert "401 & 1.00" in en
    exported = RuleSearchReport.model_validate_json(
        (publication.root / "docs/validation/fixed-rule-search-v3.json").read_bytes()
    )
    assert exported == report
    assert publication._money(-12_345) == "-123.45"


@pytest.mark.parametrize("error", ("missing_validation", "incomplete_replay", "broken_cash"))
def test_publication_rejects_incomplete_evidence(tmp_path: Path, error: str) -> None:
    report = _report()
    if error == "missing_validation":
        report = report.model_copy(update={"validation": report.validation[:2]})
    elif error == "incomplete_replay":
        broken = report.validation[0].model_copy(update={"replay_verified_days": 499})
        report = report.model_copy(update={"validation": (broken, *report.validation[1:])})
    else:
        row = report.validation[0]
        company = row.companies[0].model_copy(update={"revenue_cents": 0})
        broken = row.model_copy(update={"companies": (company, *row.companies[1:])})
        report = report.model_copy(update={"validation": (broken, *report.validation[1:])})
    path = tmp_path / "fixture.json"
    path.write_text(report.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match=r"validation seeds|replay evidence|financial identity"):
        RuleSearchPublication(ROOT, path)
