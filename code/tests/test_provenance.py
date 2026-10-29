"""Tests for the frozen simulation and Arena protocol source set."""

from pathlib import Path

from ceo_arena.provenance import simulation_fingerprints


def test_simulation_fingerprints_include_shared_arena_protocol() -> None:
    project_root = Path(__file__).resolve().parents[1]

    fingerprints = simulation_fingerprints(project_root)

    assert "src/ceo_arena/protocol.py" in fingerprints
    assert "src/ceo_arena/evaluation/leave_one_out.py" in fingerprints
    assert "src/ceo_arena/campaign/resources.py" in fingerprints
    assert "src/ceo_arena/campaign/models.py" in fingerprints
    assert "src/ceo_arena/campaign/records.py" in fingerprints
    assert "src/ceo_arena/campaign/scheduler.py" in fingerprints
