"""Shared identities and fixed-seat design for the official Arena protocol."""

from __future__ import annotations

import hashlib
import json

from pydantic import Field, field_validator, model_validator

from ceo_arena.models import StrictModel

RULE_MODEL_ID = "deterministic-rule-ceo"
NOOP_MODEL_ID = "no-op-ceo"
DECISION_PROTOCOL_VERSION = "ceos-decision-v9"


def canonical_lineup_id(model_ids: tuple[str, ...]) -> str:
    """Return the collision-resistant identity of one canonical model set."""

    payload = json.dumps(
        tuple(sorted(model_ids)),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"lineup-{hashlib.sha256(payload).hexdigest()}"


class LeaveOneOutDesign(StrictModel):
    """Generate and validate all fixed-seat single-omission lineups."""

    model_order: tuple[str, ...]
    rule_model_id: str = Field(default=RULE_MODEL_ID, min_length=1)

    @field_validator("model_order")
    @classmethod
    def validate_model_order(cls, model_order: tuple[str, ...]) -> tuple[str, ...]:
        if not model_order:
            raise ValueError("Leave-One-Out design requires at least one provider model")
        if len(model_order) != len(set(model_order)):
            raise ValueError("Leave-One-Out provider model ids must be unique")
        if any(not model_id or model_id != model_id.strip() for model_id in model_order):
            raise ValueError("Leave-One-Out provider model ids must be non-empty and trimmed")
        return model_order

    @model_validator(mode="after")
    def validate_rule_model(self) -> LeaveOneOutDesign:
        if self.rule_model_id != self.rule_model_id.strip():
            raise ValueError("Leave-One-Out rule model id must be trimmed")
        if self.rule_model_id in self.model_order:
            raise ValueError("Leave-One-Out rule model id must be distinct from provider models")
        return self

    @property
    def company_count(self) -> int:
        """Return the fixed number of company seats in every lineup."""

        return len(self.model_order)

    @property
    def company_ids(self) -> tuple[str, ...]:
        """Return canonical company ids in the same order as the fixed model seats."""

        return tuple(f"company-{index + 1}" for index in range(self.company_count))

    def seat_ordered_lineups(self) -> tuple[tuple[str, ...], ...]:
        """Return L_-R first, then L_-M1 through L_-MK in fixed seat order."""

        all_models = self.model_order
        substituted = tuple(
            (*all_models[:seat], self.rule_model_id, *all_models[seat + 1 :])
            for seat in range(self.company_count)
        )
        return (all_models, *substituted)

    def omitted_model_id(self, lineup: tuple[str, ...]) -> str:
        """Return the sole omitted identity after validating a canonical lineup."""

        try:
            index = self.seat_ordered_lineups().index(lineup)
        except ValueError:
            raise ValueError("lineup is not a canonical fixed-seat Leave-One-Out lineup") from None
        return (self.rule_model_id, *self.model_order)[index]

    def validate_lineups(
        self,
        lineups: tuple[tuple[str, ...], ...],
    ) -> tuple[tuple[str, ...], ...]:
        """Reject an incomplete, reordered, or seat-drifted lineup matrix."""

        if lineups != self.seat_ordered_lineups():
            raise ValueError(
                "lineups are not the complete canonical fixed-seat Leave-One-Out design"
            )
        return lineups


__all__ = ["RULE_MODEL_ID", "LeaveOneOutDesign", "canonical_lineup_id"]
