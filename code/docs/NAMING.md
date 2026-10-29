# CEO Arena naming contract

| Surface | Canonical name |
| --- | --- |
| Paper, dashboard, documentation | **CEO Arena** |
| Python distribution and CLI | `ceo-arena` |
| Python import package | `ceo_arena` |
| Agent worker package | `@ceo-arena/opencode-agent-worker` |
| Local configuration directory | `.ceo-arena/campaigns/` |
| Environment variable prefix | `CEO_ARENA_` |
| Dashboard control header | `X-CEOArena-Control` |

The dashboard's interface, accessibility labels, notifications and generated
decision summaries use English. Numbers use English formatting; timestamps use
`YYYY-MM-DD HH:mm:ss` in **UTC+8**, explicitly labelled in the footer. Model IDs,
run IDs and original diagnostic messages remain faithful to their source data.

Run `uv sync --extra dev` to install the renamed console entry point in a fresh
environment. The equivalent module entry point is `python -m ceo_arena.cli`.
Local TOML files have been copied to the new configuration directory without
changing model routes or credentials. These files remain excluded from Git.

## Audit and running-experiment boundaries

This section records the naming revision. Subsequent workspace cleanup moved
inactive artifacts as complete bundles into the local [archive](HISTORY.md),
without rewriting their contents or moving actively used dependencies.

This is a naming and presentation change, not a change to economic rules,
parameters, budgets, model selection or scoring. New source fingerprints change
because module names and paths change; do not use them to resume a frozen run
under a different implementation.

The physical checkout directory, active launchers, frozen source snapshots,
existing virtual environment, raw trajectories, and historical hash-bearing
validation records are not renamed or rewritten. Active experiments continue
with their original frozen implementation. Persisted protocol identifiers such
as `ceos-decision-v9` remain protocol serialization identifiers, not product names.

**CEO-Bench** is the distinct upstream research project. Its title, repository
URL and bibliographic citations must remain unchanged. Historical provenance is
evidence, not current branding; do not rewrite it to imply a new implementation
generated earlier results.
