# Analysis and figure reproduction coverage

Run the commands below from the supplement root after the installation steps in
the root README. The scripts use released data and require no model API calls.

| Current paper item | Released inputs | Reproduction coverage |
|---|---|---|
| Table 2: operating performance | `data/formal/trajectory_summary.json`, `arena_analysis.json`, `bankruptcy_rates.json` | `verify_formal_results.py` recomputes Scores, means, rankings, bankruptcy and decision counts. |
| Table 3 and Figure 6A/B: private returns and externalities | Formal company outcomes and matched comparisons | `verify_formal_results.py` checks private, aggregate, market, and directed differences for every main seed. |
| Figure 5: operating-performance points | `data/formal/tables/lineup_model_means.csv`, formal analysis JSON | Numeric lineup means and overall means are released. `render_results.py` produces a legacy operating-performance layout, not the final supplied figure artwork. |
| Figure 6C/D: baseline and repeated-run comparisons | `data/supplementary/arena_analysis.json`, `track_c_supplement_audit.json` | `reproduce_robustness.py` regenerates the five-observation statistics and three-control baseline summaries. It does not render the final C/D panels. |
| Terminal-valuation sensitivity | Formal terminal outcomes | `terminal_valuation_sensitivity.py` regenerates ranking and private/market contrast files without terminal inventory salvage. |
| Section 4.4 / Appendix E.1: mechanism accounting | Formal `company_results.csv`, `company_daily.csv`, and `directed_effects_by_seed.csv` | `analyze_mechanisms.py` regenerates and checks model/cohort profiles, aggregate/directed decompositions, and common-active-horizon comparisons. |
| Appendix E.1: selected memory/action evidence | `data/mechanisms/` and formal decision/receipt CSV tables | `build_mechanism_evidence.py` checks all 16 curated note/action records and can regenerate `memory_notes.md`. |
| Figures 3/4 and appendix market cash paths | `data/formal/tables/company_daily.csv`, `model_mean_cash_daily.csv`, assignments and run metadata | The numerical trajectories are included. The final cash-plot rendering code and supplied figure PDFs are not included in this ZIP. |
| Figures 1/2 and other explanatory diagrams | Interface/economic implementation and configuration; selected main-run records | No script in this package regenerates the final diagram artwork. |

## Legacy plotting script

`render_results.py` is retained from the main-evaluation source snapshot. Its
outputs are `figure_2a.pdf` (operating means), `figure_2b.pdf` (the former spread
diagnostic), `figure_3a.pdf` (private/market differences), and `figure_3b.pdf`
(directed differences), plus its historical table exports. Those filenames and
layouts do not correspond to the final paper's figure numbering. In particular,
the spread diagnostic is not a retained main-paper evaluation dimension.

The final artwork was supplied separately. The table above distinguishes numeric
verification from reproducing that artwork; the old renderer is not a complete
renderer for the submitted manuscript.
