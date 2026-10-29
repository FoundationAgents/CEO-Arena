"""Security and integration tests for browser-triggered Campaign control."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ceo_arena.cli import _is_loopback_host, build_parser
from ceo_arena.dashboard.app import _DashboardEventStream, create_dashboard_app
from ceo_arena.dashboard.catalog import RunDashboard
from ceo_arena.dashboard.control import ExperimentCatalog, StartReceipt
from ceo_arena.dashboard.models import RunControlReceipt, RunControlRequest
from ceo_arena.dashboard.run_control import RunControlError, RunControlErrorCode

_EXPERIMENT_REF = "a" * 32


class _FakeControl:
    """Small Adapter exposing only the CampaignControl Interface under test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.revision_value = "b" * 64

    def snapshot(self) -> ExperimentCatalog:
        return ExperimentCatalog.model_validate(
            {
                "revision": self.revision_value,
                "experiments": [
                    {
                        "experiment_ref": _EXPERIMENT_REF,
                        "campaign_id": "retail-arena",
                        "model_ids": ["model-a", "deterministic-rule-ceo"],
                        "status": "ready",
                        "action": "run",
                        "can_start": True,
                        "expected_runs": 5,
                        "completed_runs": 0,
                        "total_days": 500,
                        "seed_count": 1,
                        "company_count": 4,
                        "status_counts": {},
                        "message": "Experiment ready.",
                    }
                ],
            }
        )

    def start(self, experiment_ref: str, idempotency_key: str) -> StartReceipt:
        if experiment_ref == "c" * 32:
            raise RuntimeError(r"sk-private C:\secret\campaign.toml")
        if experiment_ref != _EXPERIMENT_REF:
            raise KeyError(experiment_ref)
        self.calls.append((experiment_ref, idempotency_key))
        return StartReceipt.model_validate(
            {
                "experiment_ref": experiment_ref,
                "campaign_id": "retail-arena",
                "disposition": "started",
                "status": "starting",
                "message": "Experiment dispatched.",
            }
        )

    def revision(self) -> str:
        return self.revision_value


class _FakeRunControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, RunControlRequest, str]] = []

    def control(
        self,
        run_ref: str,
        command: RunControlRequest,
        idempotency_key: str,
    ) -> RunControlReceipt:
        if run_ref == "e" * 16:
            raise RunControlError(
                RunControlErrorCode.REVISION_CONFLICT,
                "Run state changed. Refresh and retry.",
            )
        if run_ref != "d" * 16:
            raise RunControlError(
                RunControlErrorCode.NOT_FOUND,
                "No controllable campaign run found.",
            )
        self.calls.append((run_ref, command, idempotency_key))
        return RunControlReceipt(
            run_ref=run_ref,
            action=command.action,
            revision=command.revision + 1,
            status="stopped",
            control_action="resume",
            control_label="Resume",
            message="Run paused.",
        )


def test_control_api_starts_only_authorized_empty_json_requests(tmp_path: Path) -> None:
    control = _FakeControl()
    app = create_dashboard_app(tmp_path / "runs", control=control)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        catalog = client.get("/api/experiments")
        session = client.get("/api/control-session")
        token = session.json()["control_token"]
        headers = {
            "Origin": "http://127.0.0.1",
            "X-CEOArena-Control": token,
            "Idempotency-Key": "request-0001",
        }
        started = client.post(
            f"/api/experiments/{_EXPERIMENT_REF}/start",
            headers=headers,
            json={},
        )

        assert catalog.status_code == session.status_code == 200
        assert started.status_code == 202
        assert started.json()["disposition"] == "started"
        assert control.calls == [(_EXPERIMENT_REF, "request-0001")]
        serialized = catalog.text + session.text + started.text
        assert "sk-private" not in serialized
        assert str(tmp_path) not in serialized

        unsafe = client.post(
            f"/api/experiments/{_EXPERIMENT_REF}/start",
            headers={**headers, "Idempotency-Key": "request-0002"},
            json={"api_key": "sk-private", "spec_path": r"C:\secret\campaign.toml"},
        )
        assert unsafe.status_code == 422
        assert control.calls == [(_EXPERIMENT_REF, "request-0001")]
        assert "sk-private" not in unsafe.text
        assert "campaign.toml" not in unsafe.text


