# Agent Protocol

## Decision protocol v9: frozen inference profiles and recoverable weekly conversations

The current tool contract is `ceos-decision-v9`, with economic scenario
`ceos-market-v3` and persistence protocol `ceos-runtime-v4`. This is a new
evaluation profile, not a compatible relabeling of earlier pilots. Resolved inference
settings are frozen in the Campaign routes, Run assignments and conversation binding.
See [FORMAL_MODEL_PANEL.md](FORMAL_MODEL_PANEL.md) for exact provider fields.

Each company has at most 32 accounting-valid decision replies, 131,072 generated
tokens and 60 active minutes per window. At 28 replies, 98,304 generated tokens
or 45 minutes, enter finalization: at most four further replies, disabling
`query_company` in the dispatcher. Tool definitions remain unchanged throughout
the conversation so provider-signed thinking keeps its original prefix. A legal
seal on reply 32 is accepted. Otherwise `budget_exhausted` discards the unsealed
draft, carries prior operating policies into settlement and gives the CEO another
opportunity next window. It is a non-completion, not a successful chosen hold.
Only total tool calls remain uncapped by default. API and transport
constraints still apply. [COST_CONTROL.md](COST_CONTROL.md) defines the unchanged
window allowance; [MEMORY.md](MEMORY.md) defines private files, weekly context reset
and native conversation recovery. No automatic summary call remains.

Invalid JSON, missing tools, mixed sealing and ordinary tool/SQL/economic errors
remain correctable in the same immutable observation and draft. Failed actions
do not change the draft. Mixed seal rejects the entire response; malformed native
turns are retained in audit but not replayed as executable signed history.
Successful LLM sealing is: company-bound
authoritative Python validation → local affordability/consistency checks → seal. The Harness never chooses an
economic action for the model.

Transport retries preserve completed conversation prefixes and staged work when
possible. Unrecoverable Worker/session failures are reported with typed provider
failure information to the Coordinator, which retries transient errors only for that
company within its remaining time allowance, including automatic backoff. Other CEOs continue and their
completed decisions are saved immediately. The market still waits for every
active company before settlement. User pause/cancellation remains effective.

Provider failures report category, status/reason and safe diagnostics without
credentials or model message bodies in performance sidecars. Authentication,
transport, stream decoding, usage and internal failures must not collapse into
a generic model mistake. Transient failure alone does not advance economic time;
exhausted recovery produces an explicit infrastructure timeout with prior policies retained.
Serving-model mismatches or absent identity on a completed response are a permanent
`identity` failure: stop that Run without settling the window or automatic retry.
Do not whitelist an unverified alias simply to bypass the guard.

Persisted recovery and missing-usage diagnostics remain
health observations, not Score components. The visible reply cutoff does not
guarantee a dollar ceiling or provider availability. Known usage carries across
worker retries and restarts via `allowances/`; unreported work lost in a crash
can still be repeated. `prior_usage` carries all reported consumption into the
next attempt; result `usage` contains that attempt's delta, never the prior total.
`finalization_turns` is a subset of decision replies. `budget_usage_incomplete`
marks partial-stream accounting gaps. Generated output already includes reasoning.

## Persistent file memory

Each new week auto-loads the first 40,000 characters of MEMORY.md and a new factual
dashboard. Other files persist for on-demand reading. The current week retains its
native history. File writes persist immediately, even without sealing or after
resetting a draft. There is no fixed review format or mandatory update. See
[MEMORY.md](MEMORY.md).

The factual briefing still summarizes the two latest observed periods without an
LLM call, hidden information or missing-as-zero imputation. Memory and conversation
snapshots are not fields in the economic world or CompanyDecision. Old trajectories
retain their provenance; v8 requires a new frozen Run.

## Decision window

Every CEO receives an immutable observation for one `world_version`. Tools may
query that projection and edit a private decision draft. They never mutate the
world. Private-file tools mutate only their isolated filesystem.

Price, promotion, reorder targets, daily development, marketing, reliability,
support, and the active capacity tier persist until explicitly changed. The
daily budgets and active capacity operating cost are charged every simulated
day; setting a recurring budget to zero stops it. A sealed draft with no operating changes keeps
those policies unchanged. Innovation projects, capacity requests, bids, and
market-research requests are one-time actions and are never repeated implicitly.

`start_research_project` is a delayed product-quality innovation investment.
`request_market_research` is a delayed, noisy report about the request day's
customer demand, price sensitivity and quality expectation; it does not improve
product quality. Public market signals lag their sample day and reveal no future phase.

