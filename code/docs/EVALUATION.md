# Complete Leave-One-Out Arena

The official protocol evaluates `K = 8` LLM CEOs and one deterministic rule CEO
`R` in a market with `N = 8` company seats. It exhaustively runs the nine
leave-one-out lineups over three preregistered seeds:

```text
lineups = C(9, 8) = 9
official runs = 3 × 9 = 27
company trajectories = 27 × 8 = 216 (192 LLM + 24 Rule)
```

Let `C = {M1, ..., M8, R}` and `L_-j = C \ {j}`. `L_-R` is the all-LLM
reference market. In `L_-Mi`, the rule CEO replaces `Mi` in its company seat;
the other seven LLMs retain their seats. The frozen model order, seat mapping,
lineups, and seeds are part of the plan hash and are independent of API-key
grouping. There is no seat rotation or competitor-alias layer.

The formal panel dated 2026-09-11 removes Fable, uses `deepseek-v4-flash` in seat 7,
and retains `deepseek-v4-pro` in seat 2. The exact model order and key-entry routing are listed
in [FORMAL_MODEL_PANEL.md](FORMAL_MODEL_PANEL.md) and the public example TOML.
The new campaign `retail-eight-models-20260911` preserves the other model routes,
score, seeds and horizon. The reduced company count requires fresh runs; the
paused 30-run campaign and its frozen records are not relabeled or resumed.

## Outputs

The sole official company score is terminal return. Write `S(i,-j,s)` for CEO
`i`'s score in lineup `L_-j` under seed `s`.

- **Track A — average operating performance.** Each candidate appears in eight
  lineups and receives 24 scores. Track A is their arithmetic mean and is the
  only leaderboard value.
- **Track B — leave-one-out opponent stability.** For each candidate and lineup,
  first average its three seed scores. Report the population standard deviation
  across the eight lineup means, plus a separate pooled within-lineup seed
  standard deviation. These are diagnostics and are never blended into Track A.
- **Track C — Rule-anchored value creation and directed payoff externalities.**
  For each LLM `B` and seed, compare `L_-R` with `L_-B`. The pair changes only
  the CEO in `B`'s fixed seat: `B` in the reference Run and `R` in the
  replacement Run. All other LLM identities and seats are unchanged.
- Harness errors and usage remain diagnostic, never extra Score terms.
  The `ceos-decision-v9` profile allows 32 operating replies, 131,072 generated
  tokens and 60 active minutes per window, with a query-free closing phase.
  A model that cannot seal within its budget receives a recorded
  `budget_exhausted` hold. Context maintenance adds charged requests. This is not equal compute.
  Provider errors retry per company with no total attempt ceiling while healthy
  CEOs finish and persist. An incomplete or paused market has no official final
  result. Explicit finite test profiles must be separately labeled.

### Track A and Track B definitions

For the three-seed lineup mean and each candidate's overall mean:

```text
mean_score(i,-j) = mean_s S(i,-j,s)
track_a(i) = mean_(j != i) mean_score(i,-j)
```

Track B reports:

```text
opponent_sd(i) = population_sd_(j != i) mean_score(i,-j)
seed_sd(i) = sqrt(mean_(j != i,s) (S(i,-j,s) - mean_score(i,-j))^2)
```

For an LLM, its company seat is fixed across all eight lineups in which it
appears. `R` necessarily inherits eight different omitted-model seats, so its
Track B value is a baseline diagnostic rather than a directly comparable LLM
stability estimate.

### Track C decomposition

Let `Y(-j,s)` be the sum of all eight company scores in `L_-j`. It measures total
company return, not consumer surplus or social welfare. For LLM `B`:

```text
private(B,s) = S(B,-R,s) - S(R,-B,s)
effect(B -> A,s) = S(A,-R,s) - S(A,-B,s),  A != B
others(B,s) = sum_(A != B) effect(B -> A,s)
system(B,s) = Y(-R,s) - Y(-B,s)
system(B,s) = private(B,s) + others(B,s)
```

A positive `effect(B -> A)` means that replacing the Rule CEO with `B` raises
`A`'s score in this fixed reference market; a negative value means it lowers
`A`'s score. The effect is directional and need not be symmetric. It measures an
observed payoff externality, not intent, communication, cooperation, altruism,
or a universal relationship across arbitrary markets. Report the three
seed-level effects together with their mean; do not turn the sign into a claim
about a model's motive.

