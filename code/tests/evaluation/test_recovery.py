"""Recovery diagnostics remain separate from official profit and failure scores."""

import pytest

from ceo_arena.evaluation import ArenaAnalyzer, ArenaRunResult
from ceo_arena.models import DecisionDiagnostics, RecoverySummary
from ceo_arena.runtime import AgentAssignment


def test_model_health_aggregates_recovered_and_unrecovered_windows() -> None:
    diagnostics = RecoverySummary()
    diagnostics.include(DecisionDiagnostics(protocol_errors=1, tool_errors=2, recovered=True))
    diagnostics.include(DecisionDiagnostics(protocol_errors=1))
    diagnostics.include(DecisionDiagnostics())
    result = ArenaRunResult(
        run_id="run-1",
        lineup_id="test-lineup",
        seed=1,
        assignments=(AgentAssignment(company_id="acme", model_id="model", harness_id="test"),),
        official_score_by_company_cents={"acme": 123},
        decision_status_by_company={"acme": {"sealed": 2, "protocol_error": 1}},
        recovery_by_company={"acme": diagnostics},
    )
    restored = ArenaRunResult.model_validate_json(result.model_dump_json())
    health = ArenaAnalyzer((restored,)).health("model")
    assert health.decision_count == 3 and health.failure_count == 1
    assert health.recovery_rate == 0.5
    assert health.recovery.protocol_errors == 2
    assert health.recovery.tool_errors == 2
    assert restored.model_score("model") == 123


def test_no_errors_has_no_recovery_rate() -> None:
    assert RecoverySummary().recovery_rate is None


def test_infrastructure_timeouts_are_separate_from_model_recovery() -> None:
    summary = RecoverySummary()
    summary.include(DecisionDiagnostics(), infrastructure_timeout=True)
    summary.include(DecisionDiagnostics())
    restored = RecoverySummary.model_validate_json(summary.model_dump_json())
    total = RecoverySummary()
    total.merge(restored)
    assert total.infrastructure_timeout_count == 1
    assert total.windows_with_errors == total.recovered_windows == 0
    assert total.recovery_rate is None


def test_infrastructure_timeout_count_cannot_exceed_actual_timeout_receipts() -> None:
    with pytest.raises(ValueError, match="recovery diagnostics disagree"):
        ArenaRunResult(
            run_id="run-1",
            lineup_id="test-lineup",
            seed=1,
            assignments=(AgentAssignment(company_id="acme", model_id="model", harness_id="test"),),
            official_score_by_company_cents={"acme": 123},
            decision_status_by_company={"acme": {"sealed": 1}},
            recovery_by_company={"acme": RecoverySummary(infrastructure_timeout_count=1)},
        )
