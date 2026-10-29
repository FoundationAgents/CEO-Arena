"""Recompute main-evaluation Scores with terminal inventory salvage set to zero."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import fmean

RULE_MODEL_ID = "deterministic-rule-ceo"
EXPECTED_INITIAL_VALUE_CENTS = 51_600_000
CENTS_PER_USD_THOUSAND = 100_000


@dataclass(frozen=True, slots=True)
class Outcome:
    """One completed company trajectory."""

    run_id: str
    seed: int
    company_id: str
    model_id: str
    official_score_cents: int
    terminal_cash_cents: int
    terminal_salvage_cents: int

    @classmethod
    def from_record(cls, record: dict[str, object]) -> Outcome:
        """Read the fields needed for terminal revaluation."""
        return cls(
            run_id=str(record["run_id"]),
            seed=int(record["seed"]),
            company_id=str(record["company_id"]),
            model_id=str(record["model_id"]),
            official_score_cents=int(record["official_score_cents"]),
            terminal_cash_cents=int(record["terminal_cash_cents"]),
            terminal_salvage_cents=int(record["terminal_salvage_cents"]),
        )

    @property
    def cash_only_score_cents(self) -> int:
        """Remove only the terminal salvage term."""
        return self.official_score_cents - self.terminal_salvage_cents


@dataclass(frozen=True, slots=True)
class RankingRow:
    """One candidate's equal-weight 24-outcome result."""

    rank_official: int
    rank_cash_only: int
    model_id: str
    outcomes: int
    mean_score_usd_k: float
    mean_terminal_salvage_usd_k: float
    mean_cash_only_score_usd_k: float


@dataclass(frozen=True, slots=True)
class SeedEffect:
    """Matched entrant-versus-Rule difference at one seed."""

    seed: int
    private_official_cents: int
    market_official_cents: int
    private_cash_only_cents: int
    market_cash_only_cents: int


