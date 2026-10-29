"""Command-line entry points for scenarios, deterministic smoke runs, and replay."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import uvicorn
from pydantic import SecretStr

from ceo_arena.campaign import (
    CampaignApplication,
    CampaignCompiler,
    ExperimentSpec,
    FrozenCampaignPlan,
    campaign_report,
    load_experiment_spec,
)
from ceo_arena.config import default_scenario, load_scenario, write_default_scenario
from ceo_arena.dashboard import create_dashboard_app
from ceo_arena.economy import MarketWorld
from ceo_arena.execution import (
    execute_run as _execute_run,
)
from ceo_arena.harness import (
    RULE_CEO_HARNESS_ID,
    NewApiProbe,
    OpenCodeHarness,
    OpenCodeHarnessConfig,
    RuleCeoHarness,
)
from ceo_arena.harness.bun import BUN_VERSION, project_bun_path
from ceo_arena.harness.opencode import validate_newapi_base_url
from ceo_arena.hashing import provider_endpoint_fingerprint
from ceo_arena.models import ScenarioConfig
from ceo_arena.protocol import RULE_MODEL_ID
from ceo_arena.provenance import simulation_fingerprints
from ceo_arena.runtime import (
    AgentAssignment,
    AgentHarness,
    ExactReplay,
    RunConfig,
    RunManifest,
    RunStore,
)
from ceo_arena.runtime._redact import redact_sensitive_text


class _IdentifiedRuleCeoHarness(RuleCeoHarness):
    """Expose distinct company identities for deterministic smoke runs."""

    def __init__(self, model_id: str) -> None:
        super().__init__()
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id


def _init(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    if output.exists() and not args.force:
        raise FileExistsError(f"scenario already exists: {output}; pass --force to replace it")
    write_default_scenario(output, seed=args.seed, total_days=args.days)
    print(output)
    return 0


def _setup_worker(args: argparse.Namespace) -> int:
    del args
    root = Path(__file__).resolve().parents[2]
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm is required to install the pinned Bun worker runtime")
    prefix = root / "agent_worker" / ".tools"
    subprocess.run(
        (
            npm,
            "install",
            "--prefix",
            str(prefix),
            "--no-save",
            f"bun@{BUN_VERSION}",
        ),
        check=True,
        cwd=root,
    )
    bun = project_bun_path(root)
    if not bun.is_file():
        raise RuntimeError("pinned Bun executable was not installed")
    completed = subprocess.run(
        (str(bun), "--version"),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.stdout.strip() != BUN_VERSION:
        raise RuntimeError("installed Bun runtime failed its version check")
    print(bun)
    return 0


def _dashboard(args: argparse.Namespace) -> int:
    runs_root = Path(args.runs_root).resolve()
    runs_root.mkdir(parents=True, exist_ok=True)
    control_enabled = not args.no_campaign_control
    if control_enabled and not _is_loopback_host(args.host):
        raise ValueError("Campaign control requires a loopback dashboard host")
    campaign_config_root = None
    if control_enabled:
        campaign_config_root = Path(args.campaign_config_root).resolve()
        campaign_config_root.mkdir(parents=True, exist_ok=True)
    app = create_dashboard_app(
        runs_root,
        campaign_config_root=campaign_config_root,
        max_active_campaigns=args.max_active_campaigns,
    )
    print(f"CEO Arena dashboard: http://{args.host}:{args.port}")
    print(f"Watching run artifacts: {runs_root}")
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


def _is_loopback_host(host: str) -> bool:
    """Return whether a Dashboard bind target is an explicit local address."""

    return host.casefold() in {"127.0.0.1", "localhost"}


def _rule_assignments(company_count: int) -> tuple[AgentAssignment, ...]:
    return tuple(
        AgentAssignment(
            company_id=f"company-{index + 1}",
            model_id=f"{RULE_MODEL_ID}@company-{index + 1}",
            harness_id=RULE_CEO_HARNESS_ID,
        )
        for index in range(company_count)
    )


def _artifact_fingerprints() -> dict[str, str]:
    return simulation_fingerprints()


def _scenario_from_args(args: argparse.Namespace) -> ScenarioConfig:
    if not args.scenario:
        days = args.days if args.days is not None else args.default_days
        return default_scenario(seed=args.seed, total_days=days)
    scenario = load_scenario(Path(args.scenario).resolve())
    return _with_total_days(scenario, args.days) if args.days is not None else scenario


def _with_total_days(scenario: ScenarioConfig, total_days: int) -> ScenarioConfig:
    payload = scenario.model_dump(mode="json")
    payload["total_days"] = total_days
    return ScenarioConfig.model_validate(payload)


async def _run_rule_async(args: argparse.Namespace) -> int:
    scenario = _scenario_from_args(args)
    output = Path(args.output).resolve()
    assignments = _rule_assignments(args.companies)
    run_id = args.run_id or output.name
    run_config = RunConfig(
        run_id=run_id,
        scenario=scenario,
        assignments=assignments,
        artifact_fingerprints=_artifact_fingerprints(),
    )
    harnesses = {
        assignment.company_id: _IdentifiedRuleCeoHarness(assignment.model_id)
        for assignment in assignments
    }
    await _execute_run(run_config, harnesses, output, fresh=args.fresh)
    return 0


def _run_rule(args: argparse.Namespace) -> int:
    return asyncio.run(_run_rule_async(args))


def _parse_model_id(value: str) -> str:
    if not value or value != value.strip() or any(character.isspace() for character in value):
        raise argparse.ArgumentTypeError("model id must be non-empty and contain no whitespace")
    if "=" in value:
        raise argparse.ArgumentTypeError("model aliases are not supported; pass MODEL_ID directly")
    return value


async def _run_lineup_async(args: argparse.Namespace) -> int:
    scenario = _scenario_from_args(args)
    output = Path(args.output).resolve()
    base_url = args.base_url or os.environ.get("CEO_ARENA_NEWAPI_BASE_URL", "")
    api_key = os.environ.get("CEO_ARENA_NEWAPI_API_KEY", "")
    assignments: list[AgentAssignment] = []
    harnesses: dict[str, AgentHarness] = {}
    for index, requested_model_id in enumerate(args.participant):
        company_id = f"company-{index + 1}"
        if requested_model_id.casefold() == "rule":
            model_id = f"{RULE_MODEL_ID}@{company_id}"
            harness = _IdentifiedRuleCeoHarness(model_id)
            api_index = None
        else:
            if not base_url or not api_key:
                raise ValueError(
                    "model participants require CEO_ARENA_NEWAPI_BASE_URL and "
                    "CEO_ARENA_NEWAPI_API_KEY"
                )
            harness = OpenCodeHarness(
                OpenCodeHarnessConfig(
                    base_url=base_url,
                    model=requested_model_id,
                    api_key=SecretStr(api_key),
                    trace_directory=output / "agent-traces",
                )
            )
            model_id = requested_model_id
            api_index = 0
        assignments.append(
            AgentAssignment(
                company_id=company_id,
                model_id=model_id,
                harness_id=harness.harness_id,
                api_index=api_index,
            )
        )
        harnesses[company_id] = harness
    run_config = RunConfig(
        run_id=args.run_id or output.name,
        scenario=scenario,
        assignments=tuple(assignments),
        provider_endpoint_sha256=(
            provider_endpoint_fingerprint(base_url)
            if any(item.harness_id.startswith("opencode-native-") for item in assignments)
            else None
        ),
        artifact_fingerprints=_artifact_fingerprints(),
    )
    await _execute_run(run_config, harnesses, output, fresh=args.fresh)
    return 0


def _run_lineup(args: argparse.Namespace) -> int:
    return asyncio.run(_run_lineup_async(args))


async def _probe_newapi_async(args: argparse.Namespace) -> int:
    base_url = args.base_url or os.environ.get("CEO_ARENA_NEWAPI_BASE_URL", "")
    api_key = os.environ.get("CEO_ARENA_NEWAPI_API_KEY", "")
    if not base_url or not api_key:
        raise ValueError("probe requires CEO_ARENA_NEWAPI_BASE_URL and CEO_ARENA_NEWAPI_API_KEY")
    base_url = validate_newapi_base_url(base_url)
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=args.timeout,
    ) as client:
        result = await NewApiProbe(client, base_url).probe(tuple(args.model))
    print(result.model_dump_json(indent=2))
    return 3 if result.missing_models else 0


def _probe_newapi(args: argparse.Namespace) -> int:
    return asyncio.run(_probe_newapi_async(args))


def _replay(args: argparse.Namespace) -> int:
    run_directory = Path(args.run_directory).resolve()
    manifest = RunManifest.model_validate_json(
        (run_directory / "manifest.json").read_text(encoding="utf-8")
    )
    store = RunStore(run_directory)
    with store.lock:
        store.refresh_after_lock()
        checkpoint = store.load_checkpoint(
            run_id=manifest.run_id,
            config_hash=manifest.config_hash,
        )
        if checkpoint is None:
            raise FileNotFoundError(f"checkpoint not found in {run_directory}")
        report = ExactReplay(
            MarketWorld(manifest.config.scenario),
            artifact_directory=run_directory / "agent-traces",
            runtime_trace_path=run_directory / "trace.jsonl",
        ).verify(checkpoint)
    print(report.model_dump_json(indent=2))
    return 0


def _compile_campaign(args: argparse.Namespace) -> tuple[ExperimentSpec, FrozenCampaignPlan]:
    spec = load_experiment_spec(Path(args.spec))
    plan = CampaignCompiler().compile(
        spec,
        simulation_fingerprints=_artifact_fingerprints(),
    )
    return spec, plan


def _campaign_plan(args: argparse.Namespace) -> int:
    _spec, plan = _compile_campaign(args)
    text = plan.model_dump_json(indent=2)
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"{text}\n", encoding="utf-8")
        print(output)
    else:
        print(text)
    return 0


async def _campaign_run_async(args: argparse.Namespace) -> int:
    spec, plan = _compile_campaign(args)
    application = CampaignApplication(spec, plan, Path(args.output))
    report = await application.run()
    print(report.model_dump_json(indent=2))
    return 0 if report.is_complete else 3


async def _campaign_resume_async(args: argparse.Namespace) -> int:
    spec, plan = _compile_campaign(args)
    application = CampaignApplication(spec, plan, Path(args.output))
    report = await application.resume()
    print(report.model_dump_json(indent=2))
    return 0 if report.is_complete else 3


def _campaign_run(args: argparse.Namespace) -> int:
    return asyncio.run(_campaign_run_async(args))


def _campaign_resume(args: argparse.Namespace) -> int:
    return asyncio.run(_campaign_resume_async(args))


def _campaign_status(args: argparse.Namespace) -> int:
    report = campaign_report(Path(args.output))
    print(report.model_dump_json(indent=2))
    return 0 if report.is_complete else 3


def build_parser() -> argparse.ArgumentParser:
    """Build the stable public CLI parser."""
    parser = argparse.ArgumentParser(prog="ceo-arena")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="write the public reference scenario")
    init.add_argument("--output", required=True)
    init.add_argument("--seed", type=int, default=42)
    init.add_argument("--days", type=int, default=500)
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=_init)

    setup_worker = commands.add_parser(
        "setup-worker", help="install and verify the pinned direct Bun runtime"
    )
    setup_worker.set_defaults(handler=_setup_worker)

    dashboard = commands.add_parser("dashboard", help="serve the local live run dashboard")
    dashboard.add_argument("--runs-root", default="runs")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8766)
    dashboard.add_argument(
        "--campaign-config-root",
        default=".ceo-arena/campaigns",
        help="directory containing browser-runnable TOML Campaign specs",
    )
    dashboard.add_argument(
        "--max-active-campaigns",
        type=int,
        default=1,
        help="maximum Campaign processes launched by this dashboard",
    )
    dashboard.add_argument(
        "--no-campaign-control",
        action="store_true",
        help="serve the dashboard read-only and allow non-loopback binding",
    )
    dashboard.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug"),
        default="warning",
    )
    dashboard.set_defaults(handler=_dashboard)

    run_rule = commands.add_parser("run-rule", help="run deterministic rule CEOs")
    run_rule.add_argument("--companies", type=int, default=4)
    run_rule.add_argument("--days", type=int)
    run_rule.add_argument("--seed", type=int, default=42)
    run_rule.add_argument("--scenario")
    run_rule.add_argument("--output", required=True)
    run_rule.add_argument("--run-id")
    run_rule.add_argument("--fresh", action="store_true", help="reject an existing run")
    run_rule.set_defaults(default_days=28, handler=_run_rule)

    run_lineup = commands.add_parser(
        "run-lineup", help="run one mixed OpenCode/NewAPI and rule-CEO lineup"
    )
    run_lineup.add_argument(
        "--participant",
        type=_parse_model_id,
        action="append",
        required=True,
        help="MODEL_ID; use rule for the deterministic baseline",
    )
    run_lineup.add_argument("--days", type=int)
    run_lineup.add_argument("--seed", type=int, default=42)
    run_lineup.add_argument("--scenario")
    run_lineup.add_argument("--base-url")
    run_lineup.add_argument("--output", required=True)
    run_lineup.add_argument("--run-id")
    run_lineup.add_argument("--fresh", action="store_true", help="reject an existing run")
    run_lineup.set_defaults(default_days=500, handler=_run_lineup)

    replay = commands.add_parser("replay", help="verify an episode without calling a model")
    replay.add_argument("run_directory")
    replay.set_defaults(handler=_replay)

    probe = commands.add_parser("probe-newapi", help="validate the secret-safe model catalog")
    probe.add_argument("--base-url")
    probe.add_argument("--model", action="append", default=[])
    probe.add_argument("--timeout", type=float, default=30.0)
    probe.set_defaults(handler=_probe_newapi)

    campaign = commands.add_parser(
        "campaign",
        help="plan, run, resume, or inspect a persistent multi-key Campaign",
    )
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)

    campaign_plan = campaign_commands.add_parser(
        "plan",
        help="compile the credential-bearing TOML into a secret-free run matrix",
    )
    campaign_plan.add_argument("--spec", required=True)
    campaign_plan.add_argument("--output")
    campaign_plan.set_defaults(handler=_campaign_plan)

    for command in ("run", "resume"):
        campaign_run = campaign_commands.add_parser(
            command,
            help=(
                "explicitly requeue stopped Runs and continue the Campaign"
                if command == "resume"
                else "start or auto-resume every eligible Seed wave"
            ),
        )
        campaign_run.add_argument("--spec", required=True)
        campaign_run.add_argument("--output", required=True)
        campaign_run.set_defaults(
            handler=_campaign_resume if command == "resume" else _campaign_run
        )

    campaign_status = campaign_commands.add_parser(
        "status",
        help="inspect durable progress without loading API credentials",
    )
    campaign_status.add_argument("--output", required=True)
    campaign_status.set_defaults(handler=_campaign_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute the public CLI and report concise operational errors."""
    try:
        args = build_parser().parse_args(argv)
        if getattr(args, "companies", 1) < 1:
            raise ValueError("--companies must be positive")
        return int(args.handler(args))
    except (
        FileExistsError,
        FileNotFoundError,
        httpx.HTTPError,
        RuntimeError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        message = redact_sensitive_text(str(exc)) or type(exc).__name__
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