The final tool call is `seal_decision`. Its payload must validate as a
`CompanyDecision` and must carry the server-bound company and world identity.
The model cannot select another `company_id`.

## Official tool surface

```text
query_company
set_product
set_marketing
set_operations
start_research_project
expand_capacity
downsize_capacity
cancel_capacity_project
submit_enterprise_bid
request_market_research
read_file
write_file
edit_file
search_files
glob_files
preview_decision
reset_decision
seal_decision
```

No web, natural-language CEO messaging, subagent, arbitrary MCP, or provider
fallback is part of the official profile.

Use `capacity_action_quotes` for the actual charge, refund and effective date
of available adjustments; catalog capital is cumulative, not each action's cost.
`preview_decision` reports committed one-time charges/refunds, affordability and
known recurring policy cost. It excludes future procurement, holding and failure
costs. A funds-invalid seal leaves the draft editable. `reset_decision` clears
unsealed staging, not previous world policy, persistent memory files, or consumed
turn/token/tool/time budgets. Staged mutation receipts explicitly say they have
not executed; actual results are in the next observation's `action_receipts`.
For a project or bid, an `applied` receipt confirms the request was placed, not
that quality has improved or revenue has been earned. Project completion and
contract delivery determine those later economic effects.

Own-company `daily_cashflow`, `daily_expenses`, `action_receipts`,
`capacity_policy`, `capacity_action_quotes` and `market_signal` supplement the
existing SQL tables. Product gross profit and cash flow are different diagnostics;
refunds are not sales. Pending research truth and competitor private finances
remain unavailable.

## Fable Messages transport

The local Fable adapter groups one assistant turn's local tool results into a
single immediately following user message. It validates a one-to-one ID match
before sending: missing, duplicate, orphaned or interrupted result sequences
fail locally rather than inventing results. Only adjacent result-only messages
are combined. Ordinary messages, result payloads/error flags, provider-executed
tools, signed thinking and opaque blocks are not rewritten.

The three no-argument tools (`preview_decision`, `reset_decision`,
`seal_decision`) export an object-only empty schema. Neither repair changes
the common decision loop, tool/turn/token budgets, economic actions or configured
model/key routes. Source fingerprints bind both fixes; do not silently resume a
run frozen against older code. The pinned OpenCode checkout is unchanged.

Internal traces keep the shared logical tool-message representation; grouping
occurs only when preparing the outgoing Fable wire body. A grouped result message
does not reduce tool counts, clear usage counters, or execute a tool again.

## Completion and operational failures

`sealed` is the only successful model submission. `budget_exhausted` is a
finalized non-completion: no unsealed operating changes are applied;
prior policies and already written memory files persist, and the next window proceeds. Worker-enforced token,
reply and time limits produce `budget_exhausted`; an unresponsive Worker beyond
its deadline and IPC cleanup grace is instead an infrastructure failure.

Provider failure is an infrastructure event with isolated, time-bounded retry. Exhaustion
records `timeout` plus failure metadata, never `sealed`: no unsealed changes or repeated
one-time actions execute. Permanent errors require attention. A corrupt checkpoint, incompatible frozen version or invalid
configuration is still rejected rather than bypassed. No model fallback is used.
The Runtime stores each accepted receipt durably before acknowledging it and
merges such receipts on resume; interruption does not require re-calling saved
CEOs.

The 0.15.2 patch excludes confirmed length-truncated reasoning-only assistant messages
from outgoing continuations while preserving their original audit events. Recovery
repairs only the exact saved truncated-message/feedback pair and does not renew usage.
The source-bound upgrade procedure is in [RECOVERY_PATCH.md](RECOVERY_PATCH.md).

## Accounting and performance

Attempts, tool calls and available usage are recorded independently of ceilings.
`provider_turns` counts completed, accounting-valid operating replies; file tools
share the same allowance. There is no summary-specific counter.
There is no promise of equal compute across models. Repeated requests may be
billed by the provider; usage and cost reporting are separate from company Score.
CPU/transport/staging/persistence timing belongs in a separate performance log,
not in the deterministic economic event sequence or model input.

A Campaign retains a maximum of thirty simultaneous Runs. Model and resident
Worker concurrency have no default cap; resource samples are observational and
do not reduce admission. Each CEO's conversation and tool ordering remain
sequential and company-bound.

The `scenario` query table and system prompt disclose the sole score formula,
terminal day, remaining days, and decision-window length. `public_prices`
contains list price, promotion, and effective price for active competitors.
Pending stochastic R&D exposes only its tier, start day, median completion day,
expected gain, and status; realized completion and gain appear only afterward.
