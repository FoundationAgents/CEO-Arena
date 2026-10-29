"""Observability must not acknowledge a receipt that failed to persist."""

from pathlib import Path

import pytest

from ceo_arena.models import CompanyDecision
from ceo_arena.runtime.performance import PerformanceStatus
from ceo_arena.runtime.persistence import CollectingWindow, SealedDecision
from ceo_arena.runtime.store import RunStore


def test_failed_receipt_is_not_reported_as_received(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(tmp_path)
    window = CollectingWindow(
        window_index=0,
        start_day=0,
        start_world_version=0,
        start_state_hash="state",
        expected_company_ids=("company-1",),
    )
    receipt = SealedDecision(
        company_id="company-1",
        agent_status="sealed",
        decision=CompanyDecision(company_id="company-1", world_version=0),
    )

    def fail_write(**_arguments: object) -> None:
        raise OSError("private disk path is not diagnostic output")

    monkeypatch.setattr(store.checkpoints, "stage_decision", fail_write)
    with pytest.raises(OSError, match="private disk"):
        store.stage_decision(run_id="test", config_hash="config", window=window, response=receipt)

    status = PerformanceStatus.model_validate_json(
        (tmp_path / "performance_status.json").read_bytes()
    )
    assert status.companies["company-1"].state == "failed"
    assert "decision_stage" not in status.stage_count
    assert status.stage_count["decision_stage_failed"] == 1
    assert "private disk path" not in status.model_dump_json()
