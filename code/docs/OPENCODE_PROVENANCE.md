# OpenCode provenance

- Official repository: `https://github.com/anomalyco/opencode.git`
- Default branch at integration audit: `dev`
- Pinned commit: `1be9fd55a9326d5e7b09786195e5669e311e61b4`
- OpenCode package version: `1.18.25`
- License: MIT
- Reused seam: `packages/llm`

The upstream checkout is not modified. `agent_worker` imports the typed LLM
protocol and tool runtime behind a CEO Arena-owned NDJSON interface. Upgrade
only through an explicit provenance bump plus provider, tool-schema, trace, and
replay regression tests.

`agent_worker/src/compatibility.ts` is a benchmark-owned transport adapter for
NewAPI streams. It removes only empty continuation `tool_calls[].id` and
`tool_calls[].function.name` fields, allowing OpenCode to retain the non-empty
identity from the initial chunk. It does not edit upstream OpenCode, alter tool
arguments, or relax missing identity on a tool-call start.