def test_control_api_rejects_csrf_bad_hosts_and_raw_errors(tmp_path: Path) -> None:
    control = _FakeControl()
    app = create_dashboard_app(tmp_path / "runs", control=control)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/control-session").json()["control_token"]
        path = f"/api/experiments/{_EXPERIMENT_REF}/start"
        base = {
            "X-CEOArena-Control": token,
            "Idempotency-Key": "request-0003",
        }

        assert client.post(path, headers=base, json={}).status_code == 403
        assert (
            client.post(
                path,
                headers={**base, "Origin": "https://attacker.example"},
                json={},
            ).status_code
            == 403
        )
        assert (
            client.post(
                path,
                headers={
                    **base,
                    "Origin": "http://127.0.0.1",
                    "X-CEOArena-Control": "wrong-token",
                },
                json={},
            ).status_code
            == 403
        )
        assert (
            client.post(
                path,
                headers={**base, "Origin": "http://127.0.0.1"},
                content="{}",
            ).status_code
            == 415
        )
        untrusted = client.get("/api/experiments", headers={"Host": "attacker.example"})
        assert untrusted.status_code == 400

        failed = client.post(
            f"/api/experiments/{'c' * 32}/start",
            headers={
                **base,
                "Origin": "http://127.0.0.1",
                "Idempotency-Key": "request-0004",
            },
            json={},
        )
        assert failed.status_code == 409
        assert "sk-private" not in failed.text
        assert "campaign.toml" not in failed.text


def test_run_control_api_requires_explicit_authorized_cas_command(tmp_path: Path) -> None:
    campaigns = _FakeControl()
    runs = _FakeRunControl()
    app = create_dashboard_app(tmp_path / "runs", control=campaigns, run_control=runs)  # type: ignore[arg-type]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/control-session").json()["control_token"]
        headers = {
            "Origin": "http://127.0.0.1",
            "X-CEOArena-Control": token,
            "Idempotency-Key": "run-control-001",
        }
        path = f"/api/runs/{'d' * 16}/control"

        response = client.post(
            path,
            headers=headers,
            json={"action": "pause", "revision": 4},
        )
        assert response.status_code == 202
        assert response.json()["control_action"] == "resume"
        assert runs.calls == [
            (
                "d" * 16,
                RunControlRequest(action="pause", revision=4),
                "run-control-001",
            )
        ]

        assert (
            client.post(
                path,
                headers={**headers, "Idempotency-Key": "run-control-002"},
                json={"action": "toggle", "revision": 4},
            ).status_code
            == 422
        )
        assert (
            client.post(
                path,
                headers={**headers, "Idempotency-Key": "run-control-003"},
                json={"action": "pause", "revision": 4, "api_key": "sk-private"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                path,
                headers={**headers, "Idempotency-Key": "run-control-004"},
                json={"action": "pause", "revision": -1},
            ).status_code
            == 422
        )
        assert client.post(path, json={"action": "pause", "revision": 4}).status_code == 403

        conflict = client.post(
            f"/api/runs/{'e' * 16}/control",
            headers={**headers, "Idempotency-Key": "run-control-005"},
            json={"action": "resume", "revision": 7},
        )
        assert conflict.status_code == 409
        assert "sk-private" not in conflict.text
        assert str(tmp_path) not in conflict.text


def test_read_only_dashboard_disables_control_and_combines_revisions(tmp_path: Path) -> None:
    with TestClient(create_dashboard_app(tmp_path / "readonly")) as client:
        catalog = client.get("/api/experiments")
        assert catalog.status_code == 200
        assert catalog.json()["experiments"] == []
        assert client.get("/api/control-session").status_code == 404

    control = _FakeControl()
    stream = _DashboardEventStream(RunDashboard(tmp_path / "runs"), control)  # type: ignore[arg-type]
    initial = stream._revision()
    control.revision_value = "c" * 64
    assert stream._revision() != initial


def test_dashboard_cli_defaults_to_local_control_and_has_read_only_escape_hatch() -> None:
    parser = build_parser()
    controlled = parser.parse_args(["dashboard"])
    read_only = parser.parse_args(["dashboard", "--no-campaign-control"])

    assert controlled.campaign_config_root == ".ceo-arena/campaigns"
    assert controlled.max_active_campaigns == 1
    assert not controlled.no_campaign_control
    assert read_only.no_campaign_control
    assert _is_loopback_host("127.0.0.1")
    assert not _is_loopback_host("::1")
    assert _is_loopback_host("localhost")
    assert not _is_loopback_host("0.0.0.0")


def test_static_control_is_one_click_and_keeps_full_model_lineups() -> None:
    static = Path(__file__).parents[2] / "src" / "ceo_arena" / "dashboard" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    styles = (static / "app.css").read_text(encoding="utf-8")

    assert 'id="runButton"' in html
    assert 'id="experimentSelect"' in html
    assert 'id="runSelect"' in html
    assert 'id="controlMessage"' in html
    assert "refreshButton" not in html + script
    assert 'model_ids.join(" + ")' in script
    assert "width: min(900px, 58vw)" in styles
    assert "confirm(" not in script
    assert "startExperiment(this.startingExperimentRef)" in script
    assert 'id="runControlButton"' in html
    assert "/api/runs/${encodeURIComponent(runRef)}/control" in script
    assert "body: JSON.stringify(payload)" in script
    assert "Pausing..." in script
    assert "Resuming..." in script
    assert "showStartError" in script
