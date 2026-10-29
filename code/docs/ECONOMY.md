# Economic specification

This document is normative for `ceos-market-v3`. Money is integer cents,
physical flows are integer units, and one simulation step is one day. All
companies begin with identical endowments and face one shared market.

## Shared customer market and pricing

For segment `s` on day `t`, the finite purchase-opportunity pool is

```text
phase[t+1] ~ Categorical(transition_matrix[phase[t]])
weight[s,t] = base_demand[s] × phase_segment_weight[s,t]
phase_demand[s,t] = sum(base_demand) × phase_demand_multiplier × weight[s,t] / sum(weight)
D[s,t] ~ Poisson(phase_demand[s,t] × season[s,t] × macro[s,t]).
```

The opportunity scale is configured before a run; bankrupt firms do not remove
customers from the market. Phases change price, quality and service weights and
quality expectations for all firms identically, using a named seed stream that
never reads model identities, actions or Scores. Quality expectations receive a
bounded daily frontier drift plus a phase offset. CEOs receive only lagged
aggregate demand/quality indexes, not hidden phase IDs or future transitions.

For company-product offer `(i,j)`, effective price is list price minus
promotion and utility is

```text
U[i,j,s,t] = quality_weight[s,t] × (quality[i,j,t] - expected_quality[s,t])
           + service_weight[s,t] × satisfaction[i,s,t]
           + awareness_weight[s] × log(awareness[i,s,t])
           + loyalty_weight[s] × loyalty[i,s,t]
           - price_weight[s,t] × effective_price[i,j,t] / reference_price[s]
           + product_match[s,j].
```

