# CEO Arena: anonymous supplementary code and data

This ZIP accompanies the ICLR 2027 submission. It contains the frozen main-evaluation source snapshot, the No Action CEO policy added for the reference-policy check, compact results for 53 completed markets, and scripts for checking the reported analyses. It contains no API credentials, raw provider conversations, raw discarded-attempt logs, or full server checkpoints.

Contents
- code/: formal benchmark source and its pinned OpenCode dependency. Three No Action files and minimal protocol/export/route wiring were added. The economic simulator, scoring rules, and formal Rule CEO policy are unchanged.
- paper/data/formal/: 27 main-evaluation markets, including per-company outcomes and daily cash tables.
- paper/data/supplementary/: 18 repeated seed-11 markets and eight No Action markets, plus the five-observation robustness summaries.
- paper/data/workload/: recorded decision, tool, and token totals.
- paper/data/mechanisms/: main-evaluation accounting decompositions and 16 selected memory/action examples with successful file-write receipts.
- paper/scripts/: offline analysis and verification scripts.

Offline checks (Python 3.12 or newer)
1. Install the benchmark: python -m pip install -e ./code
2. Install check dependencies: python -m pip install pytest pytest-asyncio matplotlib
3. Test the No Action policy: python -m pytest code/tests/harness/test_noop_ceo.py --basetemp=.pytest-tmp
4. Check the formal summaries: python paper/scripts/verify_formal_results.py
5. Recompute robustness summaries: python paper/scripts/reproduce_robustness.py
6. Recompute terminal-valuation sensitivity: python paper/scripts/terminal_valuation_sensitivity.py
7. Recompute and verify mechanism accounting: python paper/scripts/analyze_mechanisms.py
8. Verify memory/action evidence: python paper/scripts/build_mechanism_evidence.py

See paper/scripts/README.md for figure and table reproduction coverage. The mechanism notes are selected agent-written files, not raw provider conversations.

The included source supports the formal benchmark and the No Action policy. The result files support offline verification of the repeated and baseline comparisons; exact regeneration of paid model trajectories also requires external model access and the corresponding execution environment. OpenCode is a third-party dependency pinned to the commit in PROVENANCE.json, with its own LICENSE in code/OpenCode.

The attached paper PDF already contains the text appendix. This ZIP contains code and result data rather than a second copy of the manuscript. See MANIFEST.sha256 for file hashes.
