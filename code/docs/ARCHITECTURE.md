# Architecture

Read [Running](RUNNING.md) for commands and [History](HISTORY.md) for archived versions.

## Dependency direction

```text
models
  ↑
economy ← runtime ← campaign ← cli
             ↑          ↑
          harness   SQLite repository
             ↑
       OpenCode AgentWorker

evaluation depends on models and persisted run results, never on a provider.
```

`MarketWorld` is the sole economic state writer. The Coordinator never edits
cash, inventory, orders, satisfaction, research, or tenders. An Agent Harness
never edits the world; operating tools write to a draft, while file tools
persist only company-owned files.

Initial enterprise value includes initial cash and the initial inventory at the
same liquidation basis used at the terminal date. This prevents free starting
inventory from being counted as investment return.

## Private memory and weekly conversation

`CompanyWorkspace` provides immediately persistent isolated Run/company files.
`ConversationJournal` owns the current native conversation, pending tool receipts,
draft and usage. `DecisionBriefing` provides two bounded factual feedback periods.
These are separate from the economic world. Each new shared-market window clears
conversation history and auto-loads up to 40,000 characters of MEMORY.md. Other
files remain readable on demand. No mandatory review or summarizer remains.
See [MEMORY.md](MEMORY.md). Protocols are decision v8 and runtime v4.

## Deep Module Interfaces

### MarketWorld

```python
create_initial_state(company_ids) -> WorldState
project_observations(state) -> dict[company_id, CompanyObservation]
advance_window(state, decisions) -> WindowResult
```

Its implementation owns all ordering, stochasticity, accounting, and market
mechanics. Tests cross this same interface.

### AgentHarness

```python
await decide(
    request: DecisionWindowRequest,
    context: CompanyContext,
) -> DecisionWindowResult
```

Implementations are the deterministic Rule CEO and the OpenCode Adapter. This
is a real seam because production and test adapters both exist.

### BenchmarkCoordinator

```python
await run(resume=True) -> BenchmarkRunResult
```

It snapshots observations, invokes all active CEOs concurrently, waits at the
barrier, validates every sealed decision against the same `world_version`, and
calls `MarketWorld.advance_window` exactly once.

### RunStore / ExactReplay

`CheckpointStore.stage_decision(run_id, config_hash, window, response)` durably
records one immutable current-window receipt before its task returns. Standalone
storage uses a FULL-synchronous SQLite journal next to the compact JSON world
snapshot; Campaign storage reuses its `decision_responses` rows. A load overlays
only receipts bound to the same run, configuration, window and starting state.
Conflicting duplicates or cross-window receipts are rejected.

Full snapshots are written at collection, sealing and economic commit boundaries,
not after every CEO. Snapshot publication precedes journal compaction, so crashes
on either side recover a complete frontier without losing acknowledged decisions.
Replay consumes the reconstructed checkpoint and preserved traces; it never calls
a model. Required fsync/atomicity and independent trace hash validation remain.

### CampaignApplication / CampaignRepository / ModelRouteRegistry

`models.py` defines frozen experiment plans; `records.py` defines typed lifecycle
records and their invariants; `repository.py` owns every SQLite transaction. The
split does not create another state writer or change transaction ordering.

```python
await CampaignApplication(spec, plan, output).run() -> CampaignReport
```

The compiler turns the ordered pool of nine LLMs plus the automatic Rule CEO
into the ten `C(10,9)` leave-one-out lineups for each frozen seed. Each LLM owns
one fixed company seat in the all-LLM reference lineup. When an LLM is omitted,
the Rule CEO inherits that seat and the other eight LLMs remain in place. This
single seat-mapping contract is reused by execution and evaluation.
`ModelRouteRegistry` keeps one fixed model-to-API entry mapping in process
memory; credentials never enter the plan or database. One Campaign lock owns
the shared Run pool. Each Run also uses a SQLite lease
and revision CAS, so a crashed owner can be taken over without two writers
advancing the same economy.

