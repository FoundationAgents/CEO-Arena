"""Check selected memory writes against the released decisions and action receipts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

PAPER = Path(__file__).resolve().parents[1]
type WriteKey = tuple[str, str, int, str]


class EvidenceModel(BaseModel):
    """Validate the fields needed for evidence linkage."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class MemoryInput(EvidenceModel):
    path: Literal["MEMORY.md"]
    content: str


class WriteCall(EvidenceModel):
    type: Literal["tool-call"]
    id: str
    name: Literal["write_file"]
    input: MemoryInput


class TextResult(EvidenceModel):
    type: Literal["text"]
    value: str


class ReceiptCall(EvidenceModel):
    type: Literal["tool-result"]
    id: str
    name: Literal["write_file"]
    result: TextResult


class TraceRecord(EvidenceModel):
    seed: int
    run: str
    lineup: str
    omitted: str
    company: str
    model: str
    day: int
    status: Literal["sealed"]
    sha256: str
    source: str
    line: int


class SelectedEvent(TraceRecord):
    kind: Literal["memory-write"]
    call: WriteCall
    decision: dict[str, str]
    receipts: list[dict[str, str]]

    @property
    def key(self) -> WriteKey:
        return self.run, self.company, self.day, self.call.id


class MemoryReceipt(TraceRecord):
    kind: Literal["memory-receipt"]
    call: ReceiptCall

    @property
    def key(self) -> WriteKey:
        return self.run, self.company, self.day, self.call.id


class Excerpt(EvidenceModel):
    group: str
    run: str
    company: str
    day: int
    call_id: str
    text: str

    @property
    def key(self) -> WriteKey:
        return self.run, self.company, self.day, self.call_id


