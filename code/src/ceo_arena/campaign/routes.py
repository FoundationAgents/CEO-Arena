"""Fixed model-to-credential routing for one local campaign process."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import httpx
from pydantic import SecretStr

from ceo_arena.campaign.models import RULE_MODEL_ID, CampaignRun, FrozenCampaignPlan
from ceo_arena.campaign.spec import ExperimentSpec
from ceo_arena.harness import (
    NOOP_CEO_HARNESS_ID,
    OPENCODE_LLM_COMMIT,
    RULE_CEO_HARNESS_ID,
    NewApiProbe,
    NoopCeoHarness,
    OpenCodeHarness,
    OpenCodeHarnessConfig,
    RuleCeoHarness,
)
from ceo_arena.harness.worker_pool import WorkerPool
from ceo_arena.hashing import provider_endpoint_fingerprint
from ceo_arena.inference import InferenceSettings
from ceo_arena.protocol import NOOP_MODEL_ID
from ceo_arena.runtime import AgentHarness


@dataclass(frozen=True, slots=True)
class RuntimeModelRoute:
    """One process-only route whose credential never enters a frozen plan."""

    model_id: str
    api_index: int
    base_url: str
    api_key: SecretStr
    inference: InferenceSettings


class ModelRouteRegistry:
    """Resolve each unique model identity to exactly one configured API entry."""

    def __init__(
        self,
        spec: ExperimentSpec,
        model_harness_builder: Callable[[RuntimeModelRoute, Path], AgentHarness] | None = None,
        *,
        worker_pool: WorkerPool | None = None,
    ) -> None:
        routes = (
            RuntimeModelRoute(
                model_id=model_id,
                api_index=api_index,
                base_url=spec.base_url,
                api_key=api.api_key,
                inference=spec.inference[model_id],
            )
            for api_index, api in enumerate(spec.api)
            for model_id in api.models
        )
        self._routes = {route.model_id: route for route in routes}
        if len(self._routes) != len(spec.model_ids):
            raise ValueError("runtime model routes must be globally unique")
        self._base_url = spec.base_url
        self._model_order = spec.model_ids
        self._endpoint_sha256 = provider_endpoint_fingerprint(spec.base_url)
        self._model_harness_builder = model_harness_builder or partial(
            _open_code_harness, worker_pool=worker_pool
        )
        self._api_entries = tuple(
            (index, api.api_key, api.models) for index, api in enumerate(spec.api)
        )
        self._requires_catalog_validation = model_harness_builder is None

    async def validate_catalog(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Verify each key can see every model assigned to that API entry."""

        if not self._requires_catalog_validation and transport is None:
            return

        async def validate_entry(
            api_index: int,
            api_key: SecretStr,
            model_ids: tuple[str, ...],
        ) -> tuple[int, tuple[str, ...]]:
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {api_key.get_secret_value()}"},
                timeout=30.0,
                transport=transport,
            ) as client:
                result = await NewApiProbe(client, self._base_url).probe(model_ids)
            return api_index, result.missing_models

        results = await asyncio.gather(*(validate_entry(*entry) for entry in self._api_entries))
        missing = tuple((index, models) for index, models in results if models)
        if missing:
            details = "; ".join(
                f"api[{index}] missing {', '.join(model_ids)}" for index, model_ids in missing
            )
            raise ValueError(f"configured NewAPI model routes are unavailable: {details}")

    def validate_plan(self, plan: FrozenCampaignPlan) -> None:
        """Reject a frozen plan that differs from the credential-bearing spec."""

        if plan.provider_endpoint_sha256 != self._endpoint_sha256:
            raise ValueError("campaign provider endpoint differs from the active routes")
        expected = tuple(
            (route.model_id, route.api_index, route.inference)
            for route in sorted(self._routes.values(), key=lambda route: route.model_id)
        )
        actual = tuple((route.model_id, route.api_index, route.inference) for route in plan.routes)
        if actual != expected:
            raise ValueError("campaign model routing or inference differs from the active routes")
        if plan.model_order != self._model_order:
            raise ValueError("campaign model seat order differs from the active spec")

    def resolve(self, model_id: str, api_index: int) -> RuntimeModelRoute:
        """Return one exact route and reject stale compiled routing."""

        try:
            route = self._routes[model_id]
        except KeyError as error:
            raise ValueError(f"model is not configured by any API entry: {model_id}") from error
        if route.api_index != api_index:
            raise ValueError(f"model API route drifted for {model_id}")
        return route

    def create_harnesses(
        self,
        run: CampaignRun,
        trace_directory: Path,
    ) -> dict[str, AgentHarness]:
        """Create one isolated Harness per fixed company assignment."""

        harnesses: dict[str, AgentHarness] = {}
        for company_id, model_id, api_index in zip(
            run.company_ids,
            run.model_ids,
            run.api_indices,
            strict=True,
        ):
            if model_id == RULE_MODEL_ID:
                harnesses[company_id] = RuleCeoHarness()
                continue
            if model_id == NOOP_MODEL_ID:
                harnesses[company_id] = NoopCeoHarness()
                continue
            if api_index is None:
                raise ValueError(f"provider-backed model has no API route: {model_id}")
            route = self.resolve(model_id, api_index)
            harnesses[company_id] = self._model_harness_builder(route, trace_directory)
        return harnesses

    @staticmethod
    def harness_id(model_id: str) -> str:
        """Return the frozen Harness identity for one model route."""

        if model_id == RULE_MODEL_ID:
            return RULE_CEO_HARNESS_ID
        if model_id == NOOP_MODEL_ID:
            return NOOP_CEO_HARNESS_ID
        return f"opencode-native-{OPENCODE_LLM_COMMIT[:12]}"


def _open_code_harness(
    route: RuntimeModelRoute,
    trace_directory: Path,
    *,
    worker_pool: WorkerPool | None = None,
) -> AgentHarness:
    """Build the production OpenCode Adapter for one fixed route."""

    return OpenCodeHarness(
        OpenCodeHarnessConfig(
            base_url=route.base_url,
            model=route.model_id,
            api_key=route.api_key,
            inference=route.inference,
            trace_directory=trace_directory,
        ),
        worker_pool=worker_pool,
    )


__all__ = ["ModelRouteRegistry", "RuntimeModelRoute"]
