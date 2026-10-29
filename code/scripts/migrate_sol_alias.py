"""Offline, explicitly authorized Sol response-label checkpoint migration."""

from __future__ import annotations

import argparse
from pathlib import Path

from ceo_arena.campaign.sol_alias_migration import SolAliasMigration
from ceo_arena.campaign.spec import load_experiment_spec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--old-spec", type=Path, required=True)
    parser.add_argument("--new-spec", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-old-plan-hash")
    arguments = parser.parse_args()
    receipt = SolAliasMigration(
        arguments.campaign,
        load_experiment_spec(arguments.old_spec),
        load_experiment_spec(arguments.new_spec),
        arguments.source_root,
    ).run(apply=arguments.apply, expected_old_plan_hash=arguments.expected_old_plan_hash)
    print(receipt.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