def csv_rows(path: Path) -> list[dict[str, str]]:
    """Read a released CSV without changing its values."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def require(condition: bool, message: str) -> None:
    """Raise a verification error independently of Python assertion settings."""
    if not condition:
        raise ValueError(message)


class EvidencePackage:
    """Link curated notes to successful file writes and sealed economic decisions.

    The released trace hashes identify the archived source; this checker compares
    those identifiers with the decision table. It cannot rehash omitted raw traces
    or establish the causal payoff of an action.
    """

    def __init__(self, evidence: Path, tables: Path) -> None:
        self.evidence = evidence
        self.events = [
            event
            for name in ("selected_events.json", "directed_selected_events.json")
            for event in TypeAdapter(list[SelectedEvent]).validate_json(
                (evidence / name).read_bytes()
            )
        ]
        self.receipts = TypeAdapter(list[MemoryReceipt]).validate_json(
            (evidence / "memory_write_receipts.json").read_bytes()
        )
        self.excerpts = TypeAdapter(list[Excerpt]).validate_json(
            (evidence / "excerpts.json").read_bytes()
        )
        self.decisions = {
            (row["run_id"], row["company_id"], int(row["start_day"])): row
            for row in csv_rows(tables / "company_decisions.csv")
        }
        self.actions = csv_rows(tables / "action_receipts.csv")

    def verify(self) -> None:
        """Check every released note, write receipt, decision, and action receipt."""
        events = {event.key: event for event in self.events}
        receipts = {receipt.key: receipt for receipt in self.receipts}
        excerpts = {excerpt.key: excerpt for excerpt in self.excerpts}
        require(len(events) == len(self.events) == 16, "Expected 16 unique selected writes")
        require(len(receipts) == len(self.receipts) == 16, "Expected 16 unique write receipts")
        require(len(excerpts) == len(self.excerpts) == 16, "Expected 16 unique excerpts")
        require(events.keys() == receipts.keys() == excerpts.keys(), "Evidence keys differ")
        for key, event in events.items():
            receipt, excerpt = receipts[key], excerpts[key]
            decision = self.decisions[event.run, event.company, event.day]
            require(decision["agent_status"] == "sealed", f"Unsealed decision: {key}")
            for field, expected in (
                ("seed", str(event.seed)),
                ("model_id", event.model),
                ("omitted_model_id", event.omitted),
                ("lineup", event.lineup),
                ("harness_trace_sha256", event.sha256),
            ):
                require(decision[field] == expected, f"Mismatched {field}: {key}")
            require(
                event.source.endswith("/" + decision["harness_trace_path"]),
                f"Mismatched trace path: {key}",
            )
            for field in TraceRecord.model_fields:
                if field != "line":
                    require(getattr(event, field) == getattr(receipt, field), f"Receipt {field}: {key}")
            require(receipt.line > event.line, f"Receipt precedes write: {key}")
            message = re.fullmatch(r"File written: MEMORY\.md \((\d+) bytes\)", receipt.call.result.value)
            require(message is not None, f"Write was not confirmed: {key}")
            if message is not None:
                require(int(message[1]) == len(event.call.input.content.encode("utf-8")), f"Note size: {key}")
            require(bool(excerpt.text) and excerpt.text in event.call.input.content, f"Excerpt text: {key}")
            expected_decision = {
                name: value for name, value in decision.items()
                if name.startswith("decision.") and value not in ("", "[]")
            }
            require(event.decision == expected_decision, f"Decision projection: {key}")
            expected_actions = [
                row for row in self.actions
                if row["run_id"] == event.run and row["company_id"] == event.company
                and event.day <= int(row["day"]) <= event.day + 7
            ]
            require(event.receipts == expected_actions, f"Action receipts: {key}")

    def markdown(self) -> str:
        """Render verbatim excerpts with their decision and receipt references."""
        excerpts = {excerpt.key: excerpt for excerpt in self.excerpts}
        lines = [
            "# Selected MEMORY.md excerpts and executed decisions", "",
            "These 16 excerpts support the main-evaluation mechanism hypotheses. "
            "They are agent-written beliefs and plans, not verified simulator descriptions "
            "or identified causal mechanisms. Notes preserve their original units and wording.", "",
            "The two selected-event JSON files contain the complete memory writes, sealed "
            "decision fields, and action receipts. memory_write_receipts.json confirms each "
            "file write. Trace hashes identify the archived source; raw provider traces are "
            "not included in this package.", "",
        ]
        for index, event in enumerate(self.events, 1):
            excerpt = excerpts[event.key]
            note_hash = hashlib.sha256(event.call.input.content.encode("utf-8")).hexdigest()
            lines += [
                f"## {index}. {event.model}: seed {event.seed}, day {event.day}", "",
                f"- Analysis group: {excerpt.group}",
                f"- Run: `{event.run}`; company: `{event.company}`; omitted candidate: `{event.omitted}`.",
                f"- Source: `{event.source}`, line {event.line}.",
                f"- Archived trace SHA-256: `{event.sha256}`.",
                f"- Complete note SHA-256 (UTF-8): `{note_hash}`.", "",
                *("> " + line for line in excerpt.text.splitlines()), "",
                "Sealed decision fields:", "", "```json",
                json.dumps(event.decision, indent=2, ensure_ascii=False), "```", "",
                "Related settlement receipts:", "",
                "| Receipt index | Day | Action | Status | Effective day |",
                "|---:|---:|---|---|---:|",
                *(f"| {row['receipt_index']} | {row['day']} | {row['action']} | "
                  f"{row['status']} | {row['effective_day']} |" for row in event.receipts), "",
            ]
        return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    """Verify the evidence and optionally regenerate its readable Markdown index."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=PAPER / "data/mechanisms")
    parser.add_argument("--tables-dir", type=Path, default=PAPER / "data/formal/tables")
    parser.add_argument("--output", type=Path, help="Regenerate the Markdown index at this path")
    args = parser.parse_args()
    package = EvidencePackage(args.evidence_dir, args.tables_dir)
    package.verify()
    rendered = package.markdown()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        require((args.evidence_dir / "memory_notes.md").read_text(encoding="utf-8") == rendered,
                "The readable memory index differs from its source records")
    print("PASS: 16 excerpts, successful memory writes, sealed decisions, and action receipts agree.")
    print("Scope: released evidence linkage; no raw-trace replay or causal-effect identification.")


if __name__ == "__main__":
    main()
