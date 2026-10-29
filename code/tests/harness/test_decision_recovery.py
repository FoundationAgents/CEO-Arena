"""Offline coverage of native worker RPC, frozen validation, and durable recovery."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from ceo_arena.config import default_scenario
from ceo_arena.dashboard import RunDashboard
from ceo_arena.economy import MarketWorld
from ceo_arena.execution import recovery_by_company
from ceo_arena.harness import OpenCodeHarness, OpenCodeHarnessConfig
from ceo_arena.harness.opencode import _CompanyWorker
from ceo_arena.harness.wire import WorkerQueryRequest, WorkerQueryResponse
from ceo_arena.models import AgentBudget, CompanyDecision, DecisionWindowRequest
from ceo_arena.runtime import AgentAssignment, BenchmarkCoordinator, RunConfig
from ceo_arena.runtime.context import (
    CompanyContext,
    DecisionRejectedError,
    StaleCompanyContextError,
    _issue_company_context,
)
from ceo_arena.runtime.persistence import RunCheckpoint
from ceo_arena.runtime.replay import ExactReplay
from ceo_arena.runtime.trace import JsonlTraceSink

ROOT = Path(__file__).resolve().parents[2]


class ScriptedProvider(HTTPServer):
    """A local tool-calling endpoint with no upstream network access."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), ScriptedHandler)
        self.bodies: list[str] = []
        self.turns: tuple[tuple[str, dict[str, str | int]], ...] = (
            ("set_product", {"product_id": "A", "price_cents": 6500}),
            ("start_research_project", {"tier": 3}),
            (
                "write_file",
                {
                    "path": "MEMORY.md",
                    "content": (
                        "Limit investment to affordable commitments; "
                        "verify receipts and delayed payoff next week."
                    ),
                },
            ),
            ("seal_decision", {}),
            ("start_research_project", {"tier": 1}),
            ("seal_decision", {}),
        )


class ScriptedHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        server = cast(ScriptedProvider, self.server)
        server.bodies.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
        index = len(server.bodies) - 1
        if index >= len(server.turns):
            self.send_error(500, "unexpected model call")
            return
        name, arguments = server.turns[index]
        frames = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call-{index}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                },
            },
        ]
        body = (
            "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def provider() -> Iterator[ScriptedProvider]:
    with ScriptedProvider() as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_validation_is_bound_revocable_and_never_changes_world() -> None:
    world = MarketWorld(default_scenario(total_days=7))
    state = world.create_initial_state(("acme", "rival"))
    before = world.state_hash(state)
    observation = world.project_observations(state)["acme"]
    with _issue_company_context(observation, partial(world.validate_decision, state)) as context:
        draft = CompanyDecision(company_id="acme", world_version=0, capacity_target_tier=999)
        with pytest.raises(DecisionRejectedError, match="unknown capacity tier"):
            context.validate_decision(draft)
        context.validate_decision(draft.model_copy(update={"capacity_target_tier": 1}))
        with pytest.raises(RuntimeError, match="bound company"):
            context.validate_decision(draft.model_copy(update={"company_id": "rival"}))
    with pytest.raises(StaleCompanyContextError):
        context.validate_decision(draft)
    assert world.state_hash(state) == before


@pytest.mark.asyncio
async def test_native_worker_recovers_preseal_validation_and_replays(tmp_path: Path) -> None:
    with provider() as server:
        harness = OpenCodeHarness(
            OpenCodeHarnessConfig(
                worker_directory=ROOT / "agent_worker",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="offline-model",
                api_key=SecretStr("offline-only-key"),
                trace_directory=tmp_path / "harness",
            )
        )
        config = RunConfig(
            run_id="recovery-test",
            scenario=default_scenario(total_days=7).model_copy(
                update={"initial_cash_cents": 5_000_000}
            ),
            assignments=(
                AgentAssignment(
                    company_id="acme",
                    model_id=harness.model_id,
                    harness_id=harness.harness_id,
                    api_index=0,
                ),
            ),
            provider_endpoint_sha256="0" * 64,
            budget=AgentBudget(max_provider_turns=8, timeout_seconds=30),
        )
        directory = tmp_path / "run"
        async with BenchmarkCoordinator(
            config,
            MarketWorld(config.scenario),
            {"acme": harness},
            directory,
        ) as coordinator:
            outcome = await coordinator.run(resume=False)
        assert len(server.bodies) == 6
        assert "one-time commitments exceed available cash" in server.bodies[4]
        assert "recent_period" in server.bodies[0]
        sealed = outcome.windows[0].decisions[0]
        assert sealed.agent_status == "sealed"
        assert sealed.diagnostics.recovered and sealed.diagnostics.tool_errors == 1
        assert sealed.usage.provider_turns == 6 and sealed.usage.output_tokens == 30
        assert (
            "Limit investment" in next((tmp_path / "agent-memory").rglob("MEMORY.md")).read_text()
        )
        assert outcome.final_state.companies["acme"].prices_cents["A"] == 6500
        projects = outcome.final_state.companies["acme"].innovation_projects
        assert len(projects) == 1 and projects[0].tier == 1
        checkpoint = RunCheckpoint.model_validate_json(
            (directory / "checkpoint.json").read_text(encoding="utf-8")
        )
        assert checkpoint.completed_windows[0].decisions[0].diagnostics == sealed.diagnostics
        report = ExactReplay(
            MarketWorld(config.scenario),
            artifact_directory=tmp_path / "harness",
            runtime_trace_path=directory / "trace.jsonl",
        ).verify(checkpoint)
        assert report.verified_days == 7
        trace = JsonlTraceSink(directory / "trace.jsonl").records()
        assert trace[1].decisions[0].diagnostics.recovered
        assert recovery_by_company(checkpoint.completed_windows)["acme"].recovery_rate == 1.0
        resumed_harness = OpenCodeHarness(harness._config)
        async with BenchmarkCoordinator(
            config,
            MarketWorld(config.scenario),
            {"acme": resumed_harness},
            directory,
        ) as coordinator:
            resumed = await coordinator.run(resume=True)
        assert resumed.windows == outcome.windows
        assert len(server.bodies) == 6  # no completed decision is purchased again


@pytest.mark.asyncio
async def test_exhausted_native_decisions_keep_policies_advance_next_window_and_replay(
    tmp_path: Path,
) -> None:
    with provider() as server:
        server.turns = (
            ("set_product", {"product_id": "A", "price_cents": 99999}),
            (
                "write_file",
                {"path": "MEMORY.md", "content": "Unsealed plans have not yet been executed."},
            ),
        ) * 2
        harness = OpenCodeHarness(
            OpenCodeHarnessConfig(
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="offline-model",
                api_key=SecretStr("offline-only-key"),
                trace_directory=tmp_path / "harness",
            )
        )
        config = RunConfig(
            run_id="exhaustion-test",
            scenario=default_scenario(total_days=14),
            assignments=(
                AgentAssignment(
                    company_id="acme",
                    model_id=harness.model_id,
                    harness_id=harness.harness_id,
                    api_index=0,
                ),
            ),
            provider_endpoint_sha256="0" * 64,
            budget=AgentBudget(max_provider_turns=2),
        )
        directory = tmp_path / "run"
        async with BenchmarkCoordinator(
            config, MarketWorld(config.scenario), {"acme": harness}, directory
        ) as coordinator:
            outcome = await coordinator.run(resume=False)
        assert outcome.final_state.day == 14 and len(server.bodies) == 4
        for window in outcome.windows:
            receipt = window.decisions[0]
            assert receipt.agent_status == "budget_exhausted"
            assert receipt.usage.provider_turns == 2
            assert receipt.decision.products == ()
        assert "Unsealed plans have not yet been executed." in server.bodies[2]
        assert next((tmp_path / "agent-memory").rglob("MEMORY.md")).exists()
        world = MarketWorld(config.scenario)
        reference = world.create_initial_state(("acme",))
        for _ in range(2):
            reference = world.advance_window(
                reference,
                {
                    "acme": CompanyDecision(company_id="acme", world_version=reference.version),
                },
            ).state
        assert world.state_hash(reference) == world.state_hash(outcome.final_state)
        checkpoint = RunCheckpoint.model_validate_json((directory / "checkpoint.json").read_bytes())
        replay = ExactReplay(
            world,
            artifact_directory=tmp_path / "harness",
            runtime_trace_path=directory / "trace.jsonl",
        ).verify(checkpoint)
        assert replay.verified_days == 14
        dashboard = RunDashboard(tmp_path)
        view = dashboard.get_run(dashboard.list_runs().runs[0].run_ref)
        assert view.status == "complete"
        assert all(
            item.status == "budget_exhausted"
            and "budget exhausted" in item.summary
            and not item.actions
            for item in view.companies[0].trajectory
        )


@pytest.mark.asyncio
async def test_late_rpc_replies_do_not_kill_a_worker_after_its_window_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_query = _CompanyWorker._query
    query_count = 0

    async def delayed_query(
        worker: _CompanyWorker,
        request: WorkerQueryRequest,
        context: CompanyContext,
    ) -> WorkerQueryResponse:
        nonlocal query_count
        query_count += 1
        await asyncio.sleep(0.5)
        return await original_query(worker, request, context)

    monkeypatch.setattr(_CompanyWorker, "_query", delayed_query)
    with provider() as server:
        server.turns = (("query_company", {"sql": "SELECT 1"}),) * 64
        harness = OpenCodeHarness(
            OpenCodeHarnessConfig(
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="offline-model",
                api_key=SecretStr("offline-only-key"),
                trace_directory=tmp_path / "harness",
            )
        )
        world = MarketWorld(default_scenario(total_days=14))
        state = world.create_initial_state(("acme",))
        pids: list[int] = []
        try:
            for _ in range(2):
                observation = world.project_observations(state)["acme"]
                request = DecisionWindowRequest(
                    run_id="late-rpc",
                    company_id="acme",
                    world_version=state.version,
                    observation=observation,
                    budget=AgentBudget(max_provider_turns=32, timeout_seconds=0.3),
                )
                with _issue_company_context(observation) as context:
                    result = await harness.decide(request, context)
                assert result.status == "budget_exhausted"
                process = harness._workers["acme"]._process
                assert process is not None
                pids.append(process.pid)
                await asyncio.sleep(0.05)
                assert process.returncode is None
                state = world.advance_window(
                    state,
                    {
                        "acme": CompanyDecision(
                            company_id="acme",
                            world_version=state.version,
                        )
                    },
                ).state
            assert pids[0] == pids[1]
            assert query_count == 2
        finally:
            await harness.aclose()