The standard multinomial-logit probability, including an outside option, is
`exp(U[a]) / (exp(U[outside]) + Σ exp(U[b]))`. Consumers jointly compare every
available offer; stockouts can switch to the remaining choice set or become a
backlog order. This follows the discrete-choice foundation of
[McFadden (1974)](https://escholarship.org/uc/item/0p99072q). The implementation
is MNL, not random-coefficient mixed logit.

## Marketing

Segment-specific channel effectiveness first converts spend into impact:

```text
impact[i,s,t] = (Σ channel_effectiveness[s,c] × spend[i,s,c,t] / spend_reference)^α
attention[i,s,t] = impact[i,s,t] / (outside_attention + Σ impact[k,s,t])
goodwill[i,s,t+1] = max(0, (1-decay) × goodwill[i,s,t] + gain × attention[i,s,t])
awareness[i,s,t+1] = 1 - exp(-goodwill[i,s,t+1]).
```

Thus advertising has diminishing returns, competes for finite attention, and
carries over with decay, following the goodwill-stock idea of
[Nerlove and Arrow (1962)](https://doi.org/10.2307/2551549).

## Product development and R&D projects

Daily development spend matures after a fixed delay. Knowledge capital follows
a perpetual-inventory law and quality has diminishing returns:

```text
matured daily contribution = paid_daily_budget × development_capital_multiplier
K_after_daily_completion_and_decay = (K_before + matured_contributions + completed_project_gain) × (1-δ)
quality[i,j,t] = base_quality[j] + scale × log(1 + K[i,j,t] / capital_reference).
```

Completed projects split their knowledge gain equally across product lines;
daily development targets its chosen product. Current spend matures after its
configured delay; the exact daily ordering is completion, depreciation, new
cash charges and pipeline contributions, then customer selection.

Discrete tier projects draw a lognormal duration and a mean-one lognormal gain
multiplier. Pending projects reveal only tier, start day, median completion day,
and expected gain; realized duration and gain remain server-private until
completion. The knowledge-capital interpretation follows
[Griliches (1979)](https://www.nber.org/system/files/chapters/c8340/c8340.pdf).

## Inventory, capacity, backlog, and enterprise sales

Procurement is an order-up-to policy with deterministic lead time:

```text
inventory_position = on_hand + inbound - enterprise_reserved
order = min(max(0, reorder_target - inventory_position), cash / unit_cost).
```

This is the benchmark's simple base-stock control, grounded in the classical
inventory-policy line of [Arrow, Harris, and Marschak
(1951)](https://www.or.mist.i.u-tokyo.ac.jp/takeda/FreshmanCourse/ArrowHarrisMarschak.pdf).
Daily fulfillment cannot exceed on-hand unreserved inventory or installed
capacity. Backlog is always nonnegative:

```text
Backlog[t+1] = max(0, Backlog[t] + Orders[t] - Fulfilled[t] - Abandoned[t]).
```

Capacity catalog capital values are cumulative: expansion pays the target minus
current tier's capital cost, then activates after the target construction delay.
Downsizing pays a per-tier adjustment fee and reduces throughput and daily fixed
cost after its adjustment delay. It never returns installed capital, cancels
orders or sells inventory. Re-expansion incurs new capital and construction time.

Only one adjustment may be pending. Unfinished expansion can be cancelled once:

```text
startup = floor(paid × startup_bps / 10_000)
incurred = startup + floor((paid - startup) × elapsed_days / total_construction_days)
refund = max(0, paid - incurred - floor(paid × cancellation_fee_bps / 10_000))
```

Elapsed days are bounded to the project's construction interval. The fee is
withheld only from unused prepaid capital, never charged again. Cancellation at
decision day t precedes construction completion on t+1; projects already due at
t or completed cannot cancel. Cancelled projects never complete and subsequent
cancellation is an explicit zero-refund no-op. `capacity_action_quotes` uses the
same implementation as settlement, not a separate language-specific formula.

Enterprise tenders are sealed-bid, lowest-qualified-price auctions. Quantity is
15%–45% of reference daily demand. Qualification requires reservation-price and
quality compliance, unreserved inventory, and enough cumulative capacity during
the delivery window after existing backlog commitments. Scheduled expansion or
downsizing changes that capacity forecast. Stock is reserved immediately at each
award, preventing multiple tenders from allocating the same stock. Bid closing
and delivery due date are separate. Due-day fulfillment happens before remaining
units expire and incur the default penalty; the final simulation day is included.
No tender is created without a possible closing and delivery window before the
horizon. No upfront customer payment or fictitious revenue is booked on award.

## Reliability, support, satisfaction, and loyalty

Reliability spend reduces failures exponentially toward a nonzero floor, while
high utilization raises congestion risk. Each realized customer fulfillment
failure also incurs the configured remediation cost once, as a cash expense;
sales revenue is not also reversed. Tickets join a support queue and
budget buys service capacity. Satisfaction is an exponential moving average of
quality-minus-expectation, wait, failure, and unresolved-ticket pressure;
negative disconfirmation receives greater weight. This mirrors the empirical
asymmetry in [Anderson and Sullivan
(1993)](https://doi.org/10.1287/mksc.12.2.125). Queue length and expected wait
are connected by the intuition of [Little's law
(1961)](https://doi.org/10.1287/opre.9.3.383).

## Research, macro conditions, and information delay

Paid market research snapshots the request day's phase-adjusted demand, price
sensitivity and quality expectation. Snapshot truth stays private until the
noisy report completes, and is never exposed through the private SQL projection.
Reports record sample and delivery days separately. Research observes positive parameters in log space and
performs Normal-Normal Bayesian updating. Higher levels cost more, arrive later,
and have lower signal variance. Belief variance grows between decision windows.
The macro index follows a mean-reverting stochastic process, but CEOs see only
the configured publication-lagged value.

Agents can query their own operational history, beliefs, pending public-status
jobs, public active-company list/promotional/effective prices, and open tenders.
They never receive competitor cash, costs, inventory, capacity, decisions,
private bids, research, future shocks, RNG keys, or pending R&D realizations.
Each CEO can audit its own tender bid and later win/loss/clearing-price outcome;
rival sealed bids remain hidden.

## Accounting and terminal score

Revenue is recognized at fulfillment. Procurement, policy budgets, holding
costs, research, and expansion are charged when incurred. Negative cash marks a
company bankrupt and removes its offers and future CEO calls. Upfront commitments
without enough cash, including any same-decision cancellation refund, are rejected
atomically rather than obtaining hidden credit. Recurring costs can still cause
bankruptcy. The sole official
score is

```text
terminal cash + salvage(on_hand + paid_inbound) - initial enterprise value,
```

where initial enterprise value is initial cash plus initial inventory at the
same salvage basis. All service, market-share, failure, and bankruptcy measures
are diagnostics only.

Actual refunds are separate from revenue and gross payments:

```text
net cash flow = revenue + refunds - payments
gross profit = revenue - fulfilled_units × configured_unit_purchase_cost
```

The second line is a product-margin diagnostic, not another cash debit. Initial
inventory carries its configured cost in this diagnostic and its liquidation
basis in Score; neither view estimates a real firm's accounting net income.
Daily expense categories, actual action receipts and capacity lifecycle records
are durable and replayed with the market state.

Parameters in `configs/default.json` are a model-free calibrated scenario rather than
an estimate for one country or industry. `docs/CALIBRATION.md` records empirical
anchors and model-free probes. Paper results must preregister the config hash and
report sensitivity to demand elasticity, noise, lead time, and delay parameters.
This remains one inventory-based standardized-product market, not an implementation
of services, subscription billing or an industry-plugin framework.
