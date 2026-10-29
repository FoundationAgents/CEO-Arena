# Main-evaluation mechanism evidence

These files support Section 4.4 and Appendix E.1. All accounting comparisons use
the 27 main-evaluation markets at seeds 11, 29, and 47. They address positive
operating returns, the contrasting aggregate externalities of Astra and Sol, and
the four directed pairs selected by the preceding descriptive stability analysis.

## Files and coverage

- `accounts.json`: profiles of all nine candidates (24 outcomes each), Qwen's
  positive/negative outcome cohorts, Astra/Sol aggregate decompositions, and the
  three main-seed decompositions of the four selected directed pairs. Money is in
  cents; volume fields count units.
- `common-active-pair-check.json`: 12 comparisons ending before either matched
  firm goes bankrupt, or at day 500 if neither does. This separates unequal
  operating durations from differences observed while both firms are active.
- `selected_events.json`: nine examples for Astra, Sol, and Qwen. Each record
  contains a complete selected `MEMORY.md` write, its sealed decision fields, and
  settlement receipts from that decision day through the next seven days.
- `directed_selected_events.json`: seven further examples for the directed
  mechanism hypotheses, using the same record format.
- `memory_write_receipts.json`: successful file-write tool receipts for those 16
  records. These are selected file-operation receipts, not provider conversations.
- `excerpts.json` and `memory_notes.md`: verbatim excerpts and a readable index of
  their source identifiers, sealed decisions, and settlement receipts. Full note
  text remains in the two selected-event JSON files; original note units are retained.
- `coverage.json`: the original archive audit's coverage, totaling 2,477 decision
  traces across 36 company trajectories in 12 markets. The ZIP releases 16 curated
  examples, not all 2,477 raw traces. The recorded trace hashes identify their
  archived sources and can be matched to `formal/tables/company_decisions.csv`.

Qwen is negative in all three reference markets. Its three positive examples use
the lower median among its positive outcomes within each seed: Rule replaces Pro
at seeds 11/47 and Astra at seed 29. The accounting profiles retain all 24 Qwen
outcomes; positive-case selection is not a separate experimental treatment.

## Reproduction

From the supplement root, after installing the dependencies listed in its README:

```text
python paper/scripts/analyze_mechanisms.py
python paper/scripts/build_mechanism_evidence.py
```

The first command recomputes both accounting reports from the released formal CSV
tables, checks all 216 cash/Score identities, and compares the reports with the
released JSON files. Outputs go to `data/mechanisms/recomputed/`.
The second checks the 16 excerpts against the complete selected writes, confirms
successful file-write receipts, and matches their sealed decisions and economic
receipts to the formal CSV tables. It also verifies the readable Markdown index.
To regenerate that index, pass `--output path/to/memory_notes.md`.

Optional raw-trace extraction in `analyze_mechanisms.py` requires `--seed` and
`--archive-dir` pointing to matching source archives, which are not in this ZIP.
The default checks need no raw archives, model calls, or new simulations.

## Interpretation

Notes are agent-written beliefs and plans, including mistaken beliefs. File-write
success does not establish economic execution; sealed decisions and settlement
receipts provide that separate check. Actions and accounting patterns motivate
mechanism hypotheses but do not identify isolated causal effects. Other firms
adapt in each market, and the selected examples are descriptive.
