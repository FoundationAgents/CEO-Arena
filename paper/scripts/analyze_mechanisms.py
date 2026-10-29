"""Reproduce descriptive mechanism evidence from the archived main evaluation."""

import argparse
import csv
import hashlib
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path
from statistics import mean


PAPER = Path(__file__).resolve().parents[1]
TABLES = PAPER / "data/formal/tables"
EVIDENCE = PAPER / "data/mechanisms"
RULE = "deterministic-rule-ceo"
TOP = ("gpt-6-astra", "gpt-5.6-sol", "qwen3.8-max")
PAIRS = (
    ("gpt-5.6-sol", "claude-sonnet-5"),
    ("deepseek-v4-pro", "doubao-seed-1.8"),
    ("gpt-6-astra", "deepseek-v4-flash"),
    ("gpt-6-astra", "deepseek-v4-pro"),
)


def number(row: dict[str, str], field: str) -> float:
    return float(row.get(field) or 0)


@dataclass(frozen=True)
class Trace:
    seed: int
    run: str
    lineup: str
    omitted: str
    company: str
    model: str
    day: int
    status: str
    sha256: str


class MechanismAnalysis:
    def __init__(self, tables: Path, output: Path, archives: Path | None = None) -> None:
        self.tables = tables
        self.output = output
        self.archives = archives
        self.outcomes = self.rows("company_results")
        self.outcome_index = {(r["run_id"], r["model_id"]): r for r in self.outcomes}
        self.decisions = self.rows("company_decisions")
        self.edges = [r for r in self.rows("directed_effects_by_seed")
                      if (r["target_model_id"], r["affected_model_id"]) in PAIRS]

    def rows(self, name: str) -> list[dict[str, str]]:
        """Read one released formal table."""
        with (self.tables / f"{name}.csv").open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def selected_traces(self, seed: int, qwen_positive: bool = False) -> dict[str, Trace]:
        selected = {}
        positives = sorted((r for r in self.outcomes if int(r["seed"]) == seed
                            and r["model_id"] == TOP[2] and number(r, "official_score_cents") > 0),
                           key=lambda r: number(r, "official_score_cents"))
        qwen_run = positives[(len(positives) - 1) // 2]["run_id"]
        reference_models = set(TOP) | {a for _, a in PAIRS} | {b for b, _ in PAIRS}
        for r in self.decisions:
            if int(r["seed"]) != seed or not r["harness_trace_path"]:
                continue
            eligible = (r["omitted_model_id"] == RULE and r["model_id"] in reference_models)
            eligible |= (r["omitted_model_id"], r["model_id"]) in PAIRS
            if qwen_positive:
                eligible = r["run_id"] == qwen_run and r["model_id"] == TOP[2]
            if eligible:
                path = f"data/runs/seed-{seed}/{r['lineup']}/agent-traces/{r['harness_trace_path']}"
                selected[path] = Trace(seed, r["run_id"], r["lineup"], r["omitted_model_id"],
                                       r["company_id"], r["model_id"], int(r["start_day"]),
                                       r["agent_status"], r["harness_trace_sha256"])
        return selected

    def extract(self, seed: int, qwen_positive: bool = False) -> None:
        """Optionally extract notes when matching raw archives are available."""
        if self.archives is None:
            raise ValueError("Trace extraction requires --archive-dir; raw archives are not included")
        selected = self.selected_traces(seed, qwen_positive)
        found = set()
        events = 0
        suffix = "-qwen-positive" if qwen_positive else ""
        output = self.output / f"memory-audit-seed-{seed}{suffix}.jsonl"
        with output.open("w", encoding="utf-8") as destination, tarfile.open(
                self.archives / f"seed-{seed}.tar.gz", "r|gz") as archive:
            for member in archive:
                if member.name not in selected:
                    continue
                trace = selected[member.name]
                digest = hashlib.sha256()
                loaded_seen = False
                stream = archive.extractfile(member)
                assert stream is not None
                for line_index, line in enumerate(stream, 1):
                    digest.update(line)
                    event = json.loads(line)
                    payload = event.get("payload", {})
                    action = payload.get("event", {})
                    inputs = action.get("input", {})
                    if isinstance(inputs, str):
                        try:
                            inputs = json.loads(inputs)
                        except json.JSONDecodeError:
                            inputs = {}
                    if not isinstance(inputs, dict):
                        inputs = {}
                    loaded = event.get("kind") == "request-contract" and not loaded_seen
                    write = (event.get("kind") == "llm-event" and action.get("type") == "tool-call"
                             and action.get("name") in ("write_file", "edit_file")
                             and inputs.get("path") == "MEMORY.md")
                    receipt = (event.get("kind") == "tool-event"
                               and action.get("name") in ("write_file", "edit_file")
                               and b"MEMORY.md" in line)
                    if loaded or write or receipt:
                        record = {"kind": "memory-receipt" if receipt else "memory-write",
                                  "call": {**action, **({"input": inputs} if write else {})}}
                        if loaded:
                            loaded_seen = True
                            system = "\n".join(s.get("text", "") for s in payload.get("system", []))
                            marker = "## Your MEMORY.md (auto-loaded)"
                            record = {"kind": "memory-loaded", "content": system.split(marker, 1)[-1]
                                      if marker in system else ""}
                        destination.write(json.dumps({
                            **trace.__dict__, "source": member.name, "line": line_index,
                            **record,
                        }, ensure_ascii=False) + "\n")
                        events += 1
                assert digest.hexdigest() == trace.sha256, member.name
                found.add(member.name)
                if len(found) % 100 == 0:
                    print(f"seed {seed}: {len(found)}/{len(selected)} verified traces, {events} memory events", flush=True)
                if len(found) == len(selected):
                    break
        assert found == set(selected), f"Missing traces: {set(selected) - found}"
        print(f"seed {seed}: complete; {len(found)} verified traces, {events} memory events", flush=True)

    def accounts(self) -> None:
        fields = ["official_score_cents", "terminal_cash_cents", "terminal_salvage_cents",
                  "cumulative_revenue_cents", "cumulative_cost_cents", "cumulative_refunds_cents",
                  "consumer_orders", "fulfilled_units", "abandoned_units"]
        fields += [k for k in self.outcomes[0] if k.startswith("expenses_cents.")]
        summaries = []
        for model in sorted({r["model_id"] for r in self.outcomes}):
            group = [r for r in self.outcomes if r["model_id"] == model]
            summaries.append({"model": model, "n": len(group),
                              "bankrupt": sum(r["status"] == "bankrupt" for r in group),
                              "positive": sum(number(r, "official_score_cents") > 0 for r in group),
                              "net_outlays_cents": mean(number(r, "cumulative_cost_cents")
                                                        - number(r, "cumulative_refunds_cents") for r in group),
                              **{key: mean(number(r, key) for r in group) for key in fields}})
        for r in self.outcomes:
            cash = 50_000_000 + number(r, "cumulative_revenue_cents") - number(r, "cumulative_cost_cents") + number(r, "cumulative_refunds_cents")
            assert cash == number(r, "terminal_cash_cents"), r["run_id"]
            assert cash + number(r, "terminal_salvage_cents") - number(r, "initial_enterprise_value_cents") == number(r, "official_score_cents")
        qwen_cohorts = []
        for positive in (True, False):
            group = [r for r in self.outcomes if r["model_id"] == TOP[2]
                     and (number(r, "official_score_cents") > 0) == positive]
            qwen_cohorts.append({"positive": positive, "n": len(group),
                                "net_outlays_cents": mean(number(r, "cumulative_cost_cents")
                                                          - number(r, "cumulative_refunds_cents") for r in group),
                                **{key: mean(number(r, key) for r in group) for key in fields}})
        directed = []
        for edge in self.edges:
            a = self.outcome_index[edge["reference_run_id"], edge["affected_model_id"]]
            b = self.outcome_index[edge["counterfactual_run_id"], edge["affected_model_id"]]
            directed.append({"entrant": edge["target_model_id"], "rival": edge["affected_model_id"],
                             "seed": int(edge["seed"]), "reference_run": edge["reference_run_id"],
                             "rule_run": edge["counterfactual_run_id"],
                             "reference_bankruptcy_day": a["bankruptcy_day"],
                             "rule_bankruptcy_day": b["bankruptcy_day"],
                             **{key: number(a, key) - number(b, key) for key in fields}})
        aggregate = []
        for seed in (11, 29, 47):
            ref = [r for r in self.outcomes if int(r["seed"]) == seed and r["omitted_model_id"] == RULE]
            for focal in TOP[:2]:
                other = [r for r in self.outcomes if int(r["seed"]) == seed and r["omitted_model_id"] == focal]
                for scope in ("private", "rivals"):
                    a = [r for r in ref if (r["model_id"] == focal) == (scope == "private")]
                    b = [r for r in other if (r["model_id"] == RULE) == (scope == "private")]
                    aggregate.append({"entrant": focal, "seed": seed, "scope": scope,
                                      **{k: sum(number(r, k) for r in a) - sum(number(r, k) for r in b)
                                         for k in fields}})
        result = {"units": "money in cents; counts in units", "models": summaries, "qwen_cohorts": qwen_cohorts,
                  "directed": directed, "aggregate": aggregate}
        (self.output / "accounts.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    def survival_check(self) -> None:
        daily: dict[tuple[str, str], list[dict[str, str]]] = {}
        for row in self.rows("company_daily"):
            daily.setdefault((row["run_id"], row["model_id"]), []).append(row)
        result = []
        for edge in self.edges:
            keys = [(edge[name], edge["affected_model_id"])
                    for name in ("reference_run_id", "counterfactual_run_id")]
            exits = [int(self.outcome_index[key]["bankruptcy_day"]) - 1
                     for key in keys if self.outcome_index[key]["bankruptcy_day"]]
            end = min(exits or [500])
            fields = ("revenue_cents", "consumer_orders", "fulfilled_units", "gross_profit_cents",
                      "costs_cents", "refunds_cents", "expense_breakdown.procurement")
            sums = [{k: sum(number(r, k) for r in daily[key] if int(r["day"]) <= end)
                     for k in fields} for key in keys]
            result.append({"entrant": edge["target_model_id"], "rival": edge["affected_model_id"],
                           "seed": int(edge["seed"]), "common_active_through_day": end,
                           **{k: sums[0][k] - sums[1][k] for k in fields}})
        (self.output / "common-active-pair-check.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


def main() -> None:
    """Recompute both accounting reports and compare them with the released evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tables-dir", type=Path, default=TABLES)
    parser.add_argument("--output-dir", type=Path, default=EVIDENCE / "recomputed")
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE)
    parser.add_argument("--archive-dir", type=Path, help="Optional directory of matching raw seed archives")
    parser.add_argument("--seed", type=int, choices=(11, 29, 47), help="Optional raw-trace extraction")
    parser.add_argument("--qwen-positive", action="store_true")
    args = parser.parse_args()
    if args.seed is not None and args.archive_dir is None:
        parser.error("--seed requires --archive-dir; accounting needs only the released CSV files")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    analysis = MechanismAnalysis(args.tables_dir, args.output_dir, args.archive_dir)
    analysis.accounts()
    analysis.survival_check()
    for filename in ("accounts.json", "common-active-pair-check.json"):
        actual = json.loads((args.output_dir / filename).read_text(encoding="utf-8"))
        expected = json.loads((args.evidence_dir / filename).read_text(encoding="utf-8"))
        if actual != expected:
            raise ValueError(f"Recomputed report differs: {filename}")
    print("PASS: 216 cash/Score identities, model profiles, Qwen cohorts, and matched decompositions.")
    print("PASS: all 12 common-active-horizon directed comparisons match the released reports.")
    if args.seed is not None:
        analysis.extract(args.seed, args.qwen_positive)


if __name__ == "__main__":
    main()
