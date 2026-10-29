"""FastAPI adapter serving the local CEO Arena dashboard and Campaign control."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from ceo_arena import __version__
from ceo_arena.dashboard.catalog import RunDashboard
from ceo_arena.dashboard.control import (
    CampaignControl,
    CampaignControlError,
    CampaignControlErrorCode,
    ExperimentCatalog,
    StartReceipt,
)
from ceo_arena.dashboard.models import (
    DashboardRun,
    DashboardRunList,
    DashboardUpdate,
    RunControlReceipt,
    RunControlRequest,
)
from ceo_arena.dashboard.run_control import RunControl, RunControlError, RunControlErrorCode

_EVENT_POLL_SECONDS = 0.75
_EVENT_HEARTBEAT_SECONDS = 15.0
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


class _DashboardEventStream:
    """Turn durable revisions into one cancellable same-origin SSE stream."""

    def __init__(self, dashboard: RunDashboard, control: CampaignControl | None) -> None:
        self._dashboard = dashboard
        self._control = control

    def response(self, request: Request) -> StreamingResponse:
        """Create an unbuffered event-stream response for one browser client."""

        return StreamingResponse(
            self._events(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    async def _events(self, request: Request) -> AsyncIterator[str]:
        last_revision: str | None = None
        last_message_at = asyncio.get_running_loop().time()
        while not await request.is_disconnected():
            revision = await asyncio.to_thread(self._revision)
            now = asyncio.get_running_loop().time()
            if revision != last_revision:
                payload = DashboardUpdate(revision=revision).model_dump_json()
                yield f"event: update\ndata: {payload}\n\n"
                last_revision = revision
                last_message_at = now
            elif now - last_message_at >= _EVENT_HEARTBEAT_SECONDS:
                yield ": heartbeat\n\n"
                last_message_at = now
            await asyncio.sleep(_EVENT_POLL_SECONDS)

    def _revision(self) -> str:
        parts = [self._dashboard.revision()]
        if self._control is not None:
            parts.append(self._control.revision())
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def create_dashboard_app(
    runs_root: Path,
    *,
    campaign_config_root: Path | None = None,
    control: CampaignControl | None = None,
    run_control: RunControl | None = None,
    max_active_campaigns: int = 1,
) -> FastAPI:
    """Create a dashboard with optional loopback-only Campaign control."""

    if campaign_config_root is not None and control is not None:
        raise ValueError("provide campaign_config_root or control, not both")
    if run_control is not None and control is None:
        raise ValueError("run_control requires an injected Campaign control")
    if max_active_campaigns < 1:
        raise ValueError("max_active_campaigns must be positive")
    resolved_runs_root = runs_root.resolve()
    active_control = control
    if active_control is None and campaign_config_root is not None:
        active_control = CampaignControl(
            campaign_config_root.resolve(),
            resolved_runs_root,
            max_active_campaigns=max_active_campaigns,
        )
    dashboard = RunDashboard(
        resolved_runs_root,
        controls_enabled=active_control is not None,
    )
    active_run_control = run_control
    if active_run_control is None and active_control is not None:
        active_run_control = RunControl(dashboard, active_control)
    event_stream = _DashboardEventStream(dashboard, active_control)
    control_token = secrets.token_urlsafe(32) if active_control is not None else None
    app = FastAPI(
        title="CEO Arena Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if active_control is not None:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(_LOOPBACK_HOSTS))

    @app.middleware("http")
    async def no_store_dashboard(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' "
            "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/runs", response_model=DashboardRunList)
    def list_runs() -> DashboardRunList:
        return dashboard.list_runs()

    @app.get("/api/runs/{run_ref}", response_model=DashboardRun)
    def get_run(run_ref: str) -> DashboardRun:
        try:
            return dashboard.get_run(run_ref)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post(
        "/api/runs/{run_ref}/control",
        response_model=RunControlReceipt,
        status_code=202,
    )
    async def control_run(
        run_ref: str,
        command: RunControlRequest,
        request: Request,
    ) -> RunControlReceipt:
        if active_run_control is None or control_token is None:
            raise HTTPException(status_code=404, detail="run control is disabled")
        _authorize_control_request(request, control_token)
        idempotency_key = request.headers.get("idempotency-key", "")
        if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise HTTPException(status_code=400, detail="invalid Idempotency-Key")
        try:
            return await asyncio.to_thread(
                active_run_control.control,
                run_ref,
                command,
                idempotency_key,
            )
        except RunControlError as exc:
            status_code = {
                RunControlErrorCode.NOT_FOUND: 404,
                RunControlErrorCode.LAUNCH_FAILED: 503,
            }.get(exc.code, 409)
            raise HTTPException(status_code=status_code, detail=exc.public_message) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="run control failed") from exc

    @app.get("/api/experiments", response_model=ExperimentCatalog)
    def list_experiments() -> ExperimentCatalog:
        if active_control is None:
            return ExperimentCatalog(revision=dashboard.revision(), experiments=())
        try:
            return active_control.snapshot()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="experiment catalog is temporarily unavailable",
            ) from exc

    @app.get("/api/control-session")
    def control_session() -> dict[str, str]:
        if control_token is None:
            raise HTTPException(status_code=404, detail="campaign control is disabled")
        return {"control_token": control_token}

    @app.post(
        "/api/experiments/{experiment_ref}/start",
        response_model=StartReceipt,
        status_code=202,
    )
    async def start_experiment(experiment_ref: str, request: Request) -> StartReceipt:
        if active_control is None or control_token is None:
            raise HTTPException(status_code=404, detail="campaign control is disabled")
        _authorize_control_request(request, control_token)
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="request body must be an empty object",
            ) from exc
        if payload != {}:
            raise HTTPException(status_code=422, detail="request body must be an empty object")
        idempotency_key = request.headers.get("idempotency-key", "")
        if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise HTTPException(status_code=400, detail="invalid Idempotency-Key")
        try:
            return await asyncio.to_thread(
                active_control.start,
                experiment_ref,
                idempotency_key,
            )
        except CampaignControlError as exc:
            status_code = {
                CampaignControlErrorCode.NOT_FOUND: 404,
                CampaignControlErrorCode.INVALID_CONFIG: 422,
                CampaignControlErrorCode.LAUNCH_FAILED: 503,
            }.get(exc.code, 409)
            raise HTTPException(status_code=status_code, detail=exc.public_message) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="experiment not found") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="experiment could not be started") from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail="experiment could not be started",
            ) from exc

    @app.get("/api/events")
    def events(request: Request) -> StreamingResponse:
        return event_stream.response(request)

    static_directory = Path(__file__).with_name("static")
    app.mount("/", StaticFiles(directory=static_directory, html=True), name="dashboard")
    return app


def _authorize_control_request(request: Request, expected_token: str) -> None:
    """Reject cross-origin, non-JSON, and tokenless control requests."""

    content_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="application/json is required")
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if origin is None or host is None or origin.rstrip("/") != f"{request.url.scheme}://{host}":
        raise HTTPException(status_code=403, detail="same-origin request required")
    supplied_token = request.headers.get("x-ceoarena-control", "")
    if not secrets.compare_digest(supplied_token, expected_token):
        raise HTTPException(status_code=403, detail="invalid control session")
