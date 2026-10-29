# CEO Arena benchmark source

This directory contains the Python benchmark and TypeScript model worker for
[CEO Arena](https://arxiv.org/abs/2609.34821).

Start with the [repository README](../README.md) for installation, a model-free
smoke run, released results, and citation information. See the
[model guide](../docs/RUNNING_MODELS.md) to install OpenCode/Bun and evaluate models.

## Source map

| Directory | Responsibility |
| --- | --- |
| `src/ceo_arena/campaign/` | Compile lineups and schedule persistent multi-market runs |
| `src/ceo_arena/runtime/` | Coordinate decisions, checkpoints, replay, and budgets |
| `src/ceo_arena/economy/` | Settle the shared market and company state |
| `src/ceo_arena/harness/` | Rule, No Action, and OpenCode worker adapters |
| `src/ceo_arena/evaluation/` | Compute operating returns and matched effects |
| `src/ceo_arena/dashboard/` | Inspect and control local campaigns |
| `agent_worker/` | Execute model conversations, tools, and private memory |
| `configs/`, `examples/` | Public economic configuration and credential-free templates |
| `tests/` | Offline tests, including mocked provider execution |
| `OpenCode/` | Pinned third-party Git submodule |

Execution flows from CLI/dashboard to Campaign, Coordinator, CEO agents, and
MarketWorld. Only the economic engine changes market state; tools stage decisions
until the coordinator settles the shared decision window.

Development documents are in [`docs/`](docs/), including
[architecture](docs/ARCHITECTURE.md), [economic rules](docs/ECONOMY.md),
[evaluation](docs/EVALUATION.md), and [OpenCode provenance](docs/OPENCODE_PROVENANCE.md).
Some retain historical experiment notes. The original source README is preserved
under [`release/supplement/`](../release/supplement/code-README.md).

Version: `0.15.2`. License: [MIT](LICENSE).
