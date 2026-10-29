# Public release provenance

This release packages the supplementary code and data associated with
[arXiv:2609.34821v1](https://arxiv.org/abs/2609.34821v1).
It preserves the economic simulator, scoring rules, agent policies, and released
experimental data. It does not report new model experiments.

## Source identity

| Artifact | Identity |
| --- | --- |
| Supplement archive SHA-256 | `b1c71f93f566f529eeb40ac3935a025c7fc20faf54d52104b66f5bfc0f61770e` |
| Formal source commit recorded by the supplement | `a39d283fd1c0560ec0b726f288fc0aa206606f15` |
| OpenCode submodule commit | `1be9fd55a9326d5e7b09786195e5669e311e61b4` |
| Python package version | `0.15.2` |
| Public release date | 2026-10-29 |

All 6,730 entries in the supplied archive's checksum manifest were verified.
The original supplement metadata and source README are retained in
[`release/supplement/`](../release/supplement/). Its `MANIFEST.sha256` describes
the original extracted archive, not this repository's repackaged layout.
[`PROVENANCE.json`](../PROVENANCE.json) records the public release source.

## Packaging changes

- Add public installation/model guides, citation metadata, a root MIT license,
  generated-file exclusions, and offline CI.
- Represent the bundled OpenCode snapshot as a Git submodule at the same pinned
  commit. Every file included in the supplement's OpenCode directory matches
  the corresponding upstream file byte for byte.
- Restore `code/configs/default.json` through the existing `ceo-arena init`
  command. Restore the public campaign example from the released protocol and
  recorded route grouping, using explicit nonfunctional credential placeholders.
- Add a small two-model campaign template for integration checks.
- Recognize both `bun` and `bun.exe` in the local Bun installation, including
  npm distributions that use the latter filename on macOS/Linux. The pinned
  version check remains enforced.
- Make three manuscript-only tests explicitly skip when the separately held
  `check_code_alignment` and `validate_scenario` modules are absent. Other
  tests in those files remain enabled.
- Use a pytest temporary directory that can be created in a fresh checkout,
  without depending on an existing `.tmp/` parent directory.

Only `code/src/ceo_arena/harness/bun.py` differs from the supplied runtime source.
The source fingerprint changes, so this checkout must not be substituted into
an existing frozen experiment. The included No Action policy and its protocol
wiring were already present in the supplementary archive.

## Verification scope

Local release validation on 2026-10-29 used macOS arm64, Python 3.13.16,
the locked Python dependencies, and Bun 1.3.14:

| Check | Result |
| --- | --- |
| Python test suite | 528 passed; 3 manuscript-only checks skipped |
| TypeScript worker tests | 167 passed |
| Worker type check | Passed |
| Eight-company rule smoke run and replay | 14 days and 2 windows verified |
| Released-data verification scripts | All five completed successfully |

The GitHub workflow runs the offline suite on Linux. Its status is available
under the repository's Actions tab.

The public offline checks cover Python/worker behavior, a short rule-only market
and exact replay, and the following released-data analyses:

- 27 main markets, 216 company outcomes, operating scores, bankruptcy counts,
  24 paired contrasts, and 168 directed values.
- Five-observation robustness and reference-policy summaries.
- Terminal-valuation sensitivity.
- Accounting identities, matched decompositions, and 12 common-active-horizon
  comparisons.
- All 16 curated memory/action records and their successful write/action receipts.

These checks require no paid model calls. Raw provider conversations, complete
server checkpoints, manuscript sources, and final figure artwork are not part
of this release. See [reproduction coverage](../paper/scripts/README.md).

The secret scan uses the default Gitleaks rules with narrowly scoped exceptions
for literal synthetic test data, public example placeholders, and two recorded
SHA-256 source fingerprints. The reviewed scan found no unresolved credential findings.

## Dependencies and license

CEO Arena uses the supplied [MIT License](../LICENSE). OpenCode remains under
its own MIT license and upstream attribution. Python dependencies are pinned
in [`code/uv.lock`](../code/uv.lock); the source snapshot selects Python 3.13
and requires Python 3.12 or newer. Bun is pinned to 1.3.14.

The inherited development notes document historical campaigns and operational
assumptions. Model names and gateway aliases describe the released experiments;
they are not guarantees about current provider availability or model identity.
