"""The exploratory treatment changes no controller rules beyond day-zero innovation."""

from __future__ import annotations

import pytest

from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from scripts.validate_economy import DiagnosticPolicy, PolicyName
from scripts.validate_innovation_followup import LeanInnovationPolicy


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", [1, 2])
async def test_followup_changes_only_initial_discrete_innovation(tier: int) -> None:
    world = MarketWorld(default_scenario(total_days=14))
    state = world.create_initial_state(("company-1", "company-2"))
    observation = world.project_observations(state)["company-1"]
    policy = LeanInnovationPolicy(tier)
    original = DiagnosticPolicy(PolicyName.LEAN)

    baseline = await original.decide(observation)
    treatment = await policy.decide(observation)

    assert treatment.research_project_tier == tier
    assert baseline.research_project_tier is None
    assert treatment.model_dump(exclude={"research_project_tier"}) == baseline.model_dump(
        exclude={"research_project_tier"}
    )
    assert all(product.development_budget_cents == 0 for product in treatment.products)

    later = observation.model_copy(update={"day": 7, "world_version": 1})
    assert await policy.decide(later) == await original.decide(later)