SQLite WAL is the authority for active and terminal Checkpoints, staged
responses, failure history, Results, Replay Receipts, and official Analysis.
Run completion stores Checkpoint + Result + Receipt atomically; Campaign
completion stores Analysis and status atomically. JSON Analysis and final
Checkpoint files are rebuildable projections, never recovery authorities.
Standalone recovery validates this version's snapshot plus decision journal and replay
without model calls. There is no legacy import or schema migration; unsupported historical
formats require their original archived implementation.

### CampaignScheduler / WorkerPool

The `seed_priority` scheduler fills at most thirty active Run slots, respecting
pause state and leases; eligible later seeds may backfill. There is no default
model, resident-worker, idle-worker or total-attempt ceiling. Resource sampling
is observation-only, not admission control. Explicit finite limits remain useful
for tests, but do not appear in the default/active experiment profile.

An unbounded `WorkerPool` retains each healthy process under one fixed
Harness/company identity. It never shares a worker across model, API route or
Run. A company failure does not retire another company's workers. Run shutdown
or user cancellation still drains that Run's resources before releasing ownership.

`scheduler_status.json` and `scheduler_events.jsonl` expose activity and resource
samples, with nullable limits displayed as unlimited. They are diagnostics,
not recovery authority or Score inputs.

### Performance and failure isolation

The Coordinator retries transient provider failures independently for each company, using
cancellable backoff within the original time allowance, including the backoff itself. Its immutable observation
and accepted peers remain available; a complete barrier is still required before
market settlement. Transient transport failures can resume the same Worker
conversation prefix/draft; structured permanent or Worker faults are returned
to the Coordinator rather than silently becoming economic decisions. Permanent failures
stop that Run for attention; exhausted transient recovery records a timeout with failure
metadata and an explicit no-op, not a model seal. One-time actions are never replayed.

`PerformanceRecorder` writes `performance.jsonl` and a small
`performance_status.json` with stage timings and per-company progress/failures.
Worker traces have a separate `.timing.jsonl` for request start/first event/end,
tool execution, retries and available usage. These fields contain no prompts,
SQL text, response bodies or credentials. Optional timing-file failures do not
cancel healthy decisions; failure of required durable trace/receipt writes remains
an integrity error. Timings overlap across workers and are not summed as wall time.

### LeaveOneOutDesign / ArenaAnalyzer

`ceo_arena.protocol.LeaveOneOutDesign` is the single owner of the official
lineup, Rule identity, and seat contract. It returns the all-LLM reference first,
then one Rule substitution per LLM seat, and rejects incomplete, reordered, or
seat-drifted matrices.
`ArenaAnalyzer` consumes verified terminal results from that design and produces
typed Track A means, Track B between-/within-lineup dispersion, and Track C
private, other-company, system, and directed-externality effects. Analysis never
calls a provider or mutates the economy.

### Decision admission

Campaign defaults use `UnlimitedDecisionAdmission`: it measures concurrent model
work without queueing it. Explicit finite adapters remain available for tests.
Run concurrency is capped; each decision still has its own computational allowance.
There is no second batch executor. Single-run diagnostics reuse the same Coordinator.

### RunDashboard

```python
list_runs() -> DashboardRunList
get_run(run_ref) -> DashboardRun
```

This is a read-only projection module over current standalone Run artifacts and
transactionally consistent Campaign SQLite snapshots. FastAPI is a thin adapter
at the interface; the browser receives
typed summaries, never paths, credentials, notebooks, raw world state, or raw
Harness traces. A server-issued `run_ref` prevents browser-controlled path
resolution. A cancellable SSE Adapter publishes opaque revision changes while
the typed projection exposes only concise per-window operating actions and usage
totals. `RunDashboard` itself cannot advance or modify the economy.
Lists read lightweight job/frontier metadata; only a selected Run loads its complete
checkpoint and current-window receipts. Economic caches do not invalidate on lease
renewal alone. The Run listing also includes a compact resource projection from
`scheduler_status.json`. It excludes raw probe errors and
process details. Status-file changes and sampling expiry join the existing SSE
revision. A missing, stopped, invalid, or stale sampler is shown as unavailable;
SQLite leases and Run state remain the lifecycle source of truth.

