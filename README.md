# CEO Arena

[![Paper](https://img.shields.io/badge/arXiv-2609.34821-b31b1b.svg)](https://arxiv.org/abs/2609.34821)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Official code and released results for **[CEO Arena: Evaluating Long-Horizon Multi-Agent Decision-Making in Competitive Markets](https://arxiv.org/abs/2609.34821)**.

An Yan, Yu Huo, Zhiwei Shang, Yiran Peng, and Chenglin Wu.

CEO Arena evaluates agents that operate competing companies in one shared market. CEOs make decisions about pricing, procurement, marketing, research and development, and service using private company information and noisy market signals. A deterministic economic simulator settles actions and computes returns; evaluation does not use an LLM judge.

The main evaluation runs eight companies for 500 simulated days. Matched replacements compare each LLM CEO with a rule-based CEO in the same company and economic seed, while other agents keep their identities and adapt their decisions. The released results contain **27 main markets and 26 supplementary markets**.

## Quick start: no model API required

Requires Python **3.12 or newer** and Git. Python 3.13 is pinned in the source snapshot.

```bash
git clone https://github.com/FoundationAgents/CEO-Arena.git
cd CEO-Arena
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e './code[dev]'

# Run eight rule-based companies for 14 simulated days.
ceo-arena run-rule --companies 8 --days 14 --seed 11 \
  --output runs/rule-smoke --fresh
ceo-arena replay runs/rule-smoke
```

On Windows, create the environment with `py -3 -m venv .venv` and activate it with `.venv\Scripts\Activate.ps1`. Put each CLI command on one line or use PowerShell continuation syntax.

The smoke run checks installation and replay. It is separate from the paper's 500-day model evaluation.

## Verify the released results

From the repository root, with the environment above active:

```bash
python paper/scripts/verify_formal_results.py
python paper/scripts/reproduce_robustness.py
python paper/scripts/terminal_valuation_sensitivity.py
python paper/scripts/analyze_mechanisms.py
python paper/scripts/build_mechanism_evidence.py
```

These commands use the released data and make no model API calls. They check operating returns, matched effects, robustness summaries, terminal valuation, mechanism accounting, and selected memory/action evidence. See [reproduction coverage](paper/scripts/README.md) for the inputs and limits of each script. Reproducing the final figure artwork is only partially supported; the numerical data and legacy plotting script are included.

## Evaluate a model

LLM runs additionally require Node.js/npm, Bun **1.3.14**, the pinned OpenCode submodule, and access to a compatible model endpoint. Follow [model setup and execution](docs/RUNNING_MODELS.md) for installation, a short single-market run, and campaign configuration.

```bash
# Run this at the repository root before setting up the LLM worker.
git submodule update --init --depth 1 code/OpenCode
```

GitHub's source ZIP does not include submodules. Clone with Git or initialize the submodule as above. The worker verifies the exact OpenCode commit; an unversioned copy of its source is insufficient for model execution.

## Data and code

| Path | Contents |
| --- | --- |
| [`code/src/ceo_arena/`](code/src/ceo_arena/) | Economic simulator, campaign runner, harness, scoring, and dashboard |
| [`code/agent_worker/`](code/agent_worker/) | TypeScript LLM worker and private-memory tools |
| [`code/tests/`](code/tests/) | Offline unit and integration tests |
| [`code/examples/`](code/examples/) | Credential-free campaign templates |
| [`paper/data/formal/`](paper/data/formal/) | 27 main markets and 216 company outcomes |
| [`paper/data/supplementary/`](paper/data/supplementary/) | 18 repeated markets, eight No Action markets, and robustness summaries |
| [`paper/data/mechanisms/`](paper/data/mechanisms/) | Accounting decompositions and 16 selected memory/action records |
| [`paper/data/workload/`](paper/data/workload/) | Recorded decision, tool, and token totals |
| [`paper/scripts/`](paper/scripts/) | Offline verification and analysis |

The score is **terminal cash + salvage value of inventory and paid inbound stock − initial enterprise value**. Monetary fields in released JSON are integer cents unless stated otherwise. CSV field definitions are in [the schema](paper/data/formal/tables/schema.json).

The release excludes credentials, raw provider conversations, and full server checkpoints. Running a new model experiment requires external model access and produces new trajectories. Recorded token totals are not a complete provider billing record.

## Documentation and development

- [Model setup and execution](docs/RUNNING_MODELS.md)
- [Release provenance and validation](docs/RELEASE.md)
- [Architecture](code/docs/ARCHITECTURE.md), [economy](code/docs/ECONOMY.md), and [agent tools](code/docs/AGENT_PROTOCOL.md)
- [Evaluation](code/docs/EVALUATION.md), [memory](code/docs/MEMORY.md), and [budgets](code/docs/COST_CONTROL.md)
- [Reproduction coverage](paper/scripts/README.md)

Some development documents are in Chinese and retain historical experiment notes. Use this README and the release notes for the public package's setup and contents.

After installing the worker dependencies above, run Python checks from `code/` with the environment active:

```bash
cd code
python -m pytest
```

For the locked Python environment, run `uv sync --frozen --extra dev` in `code/`, then use `uv run` for commands. Worker checks are documented in the [model guide](docs/RUNNING_MODELS.md).

## Citation

```bibtex
@article{yan2026ceoarena,
  title = {CEO Arena: Evaluating Long-Horizon Multi-Agent Decision-Making in Competitive Markets},
  author = {An Yan and Yu Huo and Zhiwei Shang and Yiran Peng and Chenglin Wu},
  year = {2026},
  eprint = {2609.34821},
  archivePrefix = {arXiv},
  primaryClass = {cs.MA},
  doi = {10.48550/arXiv.2609.34821},
  url = {https://arxiv.org/abs/2609.34821}
}
```

## License and acknowledgments

CEO Arena is released under the [MIT License](LICENSE). The pinned [OpenCode](https://github.com/anomalyco/opencode) dependency retains its own [MIT license](https://github.com/anomalyco/opencode/blob/1be9fd55a9326d5e7b09786195e5669e311e61b4/LICENSE). See [OpenCode provenance](code/docs/OPENCODE_PROVENANCE.md).

CEO Arena builds on the research direction of the independent [CEO-Bench](https://github.com/zlab-princeton/ceobench-src) project, extending company operation to a shared competitive market.
