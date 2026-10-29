import json
from pathlib import Path

import pytest

from ceo_arena.cli import _artifact_fingerprints, main
from ceo_arena.config import default_scenario
from ceo_arena.inference import resolve_inference


def test_artifact_fingerprints_are_available() -> None:
    fingerprints = _artifact_fingerprints()

    assert isinstance(fingerprints, dict)
    assert fingerprints["opencode_commit"]
    assert not any(
        path.startswith("src/ceo_arena/dashboard/")
        or path in {"src/ceo_arena/cli.py", "src/ceo_arena/campaign/application.py"}
        for path in fingerprints
    )


def test_rule_lineup_cli_is_replayable(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"

    run_status = main(
        [
            "run-lineup",
            "--participant",
            "rule",
            "--participant",
            "rule",
            "--days",
            "2",
            "--output",
            str(run_directory),
            "--fresh",
        ]
    )
    replay_status = main(["replay", str(run_directory)])

    assert run_status == 0
    assert replay_status == 0
    assert (run_directory / "summary.json").is_file()
    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    assert all(
        set(assignment) == {"company_id", "model_id", "harness_id", "api_index", "inference"}
        and assignment["inference"] is None
        for assignment in manifest["config"]["assignments"]
    )


def test_scenario_duration_is_preserved_and_invalid_override_is_rejected(
    tmp_path: Path,
) -> None:
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(default_scenario(total_days=3).model_dump_json(), encoding="utf-8")
    output = tmp_path / "duration-run"

    status = main(
        [
            "run-rule",
            "--companies",
            "2",
            "--scenario",
            str(scenario_path),
            "--output",
            str(output),
        ]
    )
    invalid = main(
        [
            "run-rule",
            "--companies",
            "2",
            "--scenario",
            str(scenario_path),
            "--days",
            "-1",
            "--output",
            str(tmp_path / "invalid"),
        ]
    )

    assert status == 0
    assert json.loads((output / "summary.json").read_text(encoding="utf-8"))["day"] == 3
    assert invalid == 2


def test_probe_newapi_requires_backend_credentials(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CEO_ARENA_NEWAPI_BASE_URL", raising=False)
    monkeypatch.delenv("CEO_ARENA_NEWAPI_API_KEY", raising=False)

    status = main(["probe-newapi", "--model", "model-a"])

    assert status == 2
    assert "requires CEO_ARENA_NEWAPI_BASE_URL" in capsys.readouterr().err


def test_campaign_plan_cli_reads_multi_key_toml_without_printing_keys(
    tmp_path: Path,
    capsys,
) -> None:
    spec_path = tmp_path / "experiment.toml"
    spec_path.write_text(
        """
schema_version = 1
campaign_id = "cli-campaign"
base_url = "https://newapi.example/v1"
company_count = 2
days = 3
seeds = [11]

[[api]]
api_key = "first-secret"
models = ["model-a"]

[[api]]
api_key = "second-secret"
models = ["model-b"]
""".strip(),
        encoding="utf-8",
    )

    status = main(["campaign", "plan", "--spec", str(spec_path)])

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert status == 0
    assert payload["estimate"]["lineup_count"] == 3
    assert payload["estimate"]["run_count"] == 3
    assert "first-secret" not in output
    assert "second-secret" not in output
    assert payload["routes"] == [
        {
            "model_id": model,
            "api_index": index,
            "inference": resolve_inference(model).model_dump(mode="json"),
        }
        for index, model in enumerate(("model-a", "model-b"))
    ]


def test_invalid_campaign_spec_never_echoes_raw_keys(tmp_path: Path, capsys) -> None:
    spec_path = tmp_path / "invalid.toml"
    spec_path.write_text(
        """
schema_version = 1
campaign_id = "invalid"
base_url = "https://newapi.example/v1"
company_count = 1
days = 1
seeds = [1]

[[api]]
api_key = "short-secret"
models = ["model-a"]

[[api]]
api_key = "short-secret"
models = ["model-b"]
""".strip(),
        encoding="utf-8",
    )

    status = main(["campaign", "plan", "--spec", str(spec_path)])

    error = capsys.readouterr().err
    assert status == 2
    assert "short-secret" not in error
    assert "distinct keys" in error


@pytest.mark.parametrize("command", ("arena-plan", "arena-run"))
def test_retired_batch_commands_are_not_exposed(command: str) -> None:
    with pytest.raises(SystemExit) as error:
        main([command])
    assert error.value.code == 2


def test_legacy_import_is_not_exposed() -> None:
    with pytest.raises(SystemExit) as error:
        main(["campaign", "import-legacy"])
    assert error.value.code == 2
