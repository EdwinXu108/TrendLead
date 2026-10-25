"""Neutral-parameter equivalence check for the round-1 selection rules.

Each new rule run with its neutral parameters must reproduce the reviewed stage 4 run of L090_N10_b2_R2 byte for byte in
summary.json, at both costs. The runs use that configuration's own identifier, so the summary's configuration field is
the stage 4 one. They are not registered and do not count as attempts. Only these four combinations are admitted.
"""
import json
from pathlib import Path

from . import paths
from .experiments import ExperimentContext, _experiment_ctx, _jobs, _run_jobs, make_point
from .perf_cache import ensure_signal_tables, identity, load_cache
from .signals import normalize

CONFIGURATION = 'L090_N10_b2_R2'
COSTS = [5, 10]
NEUTRAL = {
    'ent_pos': {'short_weight': 0},
    'ent_neg': {'max_short_pct': 1.0},
    'sector_cap': {'sector_k': 999},  # 999 stands for "no large-sector limit"
    'exit_cap': {'max_rank_exits': 999},
}


def neutral_point(rule):
    point = make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2', rule, NEUTRAL[rule])
    return dict(point, id=CONFIGURATION)


def run_neutral_check(root, output, workers=1):
    root = Path(root)
    output = Path(output)
    doc = identity(root, 'uniform')
    load_cache(root, doc)
    ensure_signal_tables(root, doc, [normalize({'kind': 'momentum', 'L': 90, 'skip': 0})])
    experiment = {'id': 'NEUTRAL_CHECK', 'note': 'not registered; not an attempt'}
    report = {'configuration': CONFIGURATION, 'rules': {}}
    for rule in NEUTRAL:
        point = neutral_point(rule)
        payload = dict(points=[point], costs=COSTS, experiment=experiment)
        ctx = _experiment_ctx(root, doc, payload)
        jobs = _jobs([point], output / rule, 'explore', 'explore', 'explore', COSTS, experiment)
        _run_jobs(jobs, ctx, workers, payload)
        cells = {}
        for cost in COSTS:
            ref = root / paths.STAGE4_RUNS / CONFIGURATION / f'cost_{cost}bps' / 'summary.json'
            new = output / rule / 'explore' / CONFIGURATION / f'cost_{cost}bps' / 'summary.json'
            cells[f'{cost}bps'] = 'PASS' if new.exists() and ref.read_bytes() == new.read_bytes() else 'FAIL'
        counts = {}
        for cost in COSTS:
            manifest = output / rule / 'explore' / CONFIGURATION / f'cost_{cost}bps' / 'run_manifest.json'
            counts[f'{cost}bps'] = json.loads(manifest.read_text(encoding='utf-8')).get('selection_counts', {})
        report['rules'][rule] = dict(params=NEUTRAL[rule], summary_bytes=cells, selection_counts=counts)
    report['status'] = 'PASS' if all(v == 'PASS' for r in report['rules'].values() for v in r['summary_bytes'].values()) else 'FAIL'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'neutral_check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report
