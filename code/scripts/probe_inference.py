"""Prepare native model requests offline, or explicitly authorize bounded multi-turn probes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ceo_arena.campaign.spec import load_experiment_spec
from ceo_arena.inference import InferenceSettings
from ceo_arena.runtime._atomic import AtomicModelFile


class IdentityObservation(BaseModel):
    requested_model: str
    served_model: str | None
    expected_model_ids: tuple[str, ...]
    echoed_effort: str | None
    protocol: str
    status: str


class ProbeTurn(BaseModel):
    index: int
    elapsed_seconds: float
    request_body_sha256: str
    request_fields: tuple[str, ...]
    finish_reason: str | None
    tool_names: tuple[str, ...]
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None


class ProbeFailure(BaseModel):
    category: str
    reason: str | None
    http_status: int | None


class ProbeResult(BaseModel):
    model: str
    inference: InferenceSettings
    status: Literal["prepared", "passed", "provider_error", "incomplete", "unexpected_tools"]
    valid_seal: bool
    issued_requests: int
    completed_rounds: int
    elapsed_seconds: float
    identities: tuple[IdentityObservation, ...]
    turns: tuple[ProbeTurn, ...]
    failure: ProbeFailure | None


class ProbeReport(BaseModel):
    created_at: datetime
    paid: bool
    max_requests_per_model: int = 3
    max_output_tokens_per_request: int = 2048
    timeout_seconds_per_request: int = 60
    max_parallel_models: int = 3
    results: tuple[ProbeResult, ...]

    @property
    def has_failures(self) -> bool:
        return self.paid and any(result.status != "passed" for result in self.results)


def main() -> None:
    """Keep keys on stdin and publish only typed, secret-free probe observations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--paid", action="store_true", help="Authorize at most three tiny requests per model"
    )
    args = parser.parse_args()
    spec = load_experiment_spec(args.config)
    root = Path(__file__).resolve().parent.parent
    local_bun = (
        root
        / "agent_worker/.tools/node_modules/bun/bin"
        / ("bun.exe" if os.name == "nt" else "bun")
    )
    bun = str(local_bun) if local_bun.is_file() else shutil.which("bun")
    if bun is None:
        raise SystemExit("Bun is unavailable; install the pinned worker runtime first.")
    entries = {model: api for api in spec.api for model in api.models}
    payload = json.dumps(
        {
            "paid": args.paid,
            "routes": [
                {
                    "model": model,
                    "apiKey": entries[model].api_key.get_secret_value(),
                    "baseURL": spec.base_url,
                    "inference": spec.inference[model].model_dump(mode="json"),
                }
                for model in spec.model_ids
            ],
        }
    )
    with subprocess.Popen(
        [bun, "run", "tests/probe_inference.ts"],
        cwd=root / "agent_worker",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(payload)
        process.stdin.close()
        results: tuple[ProbeResult, ...] | None = None
        for line in process.stdout:
            event = json.loads(line)
            if event.get("event") == "report":
                results = tuple(ProbeResult.model_validate(result) for result in event["results"])
            else:
                print(json.dumps(event, ensure_ascii=False), flush=True)
        if process.wait() != 0 or results is None:
            raise SystemExit("Probe worker failed; credentials and raw responses were not logged.")
    report = ProbeReport(created_at=datetime.now(UTC), paid=args.paid, results=results)
    AtomicModelFile(args.output, ProbeReport).save(report)
    print(
        json.dumps(
            {
                "event": "report_saved",
                "model_count": len(results),
                "output": str(args.output),
                "has_failures": report.has_failures,
            }
        ),
        flush=True,
    )
    if report.has_failures:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
