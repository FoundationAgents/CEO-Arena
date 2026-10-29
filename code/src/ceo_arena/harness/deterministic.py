"""Shared protocol checks for deterministic, provider-free CEO policies."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ceo_arena.models import (
    AgentUsage,
    CompanyDecision,
    CompanyObservation,
    DecisionWindowRequest,
    DecisionWindowResult,
)
from ceo_arena.runtime.context import CompanyContext


class DeterministicCeoHarness(ABC):
    """Seal a deterministic policy decision without model calls or query-side mutation."""

    @property
    @abstractmethod
    def harness_id(self) -> str:
        """Return the frozen policy identity."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Return the controller identity."""

    @abstractmethod
    def _decision(self, observation: CompanyObservation) -> CompanyDecision:
        """Build the policy's ordinary decision."""

    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        """Validate the company scope and return an accounting-valid sealed decision."""

        observation = request.observation
        if (
            context.company_id != request.company_id
            or context.world_version != request.world_version
        ):
            return self._error("company context does not match the decision request")
        if observation.company.company_id != request.company_id:
            return self._error("request company does not match the private observation")
        if observation.world_version != request.world_version:
            return self._error("request world version does not match the private observation")
        try:
            decision = self._decision(observation)
        except ValueError as error:
            return self._error(str(error))
        return DecisionWindowResult(status="sealed", decision=decision, usage=AgentUsage())

    async def cancel(self, company_id: str) -> None:
        """No background inference exists to cancel."""
        del company_id

    async def aclose(self) -> None:
        """No worker resources are owned by a deterministic policy."""
        return None

    @staticmethod
    def _error(message: str) -> DecisionWindowResult:
        return DecisionWindowResult(status="protocol_error", error=message)
