"""Private files are outside economic state; factual weekly feedback remains unchanged."""

import json
import subprocess
from pathlib import Path

from ceo_arena.config import default_scenario
from ceo_arena.dashboard import RunDashboard
from ceo_arena.economy import MarketWorld
from ceo_arena.harness.bun import resolve_bun_executable
from ceo_arena.harness.wire import WorkerDecisionRequest
from ceo_arena.models import CompanyDecision, CompanyState, DecisionWindowRequest
from ceo_arena.protocol import DECISION_PROTOCOL_VERSION
from ceo_arena.provenance import simulation_fingerprints

ROOT = Path(__file__).resolve().parents[2]


def test_authorized_settled_feedback_reaches_the_worker_without_private_memory() -> None:
    world = MarketWorld(default_scenario(seed=42, total_days=14))
    state = world.create_initial_state(["own", "rival"])
    state.companies["rival"].cash_cents = 9876543210123
    state = world.advance_window(
        state,
        {
            company_id: CompanyDecision(company_id=company_id, world_version=state.version)
            for company_id in state.companies
        },
    ).state
    observation = world.project_observations(state)["own"]
    message = WorkerDecisionRequest(
        request_id="briefing-probe",
        request=DecisionWindowRequest(
            run_id="offline",
            company_id="own",
            world_version=state.version,
            observation=observation,
        ),
    )
    process = subprocess.run(
        [resolve_bun_executable(ROOT), "run", "tests/harness/briefing_probe.ts"],
        input=message.model_dump_json(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stderr[:2000]
    data = json.loads(process.stdout.splitlines()[1])
    assert data["recent_period"]["observed_days"] == 7
    assert data["recent_period"]["totals"]["net_cashflow_cents"] == sum(
        row.net_cashflow_cents for row in observation.company.daily_history
    )
    assert "9876543210123" not in process.stdout
    assert data["preceding_period"]["totals"] is None


def test_memory_protocol_has_no_legacy_economic_notebook_or_summary_module() -> None:
    fingerprints = simulation_fingerprints(ROOT)
    assert DECISION_PROTOCOL_VERSION == "ceos-decision-v9"
    assert fingerprints["decision_protocol_version"] == DECISION_PROTOCOL_VERSION
    assert "agent_worker/src/memory.ts" in fingerprints
    assert "agent_worker/src/context.ts" not in fingerprints
    assert "notebook" not in CompanyDecision.model_fields
    assert "notebook" not in CompanyState.model_fields


def test_agent_authored_manifests_are_not_discovered_as_runs(tmp_path: Path) -> None:
    private = tmp_path / "agent-memory" / "owner" / "files"
    private.mkdir(parents=True)
    (private / "manifest.json").write_text('{"private": "must not be indexed"}', encoding="utf-8")
    assert RunDashboard(tmp_path)._discover_manifests() == ()
