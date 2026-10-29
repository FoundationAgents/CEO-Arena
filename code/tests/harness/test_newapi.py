import httpx
import pytest

from ceo_arena.harness import NewApiProbe


@pytest.mark.asyncio
async def test_newapi_probe_reports_missing_models_without_overclaiming_tools() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer test-secret"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "model-a",
                        "object": "model",
                        "created": 1,
                        "owned_by": "provider",
                    },
                    {
                        "id": "model-b",
                        "object": "model",
                        "created": 2,
                        "owned_by": "provider",
                    },
                ],
            },
        )

    async with httpx.AsyncClient(
        base_url="https://newapi.example",
        headers={"Authorization": "Bearer test-secret"},
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await NewApiProbe(client, "https://newapi.example/v1/").probe(
            ("model-a", "model-c")
        )

    assert result.available_model_count == 2
    assert result.missing_models == ("model-c",)
    assert result.tool_call_smoke_required
