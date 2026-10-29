"""Dry-run or explicitly apply the offline Harness recovery checkpoint migration."""

from __future__ import annotations

import argparse
from pathlib import Path

from ceo_arena.campaign.recovery_migration import RecoveryPatchMigration


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path, help="Stopped Campaign output directory")
    parser.add_argument("--source-root", type=Path, help="Updated release root to fingerprint")
    parser.add_argument("--apply", action="store_true", help="Publish the reviewed migration")
    parser.add_argument("--expected-old-plan-hash", help="Exact old plan hash from the dry run")
    arguments = parser.parse_args()
    receipt = RecoveryPatchMigration(arguments.campaign, arguments.source_root).run(
        apply=arguments.apply, expected_old_plan_hash=arguments.expected_old_plan_hash
    )
    print(receipt.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
