# Running model experiments

First complete the Python installation in the [README](../README.md). The commands below assume the repository root and an active Python environment. Model runs require a compatible API endpoint and incur provider costs.

## 1. Install the worker

Requires Git and Node.js/npm. Bun is installed locally at the version expected by the harness.

```bash
git submodule update --init --depth 1 code/OpenCode
ceo-arena setup-worker
cd code/OpenCode
../agent_worker/.tools/node_modules/.bin/bun install --frozen-lockfile --ignore-scripts
cd ../..
```

On Windows, invoke the local npm shim with the `.cmd` suffix. The upstream workspace contains unrelated applications; `--ignore-scripts` avoids running their lifecycle hooks. Do not update the submodule to its latest branch: the harness requires commit `1be9fd55a9326d5e7b09786195e5669e311e61b4` and a clean tracked checkout.

If recent npm versions report that Bun's install script was skipped, install and approve that pinned package in the local tools directory, then repeat `ceo-arena setup-worker`:

```bash
npm install --prefix code/agent_worker/.tools --save-exact --ignore-scripts bun@1.3.14
npm --prefix code/agent_worker/.tools install-scripts approve bun
npm --prefix code/agent_worker/.tools rebuild bun
```

This approval applies to the local Bun package only. It does not change the upstream workspace or the user's global npm configuration.

Check the worker without model calls:

```bash
node code/agent_worker/scripts/typecheck.mjs
cd code/agent_worker
.tools/node_modules/.bin/bun test ./src
cd ../..
```

## 2. Run one short market

For a model exposed through an OpenAI-compatible endpoint, set credentials in your local shell. This example uses `read` to avoid recording the API key in shell history.

```bash
export CEO_ARENA_NEWAPI_BASE_URL="https://your-provider.example/v1"
read -s CEO_ARENA_NEWAPI_API_KEY
export CEO_ARENA_NEWAPI_API_KEY

ceo-arena run-lineup --participant your-model-id --participant rule \
  --days 14 --seed 11 --output runs/model-smoke --fresh
ceo-arena replay runs/model-smoke
```

The single-market example tests integration. It does not reproduce the paper's eight-company evaluation. The CLI resolves known model IDs to their frozen inference profiles; unknown IDs default to compatible Chat. Use a campaign for explicit protocol, token-limit, and identity settings.

## 3. Configure a campaign

```bash
mkdir -p .ceo-arena/campaigns
cp code/examples/campaign.quickstart.toml .ceo-arena/campaigns/my-models.toml
```

Edit the local copy with your endpoint, model IDs, and credentials. `.ceo-arena/` is ignored by Git. Each `[[api]]` contains one API key and its model routes. All routes share the configured base URL. The provider must support the protocol selected for each model.

- `company_count` must equal the number of provider models. The Rule CEO is added automatically.
- With N provider models and S seeds, the complete matched-replacement design contains `(N + 1) × S` markets.
- `model_order` fixes company assignments across lineups.
- Inference table names must match the model IDs. Set `expected_model_ids` only to identities your provider is expected to report.
- Use a new campaign ID and output directory when changing models, economic parameters, or inference settings.

Compile and inspect a plan offline:

```bash
ceo-arena campaign plan --spec .ceo-arena/campaigns/my-models.toml \
  --output .ceo-arena/plan.json
```

Then launch the model experiment:

```bash
ceo-arena campaign run --spec .ceo-arena/campaigns/my-models.toml \
  --output runs/my-models
```

The quick-start template uses 14 days, one seed, two provider models, and conservative concurrency. Its inference settings are for integration testing. The [main-evaluation template](../code/examples/campaign.example.toml) records the historical eight-model panel, 500-day horizon, three seeds, and inference settings from the released protocol. Historical gateway aliases are documented assumptions; verify them before using a different provider. Availability of those model versions is not guaranteed.

## 4. Inspect and resume

```bash
ceo-arena dashboard --port 8766
ceo-arena campaign status --output runs/my-models
ceo-arena campaign resume --spec .ceo-arena/campaigns/my-models.toml \
  --output runs/my-models
```

Open <http://127.0.0.1:8766/> for the local dashboard. Starting the dashboard does not call models; its Run and Resume controls do. Resume uses the same frozen configuration and code. Preserve checkpoints, journals, memory, and usage records together.

See the snapshot's [running guide](../code/docs/RUNNING.md), [inference panel](../code/docs/FORMAL_MODEL_PANEL.md), and [budget contract](../code/docs/COST_CONTROL.md) for details. The default decision window permits up to 32 model replies, 131,072 reported output tokens, and 60 active minutes. Offline replay checks recorded execution; it does not repeat provider calls.
