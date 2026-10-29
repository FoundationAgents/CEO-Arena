# CEO-Bench-style file memory

Decision v8 follows the memory behavior of CEO-Bench at
[commit d2b7b32](https://github.com/zlab-princeton/ceobench-src/blob/d2b7b32e5301a571b77f5f68bd1032adbcd5b464/src/saas_bench/agents/bash_agent/agent.py#L166).
The provider adapter remains OpenCode's pinned native LLM package, not the entire
CEO-Bench terminal Harness.

## Weekly lifecycle

1. Each new shared-market window starts a fresh conversation: system instructions,
   the first **40,000 Unicode characters** of MEMORY.md and a factual dashboard.
   Oversized memory is explicitly marked; the complete file remains readable.
2. The CEO reads/writes its private files. Writes persist immediately, independently
   of seal_decision, reset_decision and budget exhaustion.
3. The current week retains original native messages and tool results. There is no
   automatic summarizer, rolling truncation, smaller note limit, fixed template
   or mandatory memory-update gate.
4. Only the next shared-world window clears conversation history. Files remain;
   detailed files are read on demand, not automatically injected.

The prompt recommends strategy, evidence, lessons, pending plans and next checks.
This is advice, not a structural or quality test. Memory is not economic state;
file writes cannot change cash, hidden information, competitors, time or Score.

## Storage and tools

Production defaults to a sibling of agent-traces:

```text
<run>/agent-memory/<sha256([run_id, company_id])>/
  files/MEMORY.md
  files/notes/...                 # CEO-authored files
  runtime/conversation.json      # latest window; invisible to the CEO
  runtime/file-receipts/*.json    # recovery intents; invisible to the CEO
```

Tools: `read_file(path, offset?, limit?)`, `write_file(path, content)`,
`edit_file(path, old_string, new_string)`, `search_files(pattern, path?, glob?)`,
and `glob_files(pattern)`. Reads are line-numbered; edits require one unique old
string. Search examines at most 100 files and returns up to 200 matches; glob
returns up to 200 paths, following the reference design.

Files are UTF-8. Scripts may be stored but are not executed. No shell, network,
arbitrary MCP or automatic operating-script execution is added. Paths cannot escape
a company's files through traversal, absolute/device paths, alternate streams or links.
Credentials and native snapshots are outside the model-visible root. The dashboard
ignores manifests inside the default agent-memory tree.

`OpenCodeHarnessConfig.memory_directory` overrides the storage root; Python passes
it as `CEO_ARENA_MEMORY_DIR`. Every Run/company is separately namespaced. A Harness
with neither trace nor memory storage uses temporary files for offline/probe use;
configure persistent storage for resumable experiments.

## Recovery and accounting

`ConversationJournal` saves each paid response before its tools, then records
completed tool results and the exact draft. Same-window recovery resumes unfinished
siblings, not completed tools or previously saved model replies. File mutations
persist write intents, allowing recovery after an edit but before its tool receipt
without applying the edit twice. Saved terminal results require no new model call.
Native signatures and encrypted reasoning are preserved rather than summarized.

Snapshots bind protocol, model, company, frozen observation and budget. Corrupt or
conflicting snapshots are rejected, never silently reset. A new world version starts
fresh and replaces the previous conversation snapshot. Saved and runtime-reported
usage/error counters are reconciled monotonically; results contain only the
not-yet-reported delta. Unreported requests may still be billed: exactly-once billing
is not guaranteed.

The [window allowance](COST_CONTROL.md) remains 32 replies / 131,072 generated tokens /
60 active minutes, with up to four closing replies. Memory tools share that allowance.

Legacy economic notebook fields, set_notebook, mandatory-review checks, rolling
summaries and summary-specific counters have been removed. Economic equations and
the fixed Rule's operating parameters remain unchanged. Persistence is now
ceos-runtime-v4 because company state no longer contains memory. Historical
experiments and frozen snapshots are preserved, not migrated.
