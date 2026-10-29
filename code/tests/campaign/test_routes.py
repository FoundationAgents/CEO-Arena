"""Tests for fixed multi-API model routing."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ceo_arena.campaign import CampaignCompiler, ExperimentSpec
from ceo_arena.campaign.routes import ModelRouteRegistry
from ceo_arena.harness import RuleCeoHarness


class _RouteHarness:
    def __init__(self, model_id: str, api_index: int) -> None:
        self.model_id = model_id
        self.harness_id = f"route-{api_index}"


def _spec() -> ExperimentSpec:
    return ExperimentSpec.model_validate(
        {
            "campaign_id": "routes-test",
            "base_url": "https://newapi.example/v1",
            "company_count": 3,
            "days": 14,
            "seeds": [7],
            "api": [
                {"api_key": "key-one-secret", "models": ["model-a", "model-b"]},
                {"api_key": "key-two-secret", "models": ["model-c"]},
            ],
        }
    )


def test_registry_routes_one_lineup_across_multiple_keys_without_serializing_them(
    tmp_path: Path,
) -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec)
    built: list[tuple[str, int]] = []

    def build(route, _trace_directory):
        built.append((route.model_id, route.api_index))
        return _RouteHarness(route.model_id, route.api_index)

    registry = ModelRouteRegistry(spec, build)
    registry.validate_plan(plan)
    run = next(
        item
        for item in plan.runs
        if set(item.model_ids) == {"model-a", "model-c", "deterministic-rule-ceo"}
    )

    harnesses = registry.create_harnesses(run, tmp_path / "traces")

    assert isinstance(harnesses["company-1"], RuleCeoHarness) == (
        run.model_ids[0] == "deterministic-rule-ceo"
    )
    assert set(built) == {("model-a", 0), ("model-c", 1)}
    serialized = plan.model_dump_json() + repr(registry)
    assert "key-one-secret" not in serialized
    assert "key-two-secret" not in serialized


def test_registry_rejects_plan_route_drift() -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec)
    registry = ModelRouteRegistry(spec.model_copy(update={"base_url": "https://other.example/v1"}))

    with pytest.raises(ValueError, match="endpoint differs"):
        registry.validate_plan(plan)


@pytest.mark.asyncio
async def test_catalog_validation_uses_each_keys_own_model_catalog() -> None:
    catalogs = {
        "Bearer key-one-secret": ["model-a", "model-b"],
        "Bearer key-two-secret": ["model-c"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        models = catalogs[request.headers["Authorization"]]
        return httpx.Response(200, json={"data": [{"id": model} for model in models]})

    registry = ModelRouteRegistry(_spec())
    await registry.validate_catalog(transport=httpx.MockTransport(handler))

    catalogs["Bearer key-two-secret"] = []
    with pytest.raises(ValueError, match=r"api\[1\] missing model-c"):
        await registry.validate_catalog(transport=httpx.MockTransport(handler))
