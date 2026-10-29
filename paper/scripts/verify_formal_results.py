"""Recompute paper summaries from public company cases, without running the simulator."""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from statistics import fmean, pstdev
from typing import Literal

from pydantic import BaseModel, ConfigDict, NonNegativeInt, TypeAdapter

from ceo_arena.evaluation import ArenaAnalysis

RULE = "deterministic-rule-ceo"
SEEDS = (11, 29, 47)
INITIAL_VALUE_CENTS = 51_600_000
DecisionStatus = Literal["sealed", "budget_exhausted", "timeout", "protocol_error"]


class EvidenceModel(BaseModel):
    """Read the required typed fields while allowing unrelated trajectory evidence."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class Usage(EvidenceModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_turns: NonNegativeInt
    finalization_turns: NonNegativeInt
    tool_calls: NonNegativeInt
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    reasoning_tokens: NonNegativeInt
    cache_read_tokens: NonNegativeInt
    cache_write_tokens: NonNegativeInt
    latency_ms: NonNegativeInt

    def counters(self) -> dict[str, int]:
        """Expose all validated counters for consistent summation."""
        return {field: int(getattr(self, field)) for field in type(self).model_fields}

    @staticmethod
    def total(records: Iterable[Usage]) -> dict[str, int]:
        """Sum only the explicitly supplied usage records."""
        counters: Counter[str] = Counter()
        for record in records:
            counters.update(record.counters())
        return dict(counters)


class Run(EvidenceModel):
    run_id: str
    seed: int
    lineup: str
    days: int
    windows: int


class CompanyCase(EvidenceModel):
    run_id: str
    seed: int
    lineup: str
    company_id: str
    model_id: str
    status: Literal["active", "bankrupt"]
    bankruptcy_day: int | None
    official_score_cents: int
    terminal_cash_cents: int
    terminal_salvage_cents: NonNegativeInt
    decisions: NonNegativeInt
    status_counts: dict[DecisionStatus, NonNegativeInt]
    usage: Usage


class ModelSummary(EvidenceModel):
    case_count: int
    bankrupt_count: int
    bankruptcy_rate: float
    bankruptcy_days: tuple[int, ...]
    negative_score_count: int
    mean_score_cents: float
    mean_terminal_cash_cents: float
    mean_salvage_cents: float
    decisions: int
    usage_totals: Usage


class Archive(EvidenceModel):
    operation: str
    fresh_redo_index: int
    costs_retained: Usage


class ArchivedUsage(EvidenceModel):
    archive_count: int
    included_in_settled_usage_totals: bool
    is_complete_provider_bill: bool
    totals: Usage
    archives: tuple[Archive, ...]


class Trajectories(EvidenceModel):
    runs: tuple[Run, ...]
    cases: tuple[CompanyCase, ...]
    models: dict[str, ModelSummary]
    archived_fresh_usage: ArchivedUsage


class FormalResultsVerifier:
    """Derive descriptive statistics from cases and compare them with published summaries.

    ArenaAnalysis supplies only the result schema; its analyzer and scoring code are
    never invoked. This checks agreement within the supplied evidence, not checkpoint
    hashes, simulator transitions, causal identification, or complete provider billing.
    """

    def __init__(self, data: Path) -> None:
        self.analysis = ArenaAnalysis.model_validate_json(
            (data / "arena_analysis.json").read_bytes()
        )
        self.trajectories = Trajectories.model_validate_json(
            (data / "trajectory_summary.json").read_bytes()
        )
        self.rates = TypeAdapter(dict[str, float]).validate_json(
            (data / "bankruptcy_rates.json").read_bytes(), strict=True
        )
        self.cases = self.trajectories.cases
        self.models = {case.model_id for case in self.cases}
        self.rosters: dict[tuple[str, int], dict[str, CompanyCase]] = {}

    @staticmethod
    def equal(actual: object, expected: object, label: str) -> None:
        """Reject a mismatch even when Python assertions are disabled."""
        if actual != expected:
            raise ValueError(f"{label}: expected {expected!r}, got {actual!r}")

    @staticmethod
    def close(actual: float, expected: float, label: str) -> None:
        """Allow only negligible floating-point rounding in cent-valued summaries."""
        if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-5):
            raise ValueError(f"{label}: expected {expected!r}, got {actual!r}")

    def verify(self) -> None:
        """Validate the fixed formal campaign and print its scope."""
        self.verify_design()
        self.verify_performance()
        self.verify_contrasts()
        self.verify_usage()
        print(
            "PASS: 27 runs, 216 cases, 9 candidates, 24 cases per candidate; days/seats verified."
        )
        print("PASS: Scores, Track A/B, bankruptcy rates and final decision status counts agree.")
        print(
            "PASS: 24 paired contrasts and 168 directed values; 44/56 directed pairs change sign."
        )
        print(
            "PASS: 3 archives separately retain 28 replies, "
            "1,229,231 input / 154,651 output tokens."
        )
        print(
            "Scope: public evidence consistency; "
            "no simulator replay, model requests or billing audit."
        )

    def verify_design(self) -> None:
        """Check complete leave-one-out coverage, fixed seats and terminal accounting."""
        runs = self.trajectories.runs
        self.equal((len(runs), len(self.cases), len(self.models)), (27, 216, 9), "design size")
        self.equal(len({run.run_id for run in runs}), 27, "unique run IDs")
        self.equal({case.run_id for case in self.cases}, {run.run_id for run in runs}, "run IDs")
        self.equal(Counter(run.seed for run in runs), Counter(dict.fromkeys(SEEDS, 9)), "seeds")
        self.equal(len({run.lineup for run in runs}), 9, "lineups")
        self.equal(len({(run.lineup, run.seed) for run in runs}), 27, "lineup/seed coverage")
        self.equal(RULE in self.models, True, "Rule present")
        for summary in (
            self.analysis.track_a,
            self.analysis.track_b,
            self.analysis.health,
            self.trajectories.models,
            self.rates,
        ):
            self.equal(set(summary), self.models, "candidate coverage")
        self.equal(set(self.analysis.track_c), self.models - {RULE}, "Track C candidates")
        for run in runs:
            cases = [case for case in self.cases if case.run_id == run.run_id]
            roster = {case.model_id: case for case in cases}
            self.equal(
                (run.days, run.windows, len(cases), len(roster)), (500, 72, 8, 8), run.run_id
            )
            self.equal(
                {case.company_id for case in cases},
                {f"company-{i}" for i in range(1, 9)},
                f"{run.run_id} seats",
            )
            (omitted,) = self.models - set(roster)
            self.equal((omitted, run.seed) in self.rosters, False, "duplicate substitution")
            self.rosters[omitted, run.seed] = roster
            for case in cases:
                label = f"{case.run_id}/{case.company_id}"
                self.equal((case.seed, case.lineup), (run.seed, run.lineup), label)
                self.equal(
                    case.official_score_cents,
                    case.terminal_cash_cents + case.terminal_salvage_cents - INITIAL_VALUE_CENTS,
                    f"{label} Score",
                )
                bankrupt = case.status == "bankrupt"
                self.equal(case.terminal_cash_cents < 0, bankrupt, f"{label} cash/exit")
                self.equal(case.bankruptcy_day is not None, bankrupt, f"{label} exit day")
                if case.bankruptcy_day is not None:
                    self.equal(1 <= case.bankruptcy_day <= 500, True, f"{label} exit range")
                self.equal(sum(case.status_counts.values()), case.decisions, f"{label} decisions")
                expected_windows = math.ceil((case.bankruptcy_day or 500) / 7)
                self.equal(case.decisions, expected_windows, f"{label} active windows")
        self.equal(
            set(self.rosters),
            {(model, seed) for model in self.models for seed in SEEDS},
            "complete substitutions",
        )
        seats = {model: case.company_id for model, case in self.rosters[RULE, SEEDS[0]].items()}
        for (omitted, _seed), roster in self.rosters.items():
            for model, case in roster.items():
                self.equal(
                    case.company_id,
                    seats[omitted if model == RULE else model],
                    f"{case.run_id}/{model} fixed seat",
                )

    def verify_performance(self) -> None:
        """Recompute means, population dispersions, bankruptcy and completion counts."""
        total_status: Counter[str] = Counter()
        for model in sorted(self.models):
            cases = [case for case in self.cases if case.model_id == model]
            a, b = self.analysis.track_a[model], self.analysis.track_b[model]
            summary, health = self.trajectories.models[model], self.analysis.health[model]
            scores = [case.official_score_cents for case in cases]
            mean = fmean(scores)
            self.equal((len(cases), a.run_count, b.run_count, summary.case_count), (24,) * 4, model)
            for published in (a.mean_official_score_cents, b.mean_cents, summary.mean_score_cents):
                self.close(published, mean, f"{model} mean")
            self.close(
                summary.mean_terminal_cash_cents,
                fmean(c.terminal_cash_cents for c in cases),
                f"{model} mean cash",
            )
            self.close(
                summary.mean_salvage_cents,
                fmean(c.terminal_salvage_cents for c in cases),
                f"{model} mean salvage",
            )
            backgrounds: dict[tuple[str, ...], dict[int, int]] = {}
            for (_omitted, seed), roster in self.rosters.items():
                if model in roster:
                    key = tuple(sorted(set(roster) - {model}))
                    backgrounds.setdefault(key, {})[seed] = roster[model].official_score_cents
            self.equal(
                (len(backgrounds), b.background_count, len(b.backgrounds)),
                (8, 8, 8),
                f"{model} backgrounds",
            )
            self.equal({entry.model_ids for entry in b.backgrounds}, set(backgrounds), model)
            means, residuals = [], []
            for entry in b.backgrounds:
                raw = backgrounds[entry.model_ids]
                self.equal(tuple(sorted(raw)), SEEDS, f"{model} background seeds")
                values = tuple(raw[seed] for seed in SEEDS)
                self.equal(entry.seed_scores_cents, values, f"{model} background scores")
                background_mean = fmean(values)
                self.close(entry.mean_cents, background_mean, f"{model} background mean")
                means.append(background_mean)
                residuals.extend((value - background_mean) ** 2 for value in values)
            between, within = pstdev(means), math.sqrt(fmean(residuals))
            self.close(b.between_background_std_cents, between, f"{model} opponent SD")
            self.close(b.within_background_std_cents, within, f"{model} seed RMS")
            self.close(pstdev(scores) ** 2, between**2 + within**2, f"{model} variance identity")
            self.equal(within > between, True, f"{model} reported dispersion ordering")
            self.equal(sorted(b.scores_cents), sorted(scores), f"{model} score coverage")
            self.equal((b.minimum_cents, b.maximum_cents), (min(scores), max(scores)), model)
            days = sorted(c.bankruptcy_day for c in cases if c.bankruptcy_day is not None)
            self.equal(
                (summary.bankrupt_count, sorted(summary.bankruptcy_days)),
                (len(days), days),
                f"{model} bankruptcy count",
            )
            for rate in (summary.bankruptcy_rate, self.rates[model]):
                self.close(rate, len(days) / 24, f"{model} bankruptcy rate")
            self.equal(summary.negative_score_count, sum(score < 0 for score in scores), model)
            statuses: Counter[str] = Counter()
            for case in cases:
                statuses.update(case.status_counts)
            decisions = sum(c.decisions for c in cases)
            self.equal((health.decision_count, summary.decisions), (decisions, decisions), model)
            self.equal(health.status_counts, dict(statuses), f"{model} health counts")
            failures = decisions - statuses["sealed"]
            self.equal(health.failure_count, failures, f"{model} unsealed count")
            self.close(health.failure_rate, failures / decisions, f"{model} unsealed rate")
            total_status.update(statuses)
        self.equal(
            total_status,
            Counter(sealed=14362, budget_exhausted=683, timeout=32, protocol_error=1),
            "campaign decision counts",
        )
        self.equal(sum(c.status == "bankrupt" for c in self.cases), 16, "campaign bankruptcies")
        self.equal(sum(c.official_score_cents < 0 for c in self.cases), 145, "negative Scores")

    def verify_contrasts(self) -> None:
        """Subtract raw same-seat company results, including the seven induced rival differences."""
        signs: Counter[str] = Counter()
        for model, published in self.analysis.track_c.items():
            self.equal(
                (published.model_id, published.baseline_model_id, published.pair_count),
                (model, RULE, 3),
                f"{model} comparison identity",
            )
            self.equal(sorted(c.seed for c in published.seed_comparisons), list(SEEDS), model)
            private, others, system = [], [], []
            directed: dict[str, list[int]] = {rival: [] for rival in self.models - {model, RULE}}
            for comparison in published.seed_comparisons:
                reference = self.rosters[RULE, comparison.seed]
                counterfactual = self.rosters[model, comparison.seed]
                own = (
                    reference[model].official_score_cents
                    - counterfactual[RULE].official_score_cents
                )
                effects = {
                    rival: reference[rival].official_score_cents
                    - counterfactual[rival].official_score_cents
                    for rival in directed
                }
                total = sum(c.official_score_cents for c in reference.values()) - sum(
                    c.official_score_cents for c in counterfactual.values()
                )
                self.equal(comparison.target_company_id, reference[model].company_id, model)
                self.equal(comparison.background_model_ids, tuple(sorted(directed)), model)
                self.equal(comparison.directed_effects_cents, effects, f"{model} directed effects")
                self.equal(
                    (
                        comparison.private_difference_cents,
                        comparison.others_difference_cents,
                        comparison.system_difference_cents,
                    ),
                    (own, sum(effects.values()), total),
                    f"{model}/seed-{comparison.seed} contrasts",
                )
                self.equal(total, own + sum(effects.values()), f"{model} decomposition")
                private.append(own)
                others.append(sum(effects.values()))
                system.append(total)
                for rival, value in effects.items():
                    directed[rival].append(value)
            for name, values in (("private", private), ("others", others), ("system", system)):
                self.close(
                    getattr(published, f"mean_{name}_difference_cents"),
                    fmean(values),
                    f"{model} mean {name}",
                )
            self.equal(
                published.positive_system_seed_count, sum(value > 0 for value in system), model
            )
            self.equal(set(published.mean_directed_effects_cents), set(directed), model)
            for rival, values in directed.items():
                self.close(
                    published.mean_directed_effects_cents[rival],
                    fmean(values),
                    f"{model}->{rival} mean",
                )
                positive, negative = any(v > 0 for v in values), any(v < 0 for v in values)
                signs[
                    "flips" if positive and negative else "positive" if positive else "negative"
                ] += 1
                self.equal(any(value == 0 for value in values), False, "no zero directed contrasts")
        self.equal(signs, Counter(flips=44, positive=4, negative=8), "directed sign counts")
        totals = [
            sum(c.official_score_cents for c in self.rosters[RULE, s].values()) for s in SEEDS
        ]
        self.equal(all(value < 0 for value in totals), True, "all-LLM market losses")
        self.equal(round(fmean(totals) / 100, 2), -988835.06, "all-LLM mean dollars")

    def verify_usage(self) -> None:
        """Keep final settled counters and discarded-attempt counters separate."""
        for model, summary in self.trajectories.models.items():
            counters = Usage.total(case.usage for case in self.cases if case.model_id == model)
            self.equal(summary.usage_totals.counters(), counters, f"{model} settled usage")
        archived = self.trajectories.archived_fresh_usage
        self.equal((archived.archive_count, len(archived.archives)), (3, 3), "archive count")
        self.equal(len({item.operation for item in archived.archives}), 3, "unique archives")
        self.equal(archived.included_in_settled_usage_totals, False, "archive exclusion")
        self.equal(archived.is_complete_provider_bill, False, "billing scope")
        for archive in archived.archives:
            self.equal(archive.fresh_redo_index, 1, "fresh attempt index")
        counters = Usage.total(archive.costs_retained for archive in archived.archives)
        self.equal(archived.totals.counters(), counters, "archived usage sum")
        self.equal(
            (counters["provider_turns"], counters["input_tokens"], counters["output_tokens"]),
            (28, 1_229_231, 154_651),
            "reported archived extras",
        )


def main() -> int:
    """Read only the public evidence directory, defaulting relative to this script."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data/formal",
        help="Directory containing arena_analysis, trajectory_summary and bankruptcy_rates JSONs",
    )
    arguments = parser.parse_args()
    try:
        FormalResultsVerifier(arguments.data).verify()
    except (OSError, ValueError, KeyError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
