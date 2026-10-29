# Reference calibration: ceos-market-v3

The current environment is a standardized-product, inventory-based operating
scenario, not an econometric estimate for an industry. Parameters address the
pilot's coarse capacity, weak continuous-development payback and static demand.
No evaluated LLM is called to select a named-model ranking or Score sign pattern.

## Current v3 parameters

Money below is in dollars; implementation uses integer cents. Canonical default
scenario SHA-256:
`f5a858f1bd7b0d6cd7e354f40e2db36c4e0df4a6a2299f42ca52dd1511e908fe`.

| Block | Current values and rationale |
|---|---|
| Horizon/window/cash | 500 days / 7 days / $500,000, unchanged |
| A/B/C prices and costs | $40/$75/$135 and $26/$48/$85, unchanged gross margins |
| Stock/lead/salvage/holding | Original inventory quantities, lead times and liquidation accounting retained |
| Base opportunity pool | 240 value + 300 mainstream + 150 premium = 690/day; configured for nine firms, not reduced on bankruptcy |
| Daily capacity | 60 / 90 / 135 / 200 units, smaller useful increments |
| Daily capacity overhead | $400 / $530 / $730 / $1,000 |
| Cumulative installed-capital prices | $0 / $12,000 / $30,000 / $60,000; expansion pays target minus current capital |
| Target construction delay | 1 / 21 / 35 / 49 days |
| Downsize | $1,000 per tier; 7-day adjustment; no refund of installed capital |
| Cancel unfinished expansion | 10% nonrefundable startup; remaining cost consumed linearly; 5% cancellation fee withheld from unspent cash |
| Continuous development | 7-day delay; paid contribution converts to 2.0 times knowledge capital |
| Knowledge quality | scale 0.18, reference $50,000, depreciation 0.00045/day |
| Discrete innovation | Costs, duration medians, gain distributions and equal product allocation unchanged |
| Marketing/support | Existing maintenance, queue and diminishing-return mechanisms retained |
| Reliability | Baseline 3%, floor 0.1%, spend scale $60/day; $12 remediation per realized customer failure, without also reversing sales |
| Enterprise contracts | 7-day bidding + 7-day delivery window; 10% penalty on unfulfilled value; cumulative delivery capacity accounts for prior obligations |
| Macro | Reversion 0.02/day, innovation SD 0.0016/day; separate from market phases |

## Persistent shared phases

Start in balanced. Each day, remain in the current phase with probability 59/60;
move to each other phase with probability 1/180. Mean duration is 60 days, not a
fixed reset schedule. Named seed streams do not read identities, actions or Scores.

| Phase | Demand multiplier | Value/mainstream/premium mix weights | Price/quality/service weight multipliers | Quality requirement shift |
|---|---:|---|---|---:|
| balanced | 1.00 | 1/1/1 | 1/1/1 | 0 |
| price pressure | 0.90 | 1.5/1/0.7 | 1.08/0.90/1 | 0 |
| quality growth | 1.10 | 0.8/1/1.4 | 0.95/1.25/1 | 0.035 |
| delivery peak | 1.35 | 1/1/1 | 0.97/1/1.35 | 0 |

Mix weights are normalized against baseline demand. An additional quality
frontier drifts 0.00015/day, capped at 0.075. Public indexes lag seven days;
market research freezes request-day truth and returns a delayed noisy report.
These phases and their magnitudes are design choices, not empirically estimated
business-cycle durations. Formal results must test sensitivity, not claim realism
from the chosen functional form alone.

## Current validation and evidence limits

The separate fixed-Rule-v3 parameter search leaves all economic coefficients
unchanged: 24 playbooks on seeds 7/19/42, then the frozen winner on 211/307/401.
Only the focal company varies against eight fixed heterogeneous Rule opponents.
These 75 worlds concern baseline selection, not formal model rankings; see
[RULE_BASELINE.md](RULE_BASELINE.md). The older checks below retain their provenance.

See [historical validation](HISTORY.md) and
`scripts/validate_economy.py` for source-hashed 500-day scripted-policy reports.
The archived source-v3 checks used Rule v1; they are not measurements of the
current fixed Rule v3. Each counterfactual world recomputed all opponents' actions. Revenue,
cash payments, COGS and refunds remain separate and cash must reconcile.

Development seeds are 7/19/42; 73/101 are reserved from v3 selection. They appeared
in historical v2 calibration, so they are not globally unseen data. Inspect a
report's requested cases, completion flag and source hashes before claiming that
all seeds or policies completed. Results are model-free diagnostics, not LLM rankings.

Unit/integration checks cover seeded phase independence, bounded drift, hidden
research truth, non-finite configuration rejection, capacity refund anti-arbitrage,
quote/execution consistency, enterprise deadlines and reservations, isolated
snapshots and exact crash/recovery parity. They do not establish cross-industry
validity or make every investment profitable. Rational restraint remains valid.

The archived v2 record and `paper/research/model_free_validation.*` are historical;
reproduce them from checkout `d1c8377`. Do not overwrite them with v3 numbers.
The bilingual manuscript describes integrated economic v3 and fixed Rule v3;
its versioned historical diagnostics are not relabeled as integrated outcomes.
PDF packages must be rebuilt from the synchronized sources. Current integration
acceptance is tracked in [current maintenance checks](MAINTENANCE.md).

## Historical evidence

The v2 coefficient tables and old Rule-v1 validation are preserved in the
[historical archive](HISTORY.md), not current parameter recommendations.

## Sources

- [Price-elasticity meta-analysis](https://doi.org/10.1509/jmkr.42.2.141.62296)
- [Advertising-elasticity meta-analysis](https://doi.org/10.1509/jmkr.48.3.457)
- [Walmart 2025 Form 10-K](https://www.sec.gov/Archives/edgar/data/104169/000010416925000021/wmt-20250131.htm)
- [Dillard's 2024 annual report](https://www.sec.gov/Archives/edgar/data/28917/000110465925032232/tm252820d2_ars.pdf)
- [JD.com 2023 annual report](https://www.sec.gov/Archives/edgar/data/1549802/000119312524099926/d784709dex991.pdf)
- [ASCM inventory carrying-cost definition](https://learn.ascm.org/sfc/servlet.shepherd/document/download/069R3000006PlDHIA0?operationContext=S1)
- [Retail stockout study summary](https://www.fmi.org/newsroom/news-archive/view/2002/10/14/new-industry-study-explores-root-causes-of-global-out-of-stock-dilemma)
- [U.S. Census retail sales through FRED](https://fred.stlouisfed.org/series/MRTSSM4400AUSS)
