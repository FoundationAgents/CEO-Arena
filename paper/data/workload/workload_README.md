# Experimental scale and recorded workload

These tables support Appendix F.7. They cover 27 main-evaluation markets, 18 repeated markets, and eight No Action replacement markets. Each market runs for 500 days with eight firms. Rule parameter search is a separate set of 75 nine-firm markets.

## Accounting scope

Inference totals use only final, successfully sealed decision records with complete reported usage (`agent_status == "sealed"` and `budget_usage_incomplete == false`). Non-sealed records, incomplete-usage windows, and separately archived discarded attempts are excluded. Counts show the recorded workload in this subset, not total billed consumption.

`all_windows` is the complete number of settled company decision opportunities, including non-sealed outcomes. `counted_windows` is the subset used for interaction and token totals. A provider turn is a counted model reply; one reply may request multiple tools. Tool calls are the recorded requested calls, not a count of successful economic actions. Total tokens equal normalized input plus output; cached input and reported reasoning output are not added a second time.

## Files

- `experiment_scale.csv`: experiment-group scale; `evaluation_total` is a subtotal, not another group.
- `agent_usage_by_run.csv`: one row per company trajectory, including group, run, seed, replicate, controller, decision counts, and recorded usage. There are 424 rows. `replicate` is the index within each campaign; the main and repeated groups distinguish the original seed-11 observation from repetitions.
- `usage_by_model.csv`: totals across trajectories and the mean/sample SD of counted LLM turns per trajectory, including trajectories that ended in bankruptcy.

Main and robustness data retain their original score-analysis weights. This workload aggregation does not change scores, rankings, or effect estimates. Shared all-LLM controls are counted once; Exact Replay adds no model calls.

Sources are the final checkpoints in the archived formal and repeated/baseline campaign databases. The main-evaluation complete receipt totals were reconciled with `data/formal/trajectory_summary.json`. Rule-search scale is from its existing verified 75-world report.
