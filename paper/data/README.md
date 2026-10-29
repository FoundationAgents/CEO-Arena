# Released result data

formal/: 27 main-evaluation markets, with 216 company outcomes and daily cash series.
supplementary/: 18 repeated seed-11 markets and eight No Action markets. The five-observation summary combines the formal three seeds and two further seed-11 replicates.
workload/: counts of successfully sealed decisions with complete reported usage; these are recorded counts, not total provider billing.

Amounts in the JSON results are integer cents unless a field explicitly says otherwise. The formal tables/schema.json defines the CSV fields. Run IDs are pseudonymous.

The formal terminal_valuation_sensitivity.csv and terminal_valuation_matched_effects.json are deterministic post-processing of the 216 company outcomes in trajectory_summary.json. Recompute both with paper/scripts/terminal_valuation_sensitivity.py.
