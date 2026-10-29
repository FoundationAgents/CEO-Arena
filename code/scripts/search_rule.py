"""Run the predeclared model-free fixed-Rule search; never modify the installed Rule."""

from __future__ import annotations

import argparse
from pathlib import Path

from ceo_arena.evaluation.rule_search import FixedRuleSearch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = FixedRuleSearch(root, args.output).run(workers=args.workers)
    print(
        f"Completed {len(report.development) + len(report.validation)} worlds; "
        f"winner={report.selection.selected.candidate_id}; model API calls=0"
    )


if __name__ == "__main__":
    main()
