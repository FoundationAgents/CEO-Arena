# Harness setup

`RuleCeoHarness` and `OpenCodeHarness` structurally implement
`ceo_arena.runtime.ports.AgentHarness`:

```python
result = await harness.decide(request, company_context)
```

`CompanyContext` is issued and revoked by the coordinator. The OpenCode worker
can query it through NDJSON RPC, but neither the model nor its tools can supply
or change `company_id` or `world_version`.

## RuleCEO

```python
from ceo_arena.harness import RuleCeoHarness

harness = RuleCeoHarness()
```

The Fixed Single-Market Retail CEO uses a fixed price book, stock targets and
spending package. Development targets one product; advertising targets one
segment. A cash-floor gate stops or restores those two optional budgets;
reliability and support stay fixed. There is no online price matching, demand
forecast, budget optimization, enterprise bidding, research or capacity change.
It uses authorized information and the ordinary sealing/economic rules.
See [the complete policy and parameter search](../../../docs/RULE_BASELINE.md).

The packaged `rule_profile.json` supplies the selected default parameters.
Explicit `RuleCEOConfig` instances support reproducible offline searches.
The `rule-ceo-fixed-v3-<hash>` identity binds every configuration field.
Never continue a reactive-v1/v2 trajectory with fixed-v3 decisions.

## LLM private memory

The v8 worker uses CEO-authored MEMORY.md and a private persistent directory.
Each new week loads up to 40,000 characters and a factual briefing. The current
conversation is checkpointed for recovery without summaries. File writes survive
reset and budget exhaustion. No mandatory review gate remains. See
[MEMORY.md](../../../docs/MEMORY.md).

The allowance remains 32 replies / 131,072 generated tokens / 60 active minutes,
with early finalization and at most four closing replies. Rule operating parameters
and settlement are unchanged. See [COST_CONTROL.md](../../../docs/COST_CONTROL.md).

## OpenCode native LLM worker

Install the pinned OpenCode workspace dependencies as described in
`agent_worker/README.md`, then construct:

```python
import os
from pathlib import Path

from pydantic import SecretStr

from ceo_arena.harness import OpenCodeHarness, OpenCodeHarnessConfig

harness = OpenCodeHarness(
    OpenCodeHarnessConfig(
        # Use the actual OpenAI-compatible API root, not a key-management page.
        base_url="https://newapi.example/v1",
        model="upstream-model-id",
        api_key=SecretStr(os.environ["NEWAPI_API_KEY"]),
        trace_directory=Path("runs/traces"),
    )
)
```

Call `await harness.aclose()` after the experiment. Each company gets its own
long-lived worker process. The API key is passed only through that process's
environment and is redacted from errors and traces. Worker startup refuses an
OpenCode checkout whose commit differs from
`1be9fd55a9326d5e7b09786195e5669e311e61b4`.
