from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, stdev

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'paper/data/supplementary/recomputed'
FORMAL = json.loads((ROOT / 'paper/data/formal/arena_analysis.json').read_text(encoding='utf-8'))
PROTOCOL = json.loads((ROOT / 'paper/data/formal/protocol.json').read_text(encoding='utf-8'))
SUPPLEMENT = json.loads((ROOT / 'paper/data/supplementary/arena_analysis.json').read_text(encoding='utf-8'))['supplementary']
PREVIOUS = json.loads((ROOT / 'paper/data/supplementary/track_c_supplement_audit.json').read_text(encoding='utf-8'))
UNIT = 100000
FIXED = 'deterministic-rule-ceo'
NOOP = 'no-op-ceo'
NAMES = {'gpt-6-astra':'Astra','gpt-5.6-sol':'Sol','qwen3.8-max':'Qwen','claude-sonnet-5':'Sonnet','deepseek-v4-pro':'DS Pro','deepseek-v4-flash':'DS Flash','gemini-3.8-flash':'Gemini','doubao-seed-1.8':'Doubao'}


def sign(value: float) -> int:
    return (value > 0) - (value < 0)


def summarize(values: list[float]) -> dict:
    average, sample_sd = mean(values), stdev(values)
    lo, hi = average-sample_sd, average+sample_sd
    return {'values_cents':values,'mean_cents':average,'sample_sd_cents':sample_sd,'mean_minus_sd_cents':lo,'mean_plus_sd_cents':hi,'one_sd_band_excludes_zero':lo>0 or hi<0,'band_side':1 if lo>0 else (-1 if hi<0 else 0),'all_observed_signs_match':len(set(map(sign,values)))==1}


def total_values(scores: dict[str,int], target: str, baseline: str | None = None) -> dict:
    private=scores[baseline or target]
    return {'private':private,'others':sum(scores.values())-private,'market':sum(scores.values())}


assert PROTOCOL['seeds']==[11,29,47]
formal_control_scores={model:next(b for b in FORMAL['track_b'][model]['backgrounds'] if FIXED not in b['model_ids'])['seed_scores_cents'][0] for model in FORMAL['track_c']}
refs=sorted([c for c in SUPPLEMENT['cases'] if not ({FIXED,NOOP}&set(c['model_ids']))],key=lambda c:c['replicate'])
assert len(refs)==2
all_control_scores=[formal_control_scores]+[c['scores_cents'] for c in refs]
all_edges=[]
for old in PREVIOUS['all56_edges']:
    target,recipient=old['entrant'],old['recipient']
    raw=old['formal_cents']+old['fixed_paired_cents']
    assert len(raw)==5
    noop=next(c for c in SUPPLEMENT['cases'] if NOOP in c['model_ids'] and target not in c['model_ids'])
    noop_controlwise=[s[recipient]-noop['scores_cents'][recipient] for s in all_control_scores]
    noop_three=mean(noop_controlwise)
    fixed_seed11=mean([raw[i] for i in [0,3,4]])
    summary=summarize(raw)
    all_edges.append({'entrant':target,'recipient':recipient,'original_3seed_same_sign':old['selected_3seed_same_sign'],**summary,'fixed_seed11_three_mean_cents':fixed_seed11,'noop_seed11_controlwise_cents':noop_controlwise,'noop_seed11_three_control_mean_cents':noop_three,'five_fixed_mean_vs_noop_same_sign':sign(summary['mean_cents'])==sign(noop_three),'seed11_three_fixed_mean_vs_noop_same_sign':sign(fixed_seed11)==sign(noop_three),'original_three_seed_mean_vs_noop_same_sign':sign(mean(raw[:3]))==sign(noop_three)})

entrants=[]
metrics={'private':'private_difference_cents','others':'others_difference_cents','market':'system_difference_cents'}
for old in PREVIOUS['entrants']:
    target=old['model']
    formal=FORMAL['track_c'][target]
    noop=next(c for c in SUPPLEMENT['cases'] if NOOP in c['model_ids'] and target not in c['model_ids'])
    noop_v=total_values(noop['scores_cents'],target,NOOP)
    noop_ctrl=[{metric:value-noop_v[metric] for metric,value in total_values(s,target).items()} for s in all_control_scores]
    row={'model':target,'metrics':{}}
    for metric,key in metrics.items():
        raw=[c[key] for c in formal['seed_comparisons']]+[c[metric] for c in old['fixed_paired_cents']]
        summary=summarize(raw)
        fixed_seed11=mean([raw[i] for i in [0,3,4]])
        noop_controlwise=[c[metric] for c in noop_ctrl]
        noop_three=mean(noop_controlwise)
        row['metrics'][metric]={**summary,'fixed_seed11_three_values_cents':[raw[i] for i in [0,3,4]],'fixed_seed11_three_mean_cents':fixed_seed11,'noop_seed11_controlwise_cents':noop_controlwise,'noop_seed11_three_control_mean_cents':noop_three,'noop_seed11_two_new_control_mean_cents':old['noop_mean_cents'][metric],'five_fixed_mean_vs_noop_same_sign':sign(summary['mean_cents'])==sign(noop_three),'seed11_three_fixed_mean_vs_noop_same_sign':sign(fixed_seed11)==sign(noop_three),'original_three_seed_mean_vs_noop_same_sign':sign(mean(raw[:3]))==sign(noop_three)}
    entrants.append(row)

