# Candidate speed screening

This operational pilot is separate from the formal leave-one-out Arena. It does not
estimate capability equivalence or select models by their economic Score.

- Three candidates: `qwen3.8-flash`, `deepseek-v4-flash`, `glm-5.3-flash`.
- Each independent world: seed 42, nine companies, candidate in company-8 and eight
  separate instances of the unchanged Rule policy.
- Copy the reference Run's economic configuration and decision budget. Preserve
  its 500-day horizon and stop after committing day 28. Do not send a fifth decision.
- Freeze the implementation before running. Keep credentials in the existing
  local TOML; record model-to-key indices, never keys, in the screening plan.
- `RunConfig.allow_repeated_rule` permits explicit Rule replication only. Formal
  Campaign validation still rejects duplicate model identities.
- `BenchmarkCoordinator.run(stop_after_day=28)` leaves a resumable committed
  checkpoint, not a fictitious completed 500-day experiment.
- `performance.jsonl` records `decision_total` using a monotonic clock, including
  worker startup, tool calls, provider retries/backoff and decision persistence.
- Three runs execute concurrently. Each closes its own workers on completion.
- Verify the committed 28-day prefix with Exact Replay and the saved Agent trace
  hashes. Retain checkpoints, decision receipts, raw traces, timing and usage.
- Rank only candidates with all four windows genuinely `sealed`, complete timings
  and successful replay. Budget-exhausted fallback, missing windows and failed
  replay do not qualify. Legitimately sealing an unchanged policy is allowed.

`scripts/screen_candidate_models.py` accepts `--reference`, `--credentials`,
`--output`. Without `--run`, it only validates the catalog and freezes the plan;
`--run` explicitly enables paid inference. Execute from an immutable snapshot for
an actual comparison. Each candidate produces `timing.json`; the combined output
contains `report.json` and `report.md` with four weekly durations, their arithmetic
mean and the fastest eligible candidate. Four-window results are a speed screen,
not a guarantee of latency in a 500-day multi-model market.
