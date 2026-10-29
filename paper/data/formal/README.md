# Audited formal results

This directory is a secret-free projection of the completed 27-run campaign.
It is not a new model experiment or a replacement for the preserved server checkpoints.

- `arena_analysis.json`: typed Track A/B/C and per-model decision health. The canonical analysis hash is `5523b65dc1275c457c7e23eaff55e490a48cbd7c0ba863c67dc4634244a13cd7`.
- `trajectory_summary.json`: all 216 terminal company cases, explicit exit statuses, accounting and committed-action evidence for three cases, checkpoint/trace hashes, and descriptive cash paths. Intermediate cash is not interim official Score.
- `bankruptcy_rates.json`: explicit bankrupt outcomes divided by 24 for each candidate.
- `protocol.json`: allowlisted scientific fields from the final frozen plan. It omits host paths, API key indexes and credentials; it is not an executable campaign spec. The original plan hash is retained as provenance, not claimed to equal the projection's file hash.

All means and score differences originate in integer cents. Paper displays use USD thousands. The three seeds are 11, 29 and 47, and every candidate appears in eight lineups. Crossed lineups and common seeds are not independent model samples. Track C has one same-seat Rule counterfactual per target and seed.

Full independent Exact Replay verified 27 final states, 13,500 days and 1,944 windows without model calls. The portable verifier independently recomputes numerical summaries from these projected cases, but cannot replay omitted full world snapshots.

Trace paths are relative to the private campaign directory; they identify preserved evidence but the raw transcripts are not included here. Provider-private reasoning and credentials are excluded. Usage totals cover retained settled decisions; fresh-attempt archived usage is separate and neither establishes complete billed consumption.

Run `python paper/scripts/verify_formal_results.py` from the simulator repository environment to check the data. `render_results.py` reproduces legacy main-evaluation panels; `paper/scripts/README.md` maps the released data and scripts to the current paper figures and mechanism analyses. No API keys are needed.
