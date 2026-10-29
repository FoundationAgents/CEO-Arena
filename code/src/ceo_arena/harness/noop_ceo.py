"""A CEO that never changes the environment's initial standing policies."""

from __future__ import annotations

from ceo_arena.harness.deterministic import DeterministicCeoHarness
from ceo_arena.models import CompanyDecision, CompanyObservation
from ceo_arena.protocol import NOOP_MODEL_ID

NOOP_CEO_HARNESS_ID = "rule-ceo-noop-v1"


class NoopCeoHarness(DeterministicCeoHarness):
    """Submit only a legal empty decision; ordinary economic automation continues."""

    @property
    def harness_id(self) -> str:
        return NOOP_CEO_HARNESS_ID

    @property
    def model_id(self) -> str:
        return NOOP_MODEL_ID

    def _decision(self, observation: CompanyObservation) -> CompanyDecision:
        return CompanyDecision(
            company_id=observation.company.company_id,
            world_version=observation.world_version,
        )
