"""Narrow interfaces between the coordinator, agent Harnesses, and economy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from ceo_arena.inference import InferenceSettings
from ceo_arena.models import (
    CompanyDecision,
    CompanyObservation,
    DecisionWindowRequest,
    DecisionWindowResult,
    ScenarioConfig,
    WindowResult,
    WorldState,
)
from ceo_arena.runtime.context import CompanyContext


@runtime_checkable
class InferenceAwareHarness(Protocol):
    """Expose effective provider settings for manifest consistency checks."""

    @property
    def inference_settings(self) -> InferenceSettings:
        """Return the resolved, secret-free provider settings."""


@runtime_checkable
class AgentHarness(Protocol):
    """One model-facing Harness capable of sealing a decision window."""

    @property
    def harness_id(self) -> str:
        """Return the immutable Harness implementation identifier."""

    @property
    def model_id(self) -> str:
        """Return the immutable model route identity used by this Harness."""

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        """Produce one sealed company decision within the supplied budget."""

    async def cancel(self, company_id: str) -> None:
        """Stop in-flight work for one company after timeout or cancellation."""

    async def aclose(self) -> None:
        """Release all Harness resources."""


@runtime_checkable
class MarketWorld(Protocol):
    """Functional shared-world boundary consumed by orchestration and replay."""

    @property
    def config(self) -> ScenarioConfig:
        """Return the immutable scenario configuration."""

    def create_initial_state(self, company_ids: Sequence[str]) -> WorldState:
        """Create the deterministic initial state for a company lineup."""

    def project_observations(
        self,
        state: WorldState,
    ) -> dict[str, CompanyObservation]:
        """Project simultaneous private observations from one world snapshot."""

    def advance_window(
        self,
        state: WorldState,
        decisions: Mapping[str, CompanyDecision] | Sequence[CompanyDecision],
    ) -> WindowResult:
        """Advance the shared economy once after the decision barrier closes."""

    def state_hash(self, state: WorldState) -> str:
        """Return the canonical deterministic hash of a world state."""

    def validate_decision(self, state: WorldState, decision: CompanyDecision) -> None:
        """Validate one decision semantically without changing the world."""