The current reference is the [Fixed Single-Market Retail CEO v3](RULE_BASELINE.md),
not all possible non-LLM controllers. Its consumer-retail-only scope conditions
Track C; changing Rule can also change Track A/B because it appears in eight of
nine lineups. A formal campaign must use one frozen Rule version throughout;
historical v1 outcomes cannot be relabeled as v2 or mixed into v2 trajectories.

## Paper artifacts

`ceo_arena.reporting.build_paper_report()` converts a verified
`ArenaAnalysis` into the fixed Figure 2/3 and Table 2/3 data contract;
`write_paper_artifacts()` writes deterministic SVG and CSV files. Figure 2A
requires all eight backgrounds with three seeds each for this panel. Figure 3B
contains all 56 directed off-diagonal LLM pairs. Heatmap stars are disabled
unless the caller explicitly supplies a positive preregistered economic
threshold; optional bankruptcy rates are left blank when unavailable.

## Important symmetry boundary

The market rules and initial endowments are structurally symmetric, while named
random streams remain tied to company IDs. The fixed-seat construction makes
each `L_-R` versus `L_-B` comparison pathwise aligned: the same seed drives the
same company seats, `B` and `R` occupy the same replaced seat, and the other
seven LLMs retain their identities and seats. Other CEOs still observe the new
market and decide again; their actions are not replayed from the paired Run.

Track C is therefore a controlled, Rule-anchored same-seat substitution for the
specified all-LLM reference market. Multiple seeds quantify stochastic
variation, but the resulting externalities must not be generalized to every
possible market composition.

## Reproducibility and resume

An Arena plan freezes scenario content/hash, seeds, lineups, budgets,
model/Harness/API-route identities, secret-free provider-endpoint hash, Python/Bun/OpenCode
versions, lockfiles, and all economy/runtime/Harness source hashes. Each run keeps
an immutable manifest, append-only trace, atomic checkpoint, model-free exact
replay receipts, and agent-trace hashes.

Campaign Runs share a global `seed_priority` pool. The frozen seed order decides
which eligible Run gets the next free slot; later seeds can begin while earlier
Runs remain active or wait for retry. This changes execution timing only: every
Run retains its own economic state, RNG streams, observations, decisions, and
checkpoint. Historical `seed_wave` plans retain their barrier semantics.

The secret-free plan freezes the 30-Run cap, seed ordering, sampling interval,
model routes, source and protocol versions. Global model and resident-worker
concurrency and total retry attempts have no default ceiling. Resources are
sampled only for observability; neither memory nor Score throttles admissions.
A later eligible seed may fill a free Run slot without changing any market's
economic time. Independent ad-hoc launcher processes are not a substitute for
one Campaign scheduler when enforcing the shared Run cap.

The Campaign stores a durable increment for every received CEO decision, then
writes the full world checkpoint at window boundaries. The SQLite WAL/FULL
transaction and standalone receipt journal retain the acknowledged-decision
durability guarantee. Recovery overlays increments before selecting missing
CEOs. No provider failure is converted into an economic no-op merely to advance
time. Temporary failures back off without a total retry ceiling; explicit pause
stops further work. Integrity/configuration failures remain visible and must not
be ignored.

`performance.jsonl` / `performance_status.json` and Worker performance sidecars
record prompt-free timings and classified failures separately from economic
state. The dashboard uses lightweight summaries and loads full selected-run
data only when needed. Missing timings and provider usage are not invented.
Retries may duplicate billed requests whose original completion was not received.

The v9 profile freezes model-specific inference settings and expected response
identities in addition to the window budgets. See [COST_CONTROL.md](COST_CONTROL.md)
and [FORMAL_MODEL_PANEL.md](FORMAL_MODEL_PANEL.md). Usage and infrastructure
diagnostics are reported separately from Score. New manifests and decision
fingerprints are required; runtime remains v4. Historical profiles are not
evidence for this execution profile.

On resume, cached scores are not trusted: the runner validates the immutable
configuration, reuses the authoritative SQLite frontier, verifies Agent trace
hashes, performs Exact Replay, and recomputes terminal scores. SQLite leases plus
revision CAS prevent two processes from owning the same Run; per-directory locks
remain a second local guard.

Successful verification atomically stores a typed replay receipt with canonical
checkpoint/runtime-trace hashes and verifier fingerprints.
`final_checkpoint.json` is a rebuildable audit convenience, never the recovery
authority. Any changed authoritative input invalidates the receipt.

The endpoint hash proves which configured gateway URL was used, not that a
third-party gateway kept the model weights behind an alias unchanged. A paper
release should use version-pinned routes and retain any provider-resolved model
or system fingerprint available in the raw agent trace.
