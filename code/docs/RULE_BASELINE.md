# Fixed Single-Market Retail CEO (Rule v3)

The public Rule is a deterministic consumer-retail playbook, not an online
optimizer, a no-action controller or a globally optimal CEO. One configuration
is frozen for a formal campaign. The candidate label is `deterministic-rule-ceo`;
`rule-ceo-fixed-v3-<hash>` binds all fields of the full configuration.

Implementation: [`RuleCeoHarness`](../src/ceo_arena/harness/rule_ceo.py).
Installed parameters: [`rule_profile.json`](../src/ceo_arena/harness/rule_profile.json).
`RuleCEOConfig()` describes the fixed background reference; `RuleCeoHarness()`
loads the installed profile. Pass an explicit configuration for a search case.

## 1. Fixed operating policy

| Component | Rule |
|---|---|
| Prices | Catalog initial price × a fixed multiplier; integer-cent floor, minimum one cent; promotions zero |
| Inventory | Fixed order-up-to targets A/B/C = 150/100/50; the engine computes actual procurement from stock position |
| Focus | Value → product A; mainstream → B; premium → C |
| Development | A fixed daily amount on the core product only; other products zero |
| Marketing | A fixed daily total on the chosen segment only; split across its sorted channel IDs, distributing remainder cents in that order |
| Operations | Fixed daily reliability and support budgets, including when optional spending is off |
| Cash gate | At each decision boundary, cash strictly above the fixed floor enables development/marketing; cash at or below it sets both to zero |
| Excluded | Price matching, sales forecasts, adaptive targets, ROI allocation, enterprise bids, market research, capacity adjustments, discrete innovation |

All three products remain for sale to all eligible consumers. Single-market
focus concentrates investment and advertising; it does not filter buyers.
Prices and targets never react to rivals, sales, backlog or remaining days.
There is no end-of-horizon liquidation optimization or computed cash reserve.
The cash gate is the only conditional business rule; it does not guarantee
solvency, because operations and ordinary procurement continue.

Only the authorized cash balance, visible catalog and marketing labels are used.
Missing required A/B/C catalog or target-channel entries is a protocol error,
not a hidden-state fallback. Decisions use the ordinary gateway and sealing
validation. Policies persist between seven-day decision boundaries; budgets are
charged per simulated day, not once per window. Economic functions, initial
endowments, score, bankruptcy and information permissions are unchanged.

## 2. Predeclared parameter search

