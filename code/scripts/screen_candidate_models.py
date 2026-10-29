"""Run the approved three-model speed screen without modifying the formal model panel."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ceo_arena.campaign.spec import load_experiment_spec
from ceo_arena.screening import SpeedScreening


def main() -> None:
    """Keep credentials external and require explicit authorization for paid inference."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="Authorize paid model inference")
    args = parser.parse_args()
    credentials = load_experiment_spec(args.credentials)
    plan = SpeedScreening.plan_from(
        args.reference,
        credentials,
        ("qwen3.8-flash", "deepseek-v4-flash", "glm-5.3-flash"),
        api_index=2,
    )
    experiment = SpeedScreening(plan, credentials, args.output)
    report = asyncio.run(experiment.run(paid=args.run))
    print(report.markdown() if report else "Catalog verified; no inference requested.", flush=True)


if __name__ == "__main__":
    main()
