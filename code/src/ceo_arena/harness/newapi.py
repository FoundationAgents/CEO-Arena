"""Secret-safe NewAPI catalog preflight."""

from __future__ import annotations

from collections.abc import Sequence

import httpx
from pydantic import ConfigDict, Field

from ceo_arena.models import StrictModel


class NewApiModel(StrictModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)


class NewApiModelCatalog(StrictModel):
    model_config = ConfigDict(extra="ignore")

    data: tuple[NewApiModel, ...]


class NewApiProbeResult(StrictModel):
    base_url: str
    available_model_count: int = Field(ge=0)
    required_models: tuple[str, ...]
    missing_models: tuple[str, ...]
    tool_call_smoke_required: bool = True


class NewApiProbe:
    """Validate `/models` without exposing credentials or claiming tool compatibility."""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def probe(self, required_models: Sequence[str] = ()) -> NewApiProbeResult:
        """Fetch the model catalog and report missing requested model IDs."""

        response = await self._client.get(f"{self._base_url}/models")
        response.raise_for_status()
        catalog = NewApiModelCatalog.model_validate(response.json())
        available = {item.id for item in catalog.data}
        required = tuple(dict.fromkeys(required_models))
        return NewApiProbeResult(
            base_url=self._base_url,
            available_model_count=len(available),
            required_models=required,
            missing_models=tuple(model for model in required if model not in available),
        )
