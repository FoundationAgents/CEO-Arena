"""Upgrade an existing Campaign to bounded HTTP400 retries without replaying model calls."""

import argparse
from pathlib import Path

from ceo_arena.campaign.http400_migration import (
    Http400RetryMigration,
    Http400ThreeAttemptMigration,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-old-plan-hash")
    parser.add_argument("--max-attempts", type=int, choices=(3, 10), default=3)
    args = parser.parse_args()
    migration = Http400ThreeAttemptMigration if args.max_attempts == 3 else Http400RetryMigration
    receipt = migration(args.output).run(
        apply=args.apply,
        expected_old_plan_hash=args.expected_old_plan_hash,
    )
    print(receipt.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