selected=[e for e in all_edges if e['original_3seed_same_sign']]
counts={
 'all56_one_sd_band_excludes_zero':sum(e['one_sd_band_excludes_zero'] for e in all_edges),
 'all56_one_sd_band_positive':sum(e['band_side']==1 for e in all_edges),
 'all56_one_sd_band_negative':sum(e['band_side']==-1 for e in all_edges),
 'all56_all_five_same_sign':sum(e['all_observed_signs_match'] for e in all_edges),
 'selected12_one_sd_band_excludes_zero':sum(e['one_sd_band_excludes_zero'] for e in selected),
 'selected12_all_five_same_sign':sum(e['all_observed_signs_match'] for e in selected),
 'all56_five_fixed_mean_vs_noop_same_sign':sum(e['five_fixed_mean_vs_noop_same_sign'] for e in all_edges),
 'all56_seed11_three_fixed_mean_vs_noop_same_sign':sum(e['seed11_three_fixed_mean_vs_noop_same_sign'] for e in all_edges),
 'entrants_one_sd_band_excludes_zero':{k:sum(e['metrics'][k]['one_sd_band_excludes_zero'] for e in entrants) for k in metrics},
 'entrants_all_five_same_sign':{k:sum(e['metrics'][k]['all_observed_signs_match'] for e in entrants) for k in metrics},
 'entrants_five_fixed_mean_vs_noop_same_sign':{k:sum(e['metrics'][k]['five_fixed_mean_vs_noop_same_sign'] for e in entrants) for k in metrics},
 'entrants_seed11_three_fixed_mean_vs_noop_same_sign':{k:sum(e['metrics'][k]['seed11_three_fixed_mean_vs_noop_same_sign'] for e in entrants) for k in metrics},
}
result={'source_note':'Read-only reanalysis of formal arena_analysis.json and frozen completed supplement analysis. No new experiments.','observations_in_order':['formal_seed11','formal_seed29','formal_seed47','supplement_seed11_rep1','supplement_seed11_rep2'],'seed_weights':{'11':3,'29':1,'47':1},'sample_sd_ddof':1,'interpretation':'Mean +/- sample SD is an observed descriptive variability band, not a confidence interval. The five observations are not five independent seeds. The supplementary harness differs from the formal protocol. Three seed11 controls reuse the one formal and two new all-LLM runs; No Action still has a single replacement run. Fixed three-seed11 mean vs No Action under the same three-control mean is the fair seed11 baseline comparison.','counts':counts,'formal_seed11_all_llm_control_scores_cents':formal_control_scores,'all56_edges':all_edges,'selected12_edges':selected,'entrants':entrants}
OUT.mkdir(parents=True, exist_ok=True)
(OUT/'simple-five-observation-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')

lines=['# Five observed contrasts: descriptive audit','', 'Unit: USD thousands; SD uses ddof=1. Observation order: formal seed11, seed29, seed47, new seed11 R1, new seed11 R2. Seed11 weight=3/5, seed29=1/5, seed47=1/5. Mean +/- SD is not a CI; protocols also differ.','', '## Original 12 sign-stable directed pairs','', '| Entrant -> recipient | Five values | Mean +/- SD | One-SD band excludes 0? | All five same sign? |','|---|---:|---:|:---:|:---:|']
for e in selected:
    raw='/'.join(f'{v/UNIT:+.2f}' for v in e['values_cents'])
    lines.append(f"| {NAMES[e['entrant']]} -> {NAMES[e['recipient']]} | {raw} | {e['mean_cents']/UNIT:+.2f} +/- {e['sample_sd_cents']/UNIT:.2f} | {'Y' if e['one_sd_band_excludes_zero'] else 'N'} | {'Y' if e['all_observed_signs_match'] else 'N'} |")
lines+=['','## Eight entrant effects: five-observation mean +/- SD','', '| Entrant | Private | Others | Market | Bands exclude 0 P/O/M |','|---|---:|---:|---:|:---:|']
for e in entrants:
    cells=[f"{r['mean_cents']/UNIT:+.2f} +/- {r['sample_sd_cents']/UNIT:.2f}" for r in e['metrics'].values()]
    flags='/'.join('Y' if r['one_sd_band_excludes_zero'] else 'N' for r in e['metrics'].values())
    lines.append(f"| {NAMES[e['model']]} | "+' | '.join(cells)+f' | {flags} |')
lines+=['','## Baseline comparison using the same three seed11 controls','', '| Entrant | Fixed seed11 private/others/market | No Action private/others/market | Same sign P/O/M | Five-observation fixed vs NoAction same sign P/O/M |','|---|---:|---:|:---:|:---:|']
for e in entrants:
    fmt=lambda field:'/'.join(f'{r[field]/UNIT:+.2f}' for r in e['metrics'].values())
    flag=lambda field:'/'.join('Y' if r[field] else 'N' for r in e['metrics'].values())
    lines.append(f"| {NAMES[e['model']]} | {fmt('fixed_seed11_three_mean_cents')} | {fmt('noop_seed11_three_control_mean_cents')} | {flag('seed11_three_fixed_mean_vs_noop_same_sign')} | {flag('five_fixed_mean_vs_noop_same_sign')} |")
lines+=['','## Counts','', '```json',json.dumps(counts,indent=2),'```','', '## All 56 directed pairs','', '| Entrant -> recipient | Mean +/- SD | Band side | All five same sign | Fixed seed11 mean | NoAction mean |','|---|---:|:---:|:---:|---:|---:|']
for e in all_edges:
    lines.append(f"| {NAMES[e['entrant']]} -> {NAMES[e['recipient']]} | {e['mean_cents']/UNIT:+.2f} +/- {e['sample_sd_cents']/UNIT:.2f} | {e['band_side']} | {'Y' if e['all_observed_signs_match'] else 'N'} | {e['fixed_seed11_three_mean_cents']/UNIT:+.2f} | {e['noop_seed11_three_control_mean_cents']/UNIT:+.2f} |")
(OUT/'simple-five-observation-results.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print('\n'.join(lines[:lines.index('## All 56 directed pairs')]))