class TerminalValuationAnalysis:
    """Check terminal revaluation without rerunning or changing decisions."""

    def __init__(self, outcomes: tuple[Outcome, ...]) -> None:
        self.outcomes = outcomes
        grouped: dict[tuple[int, str], list[Outcome]] = defaultdict(list)
        for outcome in outcomes:
            grouped[(outcome.seed, outcome.run_id)].append(outcome)
        self.markets = {key: tuple(value) for key, value in grouped.items()}
        self._validate()

    @classmethod
    def load(cls, source: Path) -> TerminalValuationAnalysis:
        """Load archived completed outcomes."""
        document = json.loads(source.read_text(encoding="utf-8"))
        return cls(tuple(Outcome.from_record(row) for row in document["cases"]))

    def _validate(self) -> None:
        """Require the complete formal panel and exact accounting identity."""
        counts = Counter(outcome.model_id for outcome in self.outcomes)
        bases = {
            outcome.terminal_cash_cents
            + outcome.terminal_salvage_cents
            - outcome.official_score_cents
            for outcome in self.outcomes
        }
        if len(self.outcomes) != 216 or len(self.markets) != 27:
            raise ValueError("Expected 216 outcomes across 27 completed markets")
        if len(counts) != 9 or set(counts.values()) != {24}:
            raise ValueError("Expected nine candidates with 24 outcomes each")
        if {outcome.seed for outcome in self.outcomes} != {11, 29, 47}:
            raise ValueError("Unexpected main-evaluation seeds")
        if any(len(market) != 8 for market in self.markets.values()):
            raise ValueError("Every market must contain eight firms")
        if bases != {EXPECTED_INITIAL_VALUE_CENTS}:
            raise ValueError(f"Score identity or initial value differs: {bases}")

    def rankings(self) -> list[RankingRow]:
        """Compare official and cash-only mean Scores."""
        grouped: dict[str, list[Outcome]] = defaultdict(list)
        for outcome in self.outcomes:
            grouped[outcome.model_id].append(outcome)
        measures = {
            model: (
                fmean(item.official_score_cents for item in cases),
                fmean(item.terminal_salvage_cents for item in cases),
                fmean(item.cash_only_score_cents for item in cases),
            )
            for model, cases in grouped.items()
        }
        official_order = sorted(measures, key=lambda model: -measures[model][0])
        cash_order = sorted(measures, key=lambda model: -measures[model][2])
        cash_rank = {model: index + 1 for index, model in enumerate(cash_order)}
        return [
            RankingRow(
                rank_official=index + 1,
                rank_cash_only=cash_rank[model],
                model_id=model,
                outcomes=len(grouped[model]),
                mean_score_usd_k=measures[model][0] / CENTS_PER_USD_THOUSAND,
                mean_terminal_salvage_usd_k=measures[model][1] / CENTS_PER_USD_THOUSAND,
                mean_cash_only_score_usd_k=measures[model][2] / CENTS_PER_USD_THOUSAND,
            )
            for index, model in enumerate(official_order)
        ]

    def matched_effects(self) -> dict[str, dict[str, object]]:
        """Revalue all same-seat entrant-versus-Rule comparisons."""
        models = {outcome.model_id for outcome in self.outcomes} - {RULE_MODEL_ID}
        results: dict[str, dict[str, object]] = {}
        for model in sorted(models):
            effects: list[SeedEffect] = []
            for seed in (11, 29, 47):
                markets = [
                    market
                    for (market_seed, _), market in self.markets.items()
                    if market_seed == seed
                ]
                reference = self._one_market(
                    markets,
                    lambda market: RULE_MODEL_ID not in {item.model_id for item in market},
                )
                replacement = self._one_market(
                    markets,
                    lambda market: {item.model_id for item in market}
                    == models.difference({model}).union({RULE_MODEL_ID}),
                )
                entrant = self._one_outcome(reference, model)
                rule = self._one_outcome(replacement, RULE_MODEL_ID)
                if entrant.company_id != rule.company_id:
                    raise ValueError(f"Unmatched seat for {model}, seed {seed}")
                effects.append(
                    SeedEffect(
                        seed=seed,
                        private_official_cents=entrant.official_score_cents
                        - rule.official_score_cents,
                        market_official_cents=sum(
                            item.official_score_cents for item in reference
                        )
                        - sum(item.official_score_cents for item in replacement),
                        private_cash_only_cents=entrant.cash_only_score_cents
                        - rule.cash_only_score_cents,
                        market_cash_only_cents=sum(
                            item.cash_only_score_cents for item in reference
                        )
                        - sum(item.cash_only_score_cents for item in replacement),
                    )
                )
            results[model] = {
                "seed_effects_cents": [asdict(effect) for effect in effects],
                "mean_private_official_usd_k": fmean(
                    effect.private_official_cents for effect in effects
                )
                / CENTS_PER_USD_THOUSAND,
                "mean_market_official_usd_k": fmean(
                    effect.market_official_cents for effect in effects
                )
                / CENTS_PER_USD_THOUSAND,
                "mean_private_cash_only_usd_k": fmean(
                    effect.private_cash_only_cents for effect in effects
                )
                / CENTS_PER_USD_THOUSAND,
                "mean_market_cash_only_usd_k": fmean(
                    effect.market_cash_only_cents for effect in effects
                )
                / CENTS_PER_USD_THOUSAND,
                "private_cash_only_positive_seeds": sum(
                    effect.private_cash_only_cents > 0 for effect in effects
                ),
                "market_cash_only_positive_seeds": sum(
                    effect.market_cash_only_cents > 0 for effect in effects
                ),
                "market_cash_only_negative_seeds": sum(
                    effect.market_cash_only_cents < 0 for effect in effects
                ),
            }
        return results

    @staticmethod
    def _one_market(
        markets: list[tuple[Outcome, ...]],
        predicate: Callable[[tuple[Outcome, ...]], bool],
    ) -> tuple[Outcome, ...]:
        """Find one matched market."""
        matches = [market for market in markets if predicate(market)]
        if len(matches) != 1:
            raise ValueError(f"Expected one matched market, found {len(matches)}")
        return matches[0]

    @staticmethod
    def _one_outcome(market: tuple[Outcome, ...], model_id: str) -> Outcome:
        """Find a candidate within a market."""
        matches = [item for item in market if item.model_id == model_id]
        if len(matches) != 1:
            raise ValueError(f"Expected one outcome for {model_id}")
        return matches[0]

    def write(self, output_dir: Path) -> None:
        """Write ranking and matched-effect evidence."""
        output_dir.mkdir(parents=True, exist_ok=True)
        rows = self.rankings()
        with (output_dir / "terminal_valuation_sensitivity.csv").open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.writer(stream)
            writer.writerow(
                (
                    "rank_official",
                    "rank_cash_only",
                    "model_id",
                    "outcomes",
                    "mean_score_usd_k",
                    "mean_terminal_salvage_usd_k",
                    "mean_cash_only_score_usd_k",
                )
            )
            for row in rows:
                writer.writerow(
                    (
                        row.rank_official,
                        row.rank_cash_only,
                        row.model_id,
                        row.outcomes,
                        f"{row.mean_score_usd_k:.6f}",
                        f"{row.mean_terminal_salvage_usd_k:.6f}",
                        f"{row.mean_cash_only_score_usd_k:.6f}",
                    )
                )
        effects = self.matched_effects()
        (output_dir / "terminal_valuation_matched_effects.json").write_text(
            json.dumps(effects, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print("rank_order_unchanged", all(row.rank_official == row.rank_cash_only for row in rows))
        for row in rows:
            print(
                row.rank_official,
                row.model_id,
                f"{row.mean_score_usd_k:+.2f}",
                f"{row.mean_terminal_salvage_usd_k:.2f}",
                f"{row.mean_cash_only_score_usd_k:+.2f}",
            )
        for model in ("gpt-6-astra", "gpt-5.6-sol"):
            print("matched", model, effects[model])


def main() -> None:
    """Run the terminal-valuation check."""
    parser = argparse.ArgumentParser(description=__doc__)
    paper_dir = Path(__file__).resolve().parents[1]
    parser.add_argument(
        "--input",
        type=Path,
        default=paper_dir / "data/formal/trajectory_summary.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=paper_dir / "data/formal",
    )
    args = parser.parse_args()
    TerminalValuationAnalysis.load(args.input).write(args.output_dir)


if __name__ == "__main__":
    main()

