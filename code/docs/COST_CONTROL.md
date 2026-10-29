# Window cost controls (decision v9)

Package `0.15.2` retains the v8 computational allowance and
[CEO-Bench-style file memory](MEMORY.md). Economic equations, fixed Rule operating
parameters, terminal Score and Campaign concurrency are unchanged.

| Per-company, per-window constraint | Start finalization | Hard allowance |
|---|---:|---:|
| Accounting-valid model replies | 28 | 32 |
| Cumulative generated tokens | 98,304 | 131,072 |
| Active execution time | 2,700 seconds | 3,600 seconds |

The first threshold reached starts finalization: up to four further replies within
the original hard limits. query_company is rejected by the dispatcher, while its
definition stays unchanged to preserve signed prefixes. Operating tools, private files, preview, reset and sole-call seal
remain available. No auxiliary model or Rule chooses the model's actions.

Requests are bounded by phase remaining output and any lower native response limit.
Reasoning is already part of output usage, not a second charge. Rejected calls and
model errors consume the same allowance. There is no independent summary request.

Per-model reasoning and response ceilings are frozen explicitly; see
[FORMAL_MODEL_PANEL.md](FORMAL_MODEL_PANEL.md). Doubao now uses
`max_completion_tokens` (thinking plus answer), capped at 65,536, not the old
32,768-token `max_tokens` branch. Qwen uses the same total-output field. GPT and
Claude use a 128,000 ceiling; Gemini uses 65,536; DeepSeek uses 393,216 but is
always constrained by the smaller remaining window allowance. No model receives
additional cumulative tokens because its native maximum is larger.

The soft deadline cancels an unfinished response and enters finalization with the
valid conversation and draft. The hard deadline records budget_exhausted if unsealed:
prior policies continue, the draft is not executed, and private files remain saved.
Infrastructure failure is separate, never disguised as a successful model hold.

## Active time and recovery

Queue time and manual pause are excluded. Requests, tool processing and automatic
recovery backoff in both the Worker and Coordinator consume the same clock. IPC cleanup grace
is for reporting/termination, not additional model reasoning. Run concurrency remains
at most 30, with no model-concurrency cap.

Runtime `allowances/` records reported failed-attempt usage. The Worker's private
conversation checkpoint also stores native messages, draft, pending tool receipts,
error counts and usage. Restart reconciles both views without renewing replies,
generated tokens, closing replies or known active time. Saved terminal decisions
require no further model request.

provider_turns counts accounting-valid replies; finalization_turns is a subset.
budget_usage_incomplete marks missing partial-stream usage. Transient retry count has
no separate cap, but retries cannot renew the window allowance. Exhausted infrastructure
recovery records `timeout` with its failure metadata: unsealed changes are not applied,
standing policies continue, and the barrier may settle. This is not a successful seal.
Permanent HTTP, identity, accounting or persistence failures stop the affected Run for
attention instead of being retried indefinitely. See [RECOVERY_PATCH.md](RECOVERY_PATCH.md).

## Evidence limits

This is not a dollar cap or an input-context limit. Large tool/file results still
cost input tokens. Providers may omit usage or keep processing after disconnect;
crashes can lose unreported work. Exactly-once billing is not claimed. Report actual
cost/latency separately from Score.

Primary traces retain their 64 KiB/event and 16 MiB/file truncation markers. The
latest conversation snapshot is independent of audit display limits. Offline tests
verify contracts, not guaranteed profit or savings. Freeze new v9 Runs; existing
compatible economic checkpoints require an explicit recorded source migration, not relabeling.
