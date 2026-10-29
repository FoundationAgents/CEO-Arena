# CEO Arena OpenCode worker

This is a deliberately small, headless Harness worker. It uses only the native
`@opencode-ai/llm` package pinned by `src/provenance.ts`; it does not load the
OpenCode coding-agent prompt, skills, filesystem tools, subagents, fallback
models, sessions, or compaction.

## Setup

1. Install the pinned OpenCode workspace dependencies. The worker uses only
   `packages/llm`, so unrelated native coding-parser lifecycle scripts are not
   required:

   ```powershell
   cd ..\OpenCode
   npx --yes bun@1.3.14 install --frozen-lockfile --ignore-scripts
   ```

2. Type-check the worker from this directory:

   ```powershell
   cd ..\agent_worker
   node .\scripts\typecheck.mjs
   ```

3. Install the pinned direct Bun runtime from the repository root:

   ```powershell
   uv run ceo-arena setup-worker
   ```

4. The Python `OpenCodeHarness` starts one worker per company with the direct
   Bun 1.3.14 executable. It never puts `npx` between the Coordinator and Bun,
   so a timeout can terminate the complete provider-request process tree. It
   passes the API key only through
   `CEO_ARENA_API_KEY`; the
   worker never includes it in stdout, stderr, traces, command arguments, or
   files.

Required environment variables are `CEO_ARENA_API_KEY`,
`CEO_ARENA_BASE_URL`, `CEO_ARENA_COMPANY_ID`, `CEO_ARENA_MODEL`, and
`CEO_ARENA_OPENCODE_COMMIT`, `CEO_ARENA_PARENT_PID`, `CEO_ARENA_INFERENCE` (the frozen,
secret-free inference JSON), and `CEO_ARENA_MEMORY_DIR`. `CEO_ARENA_TRACE_DIR` is optional.

The stdio protocol is newline-delimited JSON. During a decision the worker can
emit a `query` message; Python executes it through the opaque, read-only
`CompanyContext` and returns `query_result`. All economic actions remain in a
local decision draft until `seal_decision` is called.

## Weekly memory and window allowance

The v8 worker follows CEO-Bench's private-file memory lifecycle. Each new week
loads up to 40,000 Unicode characters of MEMORY.md plus factual operating feedback
from `briefing.ts`. Other files persist for on-demand reads. No separate summarizer,
rolling truncation, economic notebook field or mandatory review gate remains.
File writes persist immediately, independently of sealing or draft reset.

`CompanyWorkspace` isolates Run/company files and journals mutations.
`ConversationJournal` restores native messages, exact draft, usage and completed
tool receipts for the same frozen window. It never replays completed siblings.
A new week resets the conversation, not the workspace. See
[MEMORY.md](../docs/MEMORY.md).

`DecisionBudget` retains 32 replies / 131,072 generated tokens / 60 active minutes.
28 replies / 98,304 tokens / 45 minutes triggers at most four closing replies.
See [COST_CONTROL.md](../docs/COST_CONTROL.md).

## Decision and repair contract

- Economic mutations report `staged: true, executed: false`; settlement results are read
  from `action_receipts`, not inferred from staging success.
- Capacity tools use the environment's public `capacity_action_quotes`.
  `expand_capacity`, `downsize_capacity`, and `cancel_capacity_project` replace
  one another in a draft. Lower-tier expansion is rejected, not silently ignored.
- `preview_decision` combines one-time charges/refunds and current daily policy
  costs. It is not a future cash-flow forecast: purchases, holding, failures and
  future capacity changes are excluded. An unaffordable `seal_decision` returns
  a tool error and keeps the draft editable.
- `reset_decision` discards unsealed changes when unwanted one-time commitments
  must be removed; it does not change existing policies, erase files or restore budgets.
- A provider turn containing both sealing and other calls is rejected in full;
  none of that turn's calls execute. Text-only turns receive a correction.
  Repairs consume the same remaining turns, tools, tokens and timeout as the
  original decision window, with every rejected call counted.
- Missing output-token usage or a failed provider stream is `provider_error`,
  never an automatic economic no-op. No token usage is estimated or invented.