### CampaignControl / CampaignProcessPort

```python
snapshot() -> ExperimentCatalog
start(experiment_ref, idempotency_key) -> StartReceipt
revision() -> str
```

`CampaignControl` is the write-capable local control Module beside the still
read-only `RunDashboard`. It discovers only non-linked TOML files in one fixed
server-side directory, publishes complete model lineups without credentials or
paths, prepares the selected Campaign, and derives lifecycle truth from SQLite,
the Campaign lock, and a short-lived process handle. The browser submits only an
opaque experiment reference and an idempotency key.

`CampaignProcessPort` is an internal Seam with two real Adapters: production
uses a hidden detached Python process; tests use a recording Adapter. The Runner
therefore survives Dashboard restarts while continuing to reuse
`CampaignApplication`, SQLite Lease/CAS, seed-priority scheduling, and Exact Replay.
FastAPI only adapts this small Interface and merges its Revision into the
existing SSE stream.

## Economic order within a day

1. advance the macro process;
2. receive due procurement and complete due capacity, R&D, and research jobs;
3. depreciate knowledge capital and update advertising goodwill/awareness;
4. charge active daily marketing, development, reliability, support, and
   capacity policies;
5. abandon or fulfill old backlog under inventory and capacity limits;
6. generate one shared consumer pool, allocate MNL choices, and reroute
   stockouts;
7. resolve due tenders and reserve the winner's enterprise inventory;
8. generate/resolve support tickets and update satisfaction/loyalty;
9. place cash-constrained order-up-to replenishment and charge holding costs;
10. mark bankruptcies, age research uncertainty, record daily history, and open
    scheduled future tenders.

Company collections are sorted before economic calculation. Random draws use
named seeds derived from `(scenario_seed, day, domain, stable_entity_id)`, not
from call order.

## Information model

### In-window error recovery

The TypeScript `DecisionSession` owns one conversation and draft. `DecisionBudget`
accounts for every response and attempted call, including rejected calls. Project
Provider Adapters preserve malformed tool JSON as non-executable audit data while
finishing usage collection; the pinned OpenCode tree remains unmodified.
`RpcCompanyBridge` reuses the worker's correlated NDJSON channel for queries and
pre-seal validation. `CompanyContext.validate_decision` calls the existing world's
side-effect-free validator; the draft stays open on rejection. Only finalized
outcomes reach the Coordinator barrier and receipt journal. Default budget ceilings
are 32 replies, 131,072 generated tokens and 60 active minutes. Early finalization
allows at most four further replies within those limits. `ConversationJournal`
restores native messages and tool receipts without summaries or rolling truncation.
`budget_exhausted` carries previous policies; immediately persisted files remain.
See [COST_CONTROL.md](COST_CONTROL.md). `DecisionDiagnostics`
survives checkpoint/resume, runtime trace, replay, and Arena aggregation.

Agents see only their own Company projection, public prices, open tenders, and
delayed macro publication. True segment parameters, competitor cost/inventory,
other companies' decisions, RNG keys, and pending private reports stay server
side. Market research returns a private Bayesian posterior after cost and
delay.

Public offers include list price, promotion, and effective price, and bankrupt
companies disappear from that active offer set. Pending stochastic R&D keeps
its realized completion day and gain server-side until the project completes.

## Inference identity (v9)

`InferenceSettings` owns the secret-free provider contract: TOML → frozen routes →
Run assignments → Worker environment → typed native wire adapters. Cross-language
tests keep Python and TypeScript profiles identical. `NewApiModel` routes Responses,
Messages or Chat without changing the pinned OpenCode checkout; `withIdentity`
checks serving identities before dispatch. A mismatch is a permanent infrastructure
failure, not a scored hold. Conversation recovery binds the exact inference settings;
changing a model or reasoning profile requires a new experimental run.
