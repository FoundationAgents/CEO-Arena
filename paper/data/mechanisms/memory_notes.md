# Selected MEMORY.md excerpts and executed decisions

These 16 excerpts support the main-evaluation mechanism hypotheses. They are agent-written beliefs and plans, not verified simulator descriptions or identified causal mechanisms. Notes preserve their original units and wording.

The two selected-event JSON files contain the complete memory writes, sealed decision fields, and action receipts. memory_write_receipts.json confirms each file write. Trace hashes identify the archived source; raw provider traces are not included in this package.

## 1. gpt-6-astra: seed 11, day 7

- Analysis group: Positive-return examples
- Run: `run-71a3e82592675650fe488f2f35f3c4fa0eceba661ba8a9e7bbb501a16ed710fb`; company: `company-1`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-11/lineup-371cd70e840d7b06/agent-traces/run-71a3e82592675650fe488f2f35f3c4fa0eceba661ba8a9e7bbb501a16ed710fb/company-1-7-0960e247b14ea134.ndjson`, line 2546.
- Archived trace SHA-256: `6dd909d82c68945b85e654e712b7be53f93b894512929bed52657e7f6ea72ecc`.
- Complete note SHA-256 (UTF-8): `06341dfead7c243eb4d70edd8cf9476bae36f5f865cbc41b42b52faf15135067`.

> - Reorder targets A=350 (was 600), B=160 (was 400), C=120 (was 200). Targets are inventory position: observed on-hand + inbound equaled old targets. Reductions pause future procurement; they do not immediately refund cash or cancel paid inbound.

Sealed decision fields:

```json
{
  "decision.company_id": "company-1",
  "decision.world_version": "7",
  "decision.products_json": "[{\"product_id\":\"A\",\"price_cents\":3800,\"promotion_cents\":null,\"reorder_target\":350,\"development_budget_cents\":null},{\"product_id\":\"B\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":160,\"development_budget_cents\":null},{\"product_id\":\"C\",\"price_cents\":14500,\"promotion_cents\":null,\"reorder_target\":120,\"development_budget_cents\":null}]",
  "decision.marketing_json": "[{\"segment_id\":\"premium\",\"channel\":\"search\",\"daily_budget_cents\":5000}]",
  "decision.reliability_budget_cents": "5000",
  "decision.support_budget_cents": "10000"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 3 | 7 | set_product | applied | 8 |
| 4 | 7 | set_marketing | applied | 8 |
| 5 | 7 | set_operations | applied | 8 |
| 6 | 14 | set_product | applied | 15 |

## 2. gpt-6-astra: seed 29, day 147

- Analysis group: Positive-return examples
- Run: `run-ef03e11076a59b9414bb0f17b83ce2209bed225012b602e9abb6f9344db18463`; company: `company-1`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-29/lineup-371cd70e840d7b06/agent-traces/run-ef03e11076a59b9414bb0f17b83ce2209bed225012b602e9abb6f9344db18463/company-1-147-d3009619bc914d06.ndjson`, line 1757.
- Archived trace SHA-256: `429161001a78fcaedebd9ffe785686ca13cc456e9ab807bf7e831d229f5803ce`.
- Complete note SHA-256 (UTF-8): `51bae067591dd7313e11361891757ae76eeaf99ed9c3d56ba35a1860c92280c7`.

> - Days85-147 averaged41.51 orders/day; only 1/63 days exceeded60 (61 orders), with zero abandonment. Current week40.57/day and no backlog. Tier1 is unnecessary for observed consumer volume.

Sealed decision fields:

```json
{
  "decision.company_id": "company-1",
  "decision.world_version": "147",
  "decision.products_json": "[{\"product_id\":\"C\",\"price_cents\":13500,\"promotion_cents\":null,\"reorder_target\":80,\"development_budget_cents\":null}]",
  "decision.reliability_budget_cents": "6000",
  "decision.support_budget_cents": "4000",
  "decision.capacity_downsize_tier": "0"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 43 | 147 | expand_capacity | completed | 147 |
| 44 | 147 | downsize_capacity | applied | 154 |
| 45 | 147 | set_product | applied | 148 |
| 46 | 147 | set_operations | applied | 148 |
| 47 | 154 | downsize_capacity | completed | 154 |
| 48 | 154 | set_product | applied | 155 |
| 49 | 154 | set_operations | applied | 155 |

## 3. gpt-6-astra: seed 47, day 7

- Analysis group: Positive-return examples
- Run: `run-067a23cf0a5e56ce35a935c723b96fa80d7bb48a784758eeb8b2842e4b512ef7`; company: `company-1`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-47/lineup-371cd70e840d7b06/agent-traces/run-067a23cf0a5e56ce35a935c723b96fa80d7bb48a784758eeb8b2842e4b512ef7/company-1-7-bd16ae56816ff1bc.ndjson`, line 1339.
- Archived trace SHA-256: `15ff135bb29c611e220295bc8381cdd0aa28359589a280bdd1ad9a0b39290594`.
- Complete note SHA-256 (UTF-8): `9e64f8d730ede3bbc7f8d98fb18871275284db1b3e56b56d056cf5f6c4bafbdc`.

> - Base capacity has 25 units/day spare versus week-1 average 34.9; canceling tier 1 saves 13,000/day after day 21 plus refund, unless demand rises materially. Revisit with next week’s sales, awareness, and inventory.

Sealed decision fields:

```json
{
  "decision.company_id": "company-1",
  "decision.world_version": "7",
  "decision.products_json": "[{\"product_id\":\"A\",\"price_cents\":4000,\"promotion_cents\":null,\"reorder_target\":300,\"development_budget_cents\":null},{\"product_id\":\"B\",\"price_cents\":7500,\"promotion_cents\":null,\"reorder_target\":220,\"development_budget_cents\":null},{\"product_id\":\"C\",\"price_cents\":13500,\"promotion_cents\":null,\"reorder_target\":150,\"development_budget_cents\":null}]",
  "decision.marketing_json": "[{\"segment_id\":\"mainstream\",\"channel\":\"display\",\"daily_budget_cents\":2000},{\"segment_id\":\"premium\",\"channel\":\"display\",\"daily_budget_cents\":2000},{\"segment_id\":\"value\",\"channel\":\"display\",\"daily_budget_cents\":2000}]",
  "decision.research_project_tier": "1",
  "decision.cancel_capacity_project_id": "47216e0addf4db9895b54280"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 4 | 7 | cancel_capacity_project | cancelled | 7 |
| 5 | 7 | set_product | applied | 8 |
| 6 | 7 | set_marketing | applied | 8 |
| 7 | 7 | start_research_project | applied | 8 |
| 8 | 14 | set_marketing | applied | 15 |

## 4. gpt-5.6-sol: seed 11, day 126

- Analysis group: Positive-return examples
- Run: `run-71a3e82592675650fe488f2f35f3c4fa0eceba661ba8a9e7bbb501a16ed710fb`; company: `company-5`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-11/lineup-371cd70e840d7b06/agent-traces/run-71a3e82592675650fe488f2f35f3c4fa0eceba661ba8a9e7bbb501a16ed710fb/company-5-126-0f2f5d73a601f33b.ndjson`, line 1370.
- Archived trace SHA-256: `7f00dcf38e70f916eec7564ea5e730975d46297ecd578ae1973ba4255389640f`.
- Complete note SHA-256 (UTF-8): `79a53bffd0053ce4fb5268f6231c1687dbdc0968aedfeb7a369feb2f6ee36bb8`.

> - Open tender ec1d5f1cfdb5baee: B, 152 units, minimum quality 0.7075606689, reservation 9,375, deadline 133, due 140. UNEXECUTED DRAFT: bid 5,999 cents/unit; positive 1,199-cent gross margin if won. UNEXECUTED DRAFT: expand capacity tier 1 -> 2, charged quote 1,800,000 cents, effective about day 161; this is a lead-time hedge for tier-3 innovation, to cancel/reconsider later if demand does not rise. Preview affordable; no other one-time actions.

Sealed decision fields:

```json
{
  "decision.company_id": "company-5",
  "decision.world_version": "126",
  "decision.capacity_target_tier": "2",
  "decision.enterprise_bids_json": "[{\"tender_id\":\"ec1d5f1cfdb5baee\",\"unit_price_cents\":5999}]"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 37 | 126 | expand_capacity | applied | 161 |
| 38 | 126 | submit_enterprise_bid | applied | 127 |
| 39 | 133 | seal_decision | no_op |  |

## 5. gpt-5.6-sol: seed 29, day 231

- Analysis group: Positive-return examples
- Run: `run-ef03e11076a59b9414bb0f17b83ce2209bed225012b602e9abb6f9344db18463`; company: `company-5`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-29/lineup-371cd70e840d7b06/agent-traces/run-ef03e11076a59b9414bb0f17b83ce2209bed225012b602e9abb6f9344db18463/company-5-231-751e850497acbe7c.ndjson`, line 1991.
- Archived trace SHA-256: `e69422a2680f0da5fe8866882e7fc342f08215fb3d12e29bacec56ffc846e627`.
- Complete note SHA-256 (UTF-8): `194edb9bbf630fb3e8e92f2379d65727be97aa3f9391ae8afa23f38f6470dce6`.

> Reason for changing old capacity hold: days197-231 orders averaged55.57/day, max73; a simplified 60-unit FIFO replay serves all1945 observed orders, peak queue26, ending0. This is a model, not settled tier0 performance; it excludes customer reactions/abandonment/product allocation. Full days148-231 average63.58 includes an extended >70/day peak: renewed demand is a real risk. Tier0 saves13,000c/day; fee pays in7.69 active days; if never rebuilt, quoted terminal fee savings net3,319,000c. At recent contribution2142c/unit, tier1 needs~6.1 incremental units/day merely to cover daily fee. Don't keep unused capacity just for terminal value: it has none. Preserve service spend during this capacity test.

Sealed decision fields:

```json
{
  "decision.company_id": "company-5",
  "decision.world_version": "231",
  "decision.products_json": "[{\"product_id\":\"A\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":300,\"development_budget_cents\":null}]",
  "decision.capacity_downsize_tier": "0"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 55 | 231 | downsize_capacity | applied | 238 |
| 56 | 231 | set_product | applied | 232 |
| 57 | 238 | downsize_capacity | completed | 238 |
| 58 | 238 | seal_decision | no_op |  |

## 6. gpt-5.6-sol: seed 47, day 7

- Analysis group: Positive-return examples
- Run: `run-067a23cf0a5e56ce35a935c723b96fa80d7bb48a784758eeb8b2842e4b512ef7`; company: `company-5`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-47/lineup-371cd70e840d7b06/agent-traces/run-067a23cf0a5e56ce35a935c723b96fa80d7bb48a784758eeb8b2842e4b512ef7/company-5-7-c719a6aba535b60e.ndjson`, line 2208.
- Archived trace SHA-256: `c4f07ca8835293ba4f039331cc56ae5dc4ecf6ff642735bbe7c8b185f48f30ec`.
- Complete note SHA-256 (UTF-8): `6788498ab674fe6d7105e4e5ca846f1e46e1234735a97834a1ddccf3ac2796f6`.

> - Keep all nine segment/channel marketing budgets at 5k/day (45k total) for one more comparable week. Reliability remains 10k/day; support is reduced from 10k to 5k/day. Monitor failures, satisfaction, and support backlog before cutting further.

Sealed decision fields:

```json
{
  "decision.company_id": "company-5",
  "decision.world_version": "7",
  "decision.products_json": "[{\"product_id\":\"A\",\"price_cents\":4000,\"promotion_cents\":200,\"reorder_target\":350,\"development_budget_cents\":10000},{\"product_id\":\"B\",\"price_cents\":7500,\"promotion_cents\":300,\"reorder_target\":250,\"development_budget_cents\":0},{\"product_id\":\"C\",\"price_cents\":14200,\"promotion_cents\":0,\"reorder_target\":120,\"development_budget_cents\":0}]",
  "decision.reliability_budget_cents": "10000",
  "decision.support_budget_cents": "5000",
  "decision.cancel_capacity_project_id": "fc8841b85991c7b5069ef23a"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 7 | 7 | cancel_capacity_project | cancelled | 7 |
| 8 | 7 | set_product | applied | 8 |
| 9 | 7 | set_operations | applied | 8 |
| 10 | 14 | set_product | applied | 15 |
| 11 | 14 | set_marketing | applied | 15 |
| 12 | 14 | set_operations | applied | 15 |
| 13 | 14 | submit_enterprise_bid | applied | 15 |

## 7. qwen3.8-max: seed 11, day 224

- Analysis group: Positive-return examples
- Run: `run-665f7924e763a2c5744d1a696852dbcae569d2237f365eca61cc3c73d1315383`; company: `company-4`; omitted candidate: `deepseek-v4-pro`.
- Source: `data/runs/seed-11/lineup-4f045970ec7d4ea3/agent-traces/run-665f7924e763a2c5744d1a696852dbcae569d2237f365eca61cc3c73d1315383/company-4-224-294a303693f1d4de.ndjson`, line 8009.
- Archived trace SHA-256: `3e7065302213c1a9ef8c460c4828a2da2827b5b0d463eaeeb474e974382011c4`.
- Complete note SHA-256 (UTF-8): `503242a84ff02e741abacbf99a3becede5a221f4fc66661cee7813a16bc9ac5a`.

> ## ★★★ WK32 (d218–224) — MARKETING=0 TEST **FAILED**. REVERTED. SUPPORT=0 ALSO REVERSED.

Sealed decision fields:

```json
{
  "decision.company_id": "company-4",
  "decision.world_version": "224",
  "decision.marketing_json": "[{\"segment_id\":\"mainstream\",\"channel\":\"search\",\"daily_budget_cents\":1800},{\"segment_id\":\"premium\",\"channel\":\"search\",\"daily_budget_cents\":1900},{\"segment_id\":\"value\",\"channel\":\"search\",\"daily_budget_cents\":300}]",
  "decision.reliability_budget_cents": "0",
  "decision.support_budget_cents": "4000"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 73 | 224 | set_marketing | applied | 225 |
| 74 | 224 | set_operations | applied | 225 |
| 75 | 231 | set_operations | applied | 232 |

## 8. qwen3.8-max: seed 29, day 56

- Analysis group: Positive-return examples
- Run: `run-7c9cc5bbc50ad1dc650e853e9304a2ebcb7cb3d6a9f7c398e6ad8845c782f2ac`; company: `company-4`; omitted candidate: `gpt-6-astra`.
- Source: `data/runs/seed-29/lineup-f13f4e4537977edd/agent-traces/run-7c9cc5bbc50ad1dc650e853e9304a2ebcb7cb3d6a9f7c398e6ad8845c782f2ac/company-4-56-875df803abf04978.ndjson`, line 11555.
- Archived trace SHA-256: `5157bef38ad5e00c234d33ded6cb246a5c56836444771f15821f0115dc5e1d51`.
- Complete note SHA-256 (UTF-8): `526460787490d7cbc7e4ec39c77b77cb830a658ab77af3babf89df9a18713c14`.

> **Full revert of the wk-49 cost experiment + slash development.**

Sealed decision fields:

```json
{
  "decision.company_id": "company-4",
  "decision.world_version": "56",
  "decision.products_json": "[{\"product_id\":\"A\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":null,\"development_budget_cents\":0},{\"product_id\":\"B\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":110,\"development_budget_cents\":500},{\"product_id\":\"C\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":110,\"development_budget_cents\":1000}]",
  "decision.reliability_budget_cents": "12000",
  "decision.support_budget_cents": "8000"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 27 | 56 | set_product | applied | 57 |
| 28 | 56 | set_operations | applied | 57 |
| 29 | 63 | set_product | applied | 64 |
| 30 | 63 | set_marketing | applied | 64 |
| 31 | 63 | set_operations | applied | 64 |

## 9. qwen3.8-max: seed 47, day 350

- Analysis group: Positive-return examples
- Run: `run-d8107e58113dba59749a6b69ef39c802c049ff7cea939581e0884327b9d56f07`; company: `company-4`; omitted candidate: `deepseek-v4-pro`.
- Source: `data/runs/seed-47/lineup-4f045970ec7d4ea3/agent-traces/run-d8107e58113dba59749a6b69ef39c802c049ff7cea939581e0884327b9d56f07/company-4-350-777c7d2ff2a2e36f.ndjson`, line 8962.
- Archived trace SHA-256: `ca91c7c852ef2062836e04f6b537e02a8a7ffa78faa78fb7dc5ea5171aa0be00`.
- Complete note SHA-256 (UTF-8): `381e553ce49ba4f80e7a3746d339696a97f9249e8bf245f2747d3d2465c74e3c`.

> ## !!!!! #104 A PRICE TEST RESOLVED AT d350 -> REVERTED TO 4,700. A PRICING IS NOW CLOSED. !!!!!

Sealed decision fields:

```json
{
  "decision.company_id": "company-4",
  "decision.world_version": "350",
  "decision.products_json": "[{\"product_id\":\"A\",\"price_cents\":4700,\"promotion_cents\":null,\"reorder_target\":null,\"development_budget_cents\":null}]",
  "decision.enterprise_bids_json": "[{\"tender_id\":\"0fa727bea1ddb6ee\",\"unit_price_cents\":13950}]"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 103 | 350 | set_product | applied | 351 |
| 104 | 350 | submit_enterprise_bid | applied | 351 |
| 105 | 357 | seal_decision | no_op |  |

## 10. doubao-seed-1.8: seed 11, day 84

- Analysis group: Directed-effect examples
- Run: `run-71a3e82592675650fe488f2f35f3c4fa0eceba661ba8a9e7bbb501a16ed710fb`; company: `company-8`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-11/lineup-371cd70e840d7b06/agent-traces/run-71a3e82592675650fe488f2f35f3c4fa0eceba661ba8a9e7bbb501a16ed710fb/company-8-84-e8ddbbfaf9842120.ndjson`, line 11872.
- Archived trace SHA-256: `797aa9d9915c3b68ec28cf95fda24cf6af5dd094848b1a16c61e99c4def23799`.
- Complete note SHA-256 (UTF-8): `84fc39ec1b06f33e5d855f43be3b4832fef1d34908c9dac55d2fa67c7ef6f296`.

>    - Product C: Increased promotion to1000c (effective12000c, matching company-2 to boost demand), set reorder target to50 (to resume production and avoid stockouts), resumed daily development budget2000c. Retained price13000c.

Sealed decision fields:

```json
{
  "decision.company_id": "company-8",
  "decision.world_version": "84",
  "decision.products_json": "[{\"product_id\":\"A\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":null,\"development_budget_cents\":1000},{\"product_id\":\"B\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":null,\"development_budget_cents\":1500},{\"product_id\":\"C\",\"price_cents\":null,\"promotion_cents\":1000,\"reorder_target\":50,\"development_budget_cents\":2000}]",
  "decision.marketing_json": "[{\"segment_id\":\"premium\",\"channel\":\"search\",\"daily_budget_cents\":10000}]",
  "decision.reliability_budget_cents": "4000",
  "decision.support_budget_cents": "4000"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 29 | 84 | set_product | applied | 85 |
| 30 | 84 | set_marketing | applied | 85 |
| 31 | 84 | set_operations | applied | 85 |
| 32 | 91 | set_product | applied | 92 |
| 33 | 91 | set_marketing | applied | 92 |

## 11. doubao-seed-1.8: seed 29, day 98

- Analysis group: Directed-effect examples
- Run: `run-ef03e11076a59b9414bb0f17b83ce2209bed225012b602e9abb6f9344db18463`; company: `company-8`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-29/lineup-371cd70e840d7b06/agent-traces/run-ef03e11076a59b9414bb0f17b83ce2209bed225012b602e9abb6f9344db18463/company-8-98-bae345520eded325.ndjson`, line 25306.
- Archived trace SHA-256: `abfead9026b333556df03f5e43414a94804816442933bbc089dea0834b1a2091`.
- Complete note SHA-256 (UTF-8): `897817783ca960f18bf528eaa9b33effdf477a6c2daf20fb6535dd23090cc2d0`.

>    - C: Price $125.00 (12500 cents, reduced from $130 to match company-2), reorder target 50 (reduced from 150), $5 daily development budget, promotion $0/day

Sealed decision fields:

```json
{
  "decision.company_id": "company-8",
  "decision.world_version": "98",
  "decision.products_json": "[{\"product_id\":\"A\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":300,\"development_budget_cents\":null},{\"product_id\":\"C\",\"price_cents\":12500,\"promotion_cents\":null,\"reorder_target\":50,\"development_budget_cents\":null}]",
  "decision.reliability_budget_cents": "10000",
  "decision.support_budget_cents": "15000",
  "decision.research_project_tier": "1"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 29 | 98 | set_product | applied | 99 |
| 30 | 98 | set_operations | applied | 99 |
| 31 | 98 | start_research_project | applied | 99 |
| 32 | 105 | set_product | applied | 106 |
| 33 | 105 | set_marketing | applied | 106 |
| 34 | 105 | set_operations | applied | 106 |
| 35 | 105 | start_research_project | applied | 106 |

## 12. doubao-seed-1.8: seed 47, day 28

- Analysis group: Directed-effect examples
- Run: `run-067a23cf0a5e56ce35a935c723b96fa80d7bb48a784758eeb8b2842e4b512ef7`; company: `company-8`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-47/lineup-371cd70e840d7b06/agent-traces/run-067a23cf0a5e56ce35a935c723b96fa80d7bb48a784758eeb8b2842e4b512ef7/company-8-28-51f2cb46b440acd4.ndjson`, line 13556.
- Archived trace SHA-256: `37501db75bc1d551aa2e80baf42ddcc2c8e10d4c84e6c547951bb43498d8ea70`.
- Complete note SHA-256 (UTF-8): `10e998a082d786d0d667aeb5e518556e9974b74ccc6369300358c0977c3e8559`.

> - **Product C**: Price reduced to $130 (match company-2), promotion added ($10/day), development budget reduced to $5/day; reorder target remains25 (reduce excess inventory)

Sealed decision fields:

```json
{
  "decision.company_id": "company-8",
  "decision.world_version": "28",
  "decision.products_json": "[{\"product_id\":\"A\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":400,\"development_budget_cents\":null},{\"product_id\":\"B\",\"price_cents\":7200,\"promotion_cents\":500,\"reorder_target\":null,\"development_budget_cents\":500},{\"product_id\":\"C\",\"price_cents\":13000,\"promotion_cents\":1000,\"reorder_target\":null,\"development_budget_cents\":500}]",
  "decision.marketing_json": "[{\"segment_id\":\"premium\",\"channel\":\"search\",\"daily_budget_cents\":0},{\"segment_id\":\"value\",\"channel\":\"search\",\"daily_budget_cents\":20000}]",
  "decision.reliability_budget_cents": "10000",
  "decision.support_budget_cents": "15000"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 18 | 28 | set_product | applied | 29 |
| 19 | 28 | set_marketing | applied | 29 |
| 20 | 28 | set_operations | applied | 29 |
| 21 | 35 | set_product | applied | 36 |
| 22 | 35 | set_marketing | applied | 36 |
| 23 | 35 | start_research_project | applied | 36 |
| 24 | 35 | request_market_research | applied | 36 |

## 13. claude-sonnet-5: seed 11, day 42

- Analysis group: Directed-effect examples
- Run: `run-71a3e82592675650fe488f2f35f3c4fa0eceba661ba8a9e7bbb501a16ed710fb`; company: `company-3`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-11/lineup-371cd70e840d7b06/agent-traces/run-71a3e82592675650fe488f2f35f3c4fa0eceba661ba8a9e7bbb501a16ed710fb/company-3-42-1a4fc5a38c5e557d.ndjson`, line 1818.
- Archived trace SHA-256: `846eb9f7cc15b913a3e1dd32061191fce0a2105d24f7b86cb427540595f5a6c4`.
- Complete note SHA-256 (UTF-8): `49f4a71abef582a0074c0315d07eff2e249f528a39c77067fe209e7f69a46079`.

> - Day42 DECISION: staged **expand_capacity target_tier=2** (135u/day, 73,000c/day). Quote: charge 1,800,000c now, effective_day=77 (35-day lead). Rationale: 81% utilization + rising revenue trend (wk36-42 rev 2.94M vs wk29-35 2.15M, +37%) + low awareness (25-32%, lots of headroom as marketing keeps working) means demand likely to consistently exceed 90/day well before day77. Payback math: only need ~26% utilization of the +45 unit/day increment to cover the +20,000c/day opex delta (avg gross margin/unit ~2,113c from recent mix). Can cancel before day77 if evidence changes (refund = unspent prepaid minus cancellation fee ~5%).

Sealed decision fields:

```json
{
  "decision.company_id": "company-3",
  "decision.world_version": "42",
  "decision.products_json": "[{\"product_id\":\"B\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":700,\"development_budget_cents\":null}]",
  "decision.capacity_target_tier": "2",
  "decision.enterprise_bids_json": "[{\"tender_id\":\"8631152cec835b28\",\"unit_price_cents\":9000}]",
  "decision.market_research_json": "[{\"segment_id\":\"premium\",\"level\":3}]"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 16 | 42 | expand_capacity | applied | 77 |
| 17 | 42 | set_product | applied | 43 |
| 18 | 42 | request_market_research | applied | 43 |
| 19 | 42 | submit_enterprise_bid | applied | 43 |
| 20 | 49 | cancel_capacity_project | cancelled | 49 |
| 21 | 49 | set_product | applied | 50 |
| 22 | 49 | set_marketing | applied | 50 |
| 23 | 49 | request_market_research | applied | 50 |

## 14. claude-sonnet-5: seed 47, day 70

- Analysis group: Directed-effect examples
- Run: `run-067a23cf0a5e56ce35a935c723b96fa80d7bb48a784758eeb8b2842e4b512ef7`; company: `company-3`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-47/lineup-371cd70e840d7b06/agent-traces/run-067a23cf0a5e56ce35a935c723b96fa80d7bb48a784758eeb8b2842e4b512ef7/company-3-70-6483b98c56d4ee53.ndjson`, line 2359.
- Archived trace SHA-256: `1e35c072c6920cdeecccf8ce777d91e5fb153bf672a5c2311aa8f5f286b81b75`.
- Complete note SHA-256 (UTF-8): `dd7f66cfb407b9801b2dd57502bfb05ebd55dca12ab7a768c68bf667ae44a5dc`.

> - **Action day70: started Tier1 project #3 (cost 1,000,000).** Rationale: strong 2-for-2 track record, cash ample (43.5M, commitment is ~2.3% of cash), 430 days remain to capitalize on the quality/demand benefit, and knowledge capital appears to be the main driver of the recent gross-profit uptrend. Median completion ~day115 (45-day median delay). Expected +2,000,000 capital company-wide (~667k/product) — watch for realized_capital_gain in innovation_projects table around day100-130.

Sealed decision fields:

```json
{
  "decision.company_id": "company-3",
  "decision.world_version": "70",
  "decision.research_project_tier": "1"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 21 | 70 | start_research_project | applied | 71 |
| 22 | 77 | start_research_project | applied | 78 |
| 23 | 77 | request_market_research | applied | 78 |

## 15. deepseek-v4-flash: seed 29, day 28

- Analysis group: Directed-effect examples
- Run: `run-ef03e11076a59b9414bb0f17b83ce2209bed225012b602e9abb6f9344db18463`; company: `company-7`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-29/lineup-371cd70e840d7b06/agent-traces/run-ef03e11076a59b9414bb0f17b83ce2209bed225012b602e9abb6f9344db18463/company-7-28-b39f6d0ac1ec8b8d.ndjson`, line 61940.
- Archived trace SHA-256: `ed03c5fa208314b4bed735d849b7aa1a6718aab0b744e2eed65c7fe2781d3d31`.
- Complete note SHA-256 (UTF-8): `de903da56e7aae475d18f96c6b8feda0ead980ec5fc90723756a6c073cbb0c18`.

> 2. downsize_capacity → tier 0 (fee 100k, effective d35). Saves 13k/day (53k→40k). Demand 35/day ≪ 60 cap; re-expand T1 = 1.2M + 21d if demand >60. Re-evaluate every window.

Sealed decision fields:

```json
{
  "decision.company_id": "company-7",
  "decision.world_version": "28",
  "decision.marketing_json": "[{\"segment_id\":\"mainstream\",\"channel\":\"search\",\"daily_budget_cents\":20000},{\"segment_id\":\"premium\",\"channel\":\"search\",\"daily_budget_cents\":20000}]",
  "decision.capacity_downsize_tier": "0",
  "decision.enterprise_bids_json": "[{\"tender_id\":\"a38f75603b234f4d\",\"unit_price_cents\":4900}]"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 14 | 28 | downsize_capacity | applied | 35 |
| 15 | 28 | set_marketing | applied | 29 |
| 16 | 28 | submit_enterprise_bid | applied | 29 |
| 17 | 35 | downsize_capacity | completed | 35 |
| 18 | 35 | seal_decision | no_op |  |

## 16. deepseek-v4-pro: seed 11, day 21

- Analysis group: Directed-effect examples
- Run: `run-71a3e82592675650fe488f2f35f3c4fa0eceba661ba8a9e7bbb501a16ed710fb`; company: `company-2`; omitted candidate: `deterministic-rule-ceo`.
- Source: `data/runs/seed-11/lineup-371cd70e840d7b06/agent-traces/run-71a3e82592675650fe488f2f35f3c4fa0eceba661ba8a9e7bbb501a16ed710fb/company-2-21-dd39f8ae4d5cc20c.ndjson`, line 20578.
- Archived trace SHA-256: `05f919a9de603bf13e1df1bf39fa26054d750feb98d0552d4d3559de6967270b`.
- Complete note SHA-256 (UTF-8): `7a1466081c95630ea869b3067707f6e5c7c5d3a3f833e8c956cc46e0076b785e`.

> - Cash 41.73M. Capacity tier 1 (90/day, 53k/day ops) — active day 21. We sell ~60-67/day → NOT capacity-bound anymore. Tier 2 (3M/35d/135 per day/73k per day) is a WASTE unless demand grows a lot. Skip.

Sealed decision fields:

```json
{
  "decision.company_id": "company-2",
  "decision.world_version": "21",
  "decision.products_json": "[{\"product_id\":\"A\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":350,\"development_budget_cents\":15000},{\"product_id\":\"B\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":200,\"development_budget_cents\":15000},{\"product_id\":\"C\",\"price_cents\":null,\"promotion_cents\":null,\"reorder_target\":150,\"development_budget_cents\":10000}]"
}
```

Related settlement receipts:

| Receipt index | Day | Action | Status | Effective day |
|---:|---:|---|---|---:|
| 13 | 21 | expand_capacity | completed | 21 |
| 14 | 21 | set_product | applied | 22 |
| 15 | 28 | set_product | applied | 29 |
| 16 | 28 | submit_enterprise_bid | applied | 29 |
