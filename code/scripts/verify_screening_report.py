"""Reverify a finished screening from frozen code and saved artifacts; never call a model."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from ceo_arena.economy import MarketWorld
from ceo_arena.runtime import ExactReplay, RunCheckpoint
from ceo_arena.runtime._atomic import AtomicModelFile
from ceo_arena.runtime.replay import ReplayReport
from ceo_arena.screening import CandidateTiming, ScreeningPlan, ScreeningReport


class ScreeningVerifier:
    """Correct an artifact-root verification failure without repeating paid decisions."""

    def __init__(self, output: Path) -> None:
        self.output = output

    def run(self) -> ScreeningReport:
        plan = ScreeningPlan.model_validate_json((self.output / "screening_plan.json").read_bytes())
        candidates = []
        for case in plan.cases:
            directory = self.output / case.model_id
            timing = directory / "timing.json"
            raw = json.loads(timing.read_text(encoding="utf-8"))
            for field in CandidateTiming.model_computed_fields:
                raw.pop(field, None)
            result = CandidateTiming.model_validate(raw)
            checkpoint = RunCheckpoint.model_validate_json(
                (directory / "run/checkpoint.json").read_bytes()
            )
            if (
                checkpoint.current_state.day != plan.stop_after_day
                or len(checkpoint.completed_windows) != 4
            ):
                raise ValueError(f"{case.model_id}: four-week screening is not finished")
            if result.error not in {None, "ReplayMismatchError"}:
                raise ValueError(f"{case.model_id}: cannot clear unrelated error {result.error}")
            replay = ExactReplay(
                MarketWorld(case.config.scenario),
                artifact_directory=directory / "run/agent-traces",
            ).verify_prefix(checkpoint)
            AtomicModelFile(directory / "replay.json", ReplayReport).save(replay)
            backup = directory / "timing.initial.json"
            if not backup.exists():
                shutil.copy2(timing, backup)
            result = result.model_copy(update={"replay_verified": True, "error": None})
            AtomicModelFile(timing, CandidateTiming).save(result)
            candidates.append(result)
        report = ScreeningReport(candidates=tuple(candidates))
        original = self.output / "report.initial.json"
        if (self.output / "report.json").exists() and not original.exists():
            shutil.copy2(self.output / "report.json", original)
        AtomicModelFile(self.output / "report.json", ScreeningReport).save(report)
        note = (
            "\nVerification note: the initial verifier omitted the agent-traces directory. "
            "This report was reverified from the original frozen engine, saved checkpoints "
            "and trace hashes with the correct root. No additional model calls were made "
            "and no experiment trajectories were changed; initial timing receipts are "
            "retained as timing.initial.json.\n"
        )
        (self.output / "report.md").write_text(report.markdown() + note, encoding="utf-8")
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(ScreeningVerifier(args.output).run().markdown())


if __name__ == "__main__":
    main()