The idea follows [CEO-Bench](https://arxiv.org/abs/2606.18543v2)'s fixed-playbook grid search, adapted to this shared
market rather than its single-company subscription economy. It does not import
CEO-Bench's prices or fitted result.

| Dimension | Values |
|---|---|
| Price multiplier | 0.9, 1.0 (A/B/C baseline prices: $40/$75/$135) |
| Focus | Value, mainstream, premium |
| Daily spending package | Light: development/marketing/reliability/support = $50/$50/$25/$50; standard: $100/$100/$50/$100 |
| Optional-spend cash floor | $50,000, $150,000 |

There are exactly **2 × 3 × 2 × 2 = 24 configurations**. Stock targets are not
searched. Enumeration order is the table's value order, with price outermost
and floor innermost; IDs run `candidate-01` through `candidate-24`.

Each world has nine companies and the unchanged 500-day `ceos-market-v3`
scenario. Company 1 receives the candidate. Companies 2–9 receive fixed
background playbooks: **3 value, 3 mainstream, 2 premium**, in that seat order;
all use multiplier 1.0, light spending and the $50,000 floor. These opponents
re-decide in each fresh world; their trajectories are not replayed from a
candidate's alternative world. No LLM takes part, even for background companies.

1. Freeze `search_plan.json`, its hash, all source/lockfile hashes and source archive before execution.
2. Run every candidate on development seeds **7, 19, 42**: 72 worlds.
3. Maximize the focal company's mean official terminal Score across those seeds. Compare integer score sums; an exact tie picks the earlier candidate ID. Market total or any LLM's score is not a selection objective.
4. Write `selection.json` before validation. Run only the frozen winner on **211, 307, 401**: three fresh worlds, same eight background policies.
5. Publish all development scores and the three validation scores. Do not retune from validation or silently discard bankrupt companies. Install the selected configuration only after the procedure completes.

The official Score is terminal cash plus terminal inventory liquidation value
minus initial enterprise value, not sales revenue. Negative Score does not imply
bankruptcy. A completed all-bankrupt world is valid; an interrupted world is not.

## 3. Reproduction and audit

**Version note:** the completed 75-world search used package 0.10.0 / decision
v4. Later decision v5 adds LLM-only review/memory; the fixed Rule algorithm and
selected profile remain unchanged. Preserve the archived reports and tables.
To reproduce the old search or regenerate its source-matched publication, use
its recorded source archive. Running the commands below on newer source needs
a new output directory; source guards must not be bypassed or old runs relabeled.

From the main project, without credentials:

```powershell
.\.venv\Scripts\python.exe -B scripts/search_rule.py `
  --output .ceo-arena/rule-search/fixed-rule-v3 --workers 2
```

The worker count affects scheduling only. The same command resumes the same
frozen search; incompatible source/configuration is rejected. It never resumes
or alters existing LLM experiments. All search participants receive explicit
parameters, so the eventually installed default profile is excluded from the
search's identity (but archived). Normal Arena fingerprints include that file.

Each case uses the current `execute_run`, coordinator, durable checkpoint and
decision journal, then verifies all committed days with Exact Replay and checks
cash reconciliation. The report is not published until all 75 cases complete.
Artifacts include per-run manifests, checkpoints, SQLite decisions, runtime and
performance logs, summaries, replay-verified results, `selection.json`,
`report.json`, `selected_config.json`, and `scores.csv`. No provider is created;
reported provider turns, model-tool calls and model API calls are zero.

After completion, publish the source-matched receipts and bilingual appendix
tables (this does not install parameters or rerun any world):

```powershell
.\.venv\Scripts\python.exe scripts/publish_rule_search.py `
  --report .ceo-arena/rule-search/fixed-rule-v3/report.json
```

The publisher rejects incomplete development, missing validation, a changed
winner or inconsistent replay counts. It writes the full public receipt to
`docs/validation/fixed-rule-search-v3.json` and a paper-side JSON/table export.

## 4. Interpretation and version boundary

The winner is best **within this declared 24-point grid, under one fixed
heterogeneous Rule background, on the development seeds**. Independent-seed
validation checks seed sensitivity; it does not establish optimality against
arbitrary opponents, equilibrium or transfer to LLM markets. The family and
background were designed after earlier pilot inspection, not blindly chosen.
The rule's online simplicity is compatible with offline parameter tuning.

The main evaluation uses eight LLM agents plus one Rule candidate, nine
eight-company lineups and three seeds (11/29/47), for 27 runs. No Action is
used only in the separate reference-policy check. Track C is conditional on
this exact reference; changing it can also change Track A/B because eight
lineups contain Rule. Never mix reactive v1/v2 and fixed
v3 decisions in one formal trajectory. Historical v1/v2 receipts keep their
original results and hashes, and are not relabeled as evidence for v3.

## 5. Completed search and installed profile

**All 75 worlds completed at day 500** with 37,500 replay-verified days,
48,600 sealed decisions and zero model API calls. All 675 company instances
remained active. The frozen plan hash is
`14a39d71d2854437c730615e88b4388ab6ba5e6b9995e5c5797cec7d16aca9ba`.

Selected **candidate-17**: mainstream segment / product B, multiplier 1.0,
light spending and a $50,000 cash floor. Default identity: `rule-ceo-fixed-v3-6f790d2aeb8e`.

| Stage | Seed | Focal official Score (USD) |
|---|---:|---:|
| Development | 7 | 49,793.11 |
| Development | 19 | 97,268.94 |
| Development | 42 | 69,133.14 |
| Development mean | — | **72,065.06** |
| Validation | 211 | 54,043.81 |
| Validation | 307 | 104,331.39 |
| Validation | 401 | 80,111.75 |
| Validation mean | — | **79,495.65** |

Candidates 17 and 18 tie exactly; the predefined candidate order chooses 17.
Every corresponding cash-floor pair has identical development returns in this
sample. The data therefore do **not** establish that $50,000 is a better floor
than $150,000. Neither floor nor other parameters were adjusted after validation.

The search ran directly in the main checkout. It began with two workers and
resumed the same frozen search with four; completed receipts were reverified
and preserved. The source archive is for audit, not an isolated execution copy.

[Full configuration, all 72+3 results and hashes](validation/fixed-rule-search-v3.json).
Raw per-world trajectories remain in
the local [archive](HISTORY.md), under the original `.ceosbench/rule-search/` path. The selected JSON has been installed in
`src/ceo_arena/harness/rule_profile.json`; newly started processes use it.
Existing processes and old campaigns were not migrated or restarted.

Acceptance: 431 Python tests passed with the installed profile; Ruff and
formatting checks passed. Bilingual structure/math checks cover 21 source pairs.
The English main text remains within nine pages; both full manuscripts and the
two-page standalone Rule explanation were rebuilt and visually checked.
