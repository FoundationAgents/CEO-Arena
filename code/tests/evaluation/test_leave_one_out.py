"""Tests for the canonical fixed-seat Leave-One-Out design."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ceo_arena.evaluation.leave_one_out import LeaveOneOutDesign

_RULE_MODEL_ID = "deterministic-rule-ceo"


def _design() -> LeaveOneOutDesign:
    return LeaveOneOutDesign(
        model_order=("model-a", "model-b", "model-c"),
        rule_model_id=_RULE_MODEL_ID,
    )


def test_lineups_leave_out_rule_then_each_model_without_moving_other_models() -> None:
    design = _design()

    assert design.company_count == 3
    assert design.company_ids == ("company-1", "company-2", "company-3")
    assert design.seat_ordered_lineups() == (
        ("model-a", "model-b", "model-c"),
        (_RULE_MODEL_ID, "model-b", "model-c"),
        ("model-a", _RULE_MODEL_ID, "model-c"),
        ("model-a", "model-b", _RULE_MODEL_ID),
    )
    assert tuple(
        design.omitted_model_id(lineup) for lineup in design.seat_ordered_lineups()
    ) == (_RULE_MODEL_ID, "model-a", "model-b", "model-c")


def test_validation_rejects_missing_or_seat_drifted_lineups() -> None:
    lineups = _design().seat_ordered_lineups()

    with pytest.raises(ValueError, match="complete canonical"):
        _design().validate_lineups(lineups[:-1])

    seat_drifted = (lineups[0], (lineups[1][1], lineups[1][0], lineups[1][2]), *lineups[2:])
    with pytest.raises(ValueError, match="fixed-seat"):
        _design().validate_lineups(seat_drifted)

    with pytest.raises(ValueError, match="not a canonical"):
        _design().omitted_model_id(("model-a", _RULE_MODEL_ID, "model-b"))


@pytest.mark.parametrize(
    "payload",
    [
        {"model_order": (), "rule_model_id": _RULE_MODEL_ID},
        {"model_order": ("model-a", "model-a"), "rule_model_id": _RULE_MODEL_ID},
        {"model_order": ("model-a", _RULE_MODEL_ID), "rule_model_id": _RULE_MODEL_ID},
    ],
)
def test_design_rejects_ambiguous_identities(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LeaveOneOutDesign.model_validate(payload)
