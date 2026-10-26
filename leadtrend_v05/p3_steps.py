"""Command-line steps for the P3 diagnostics (plan 9.3): each writes its tables under work/diagnostics/P3/ and the
final tables under research/v05/diagnostics/P3/. Runs are diagnostics only: no registration, no attempt, no choice.

    python -X utf8 -m leadtrend_v05.p3_steps match
    python -X utf8 -m leadtrend_v05.p3_steps matched --workers auto
    python -X utf8 -m leadtrend_v05.p3_steps matched-summary
    python -X utf8 -m leadtrend_v05.p3_steps issuers --workers auto
    python -X utf8 -m leadtrend_v05.p3_steps issuers-summary
    python -X utf8 -m leadtrend_v05.p3_steps attribution
    python -X utf8 -m leadtrend_v05.p3_steps voo
    python -X utf8 -m leadtrend_v05.p3_steps supplement
    python -X utf8 -m leadtrend_v05.p3_steps drawdowns
    python -X utf8 -m leadtrend_v05.p3_steps early
"""
import argparse
import csv
import json
import math
import os
from pathlib import Path

from . import paths
from .diagnostics import weekly_records
from .drawdown import load_dtb3, risk_profile
from .experiments import _profile_cells
from .perf_artifacts import read_table
from .perf_bench import available_physical, dump
from .perf_cache import build_context
from .perf_runner import WORKER_PEAK_BYTES, auto_workers
from .sectors import load_sector_map
from .p3_diagnostics import (COST, MATCH_OBJECTS, MODES, NVDA, OBJECTS, _write, attribution, exclusion_table,
                             match_files, pool_table, reference_buys, rebuild_summary, run_issuer_exclusions,
                             run_matched, matched_summary, top_exclusion_table, voo_rebuild)

R2_RUN = Path('work/runs/experiments/R2_robustness')
R1_RUN = Path('work/runs/experiments/R1_mechanisms')
WORK = Path('work/diagnostics/P3')
OUT = Path('research/v05/diagnostics/P3')


def _issuers(root):
    return {r['security_id']: r.get('issuer_id', '') for r in read_table(root / paths.NORMALIZED / 'securities.csv')}


def step_match(root):
    """Pools and reference buys -> match files for the four objects in both modes."""
    ctx = build_context(root, 'uniform')
    records = weekly_records(ctx)
    pools = pool_table(ctx, records, load_sector_map(root))
    buys = {obj: reference_buys(R2_RUN, OBJECTS[obj]) for obj in MATCH_OBJECTS}
    pool_keys = {(p['signal_session_date'], p['security_id']) for p in pools}
    paths_by = match_files(pools, buys, WORK / 'match')
    summary = dict(pool_rows=len(pools), weeks=len({p['signal_session_date'] for p in pools}),
                   buys={obj: len(buys[obj]) for obj in MATCH_OBJECTS},
                   buys_outside_pool={obj: sum(1 for b in buys[obj] if b not in pool_keys) for obj in MATCH_OBJECTS})
    dump(WORK / 'match' / 'match_summary.json', summary)
    return summary


def step_matched(root, workers):
    return run_matched(root, WORK / 'runs' / 'matched', R2_RUN,
                       {(obj, mode): WORK / 'match' / f'match_{obj}_{mode}.csv' for obj in MATCH_OBJECTS for mode in MODES},
                       workers)


def step_matched_summary():
    rows = matched_summary(R2_RUN, WORK / 'runs' / 'matched')
    OUT.mkdir(parents=True, exist_ok=True)
    _write(OUT / 'matched_controls.csv', rows)
    return rows


def step_issuers(root, workers):
    return run_issuer_exclusions(root, WORK / 'runs' / 'issuers', R2_RUN, workers)


def step_issuers_summary(root):
    per = exclusion_table(root, R2_RUN, WORK / 'runs' / 'issuers')
    top = top_exclusion_table(root, R2_RUN, WORK / 'runs' / 'issuers')
    OUT.mkdir(parents=True, exist_ok=True)
    _write(OUT / 'issuer_exclusion_distribution.csv', per)
    _write(OUT / 'issuer_exclusion_top.csv', top)
    return dict(per_object=per, top=top)


def step_attribution(root):
    ctx = build_context(root, 'uniform')
    records = weekly_records(ctx)
    OUT.mkdir(parents=True, exist_ok=True)
    return attribution(root, ctx, records, R2_RUN, OUT)


def step_voo(root):
    """VOO rebuilt from the weekly N-PORT weights, plain and without NVIDIA; round 2's NVIDIA-excluded runs against it."""
    from .p3_diagnostics import NVDA as nvda_issuer
    ctx = build_context(root, 'uniform')
    records = weekly_records(ctx)
    plain = voo_rebuild(root, ctx, records)
    ex = voo_rebuild(root, ctx, records, exclude_issuer=nvda_issuer)
    (WORK / 'voo').mkdir(parents=True, exist_ok=True)
    _write(WORK / 'voo' / 'rebuild_weekly.csv', plain)
    _write(WORK / 'voo' / 'rebuild_ex_nvda_weekly.csv', ex)
    summary = dict(rebuilt_vs_voo=rebuild_summary(plain), ex_nvda_vs_voo=rebuild_summary(ex))
    usable = [r for r in ex if r['index_week'] is not None]
    years = len(usable) / 52
    index_cagr = math.prod(1 + r['index_week'] for r in usable) ** (1 / years) - 1
    rows = []
    for name, pid in OBJECTS.items():
        path = Path('work/runs/robustness/issuer') / pid / f'cost_{COST}bps' / 'summary.json'
        if path.exists():
            run_ann = json.loads(path.read_text(encoding='utf-8'))['annualized_return']
            rows.append(dict(object=name, nvda_excluded_run_cagr=run_ann, ex_nvda_index_cagr=index_cagr,
                             difference=run_ann - index_cagr))
    summary['nvda_excluded_runs_vs_ex_nvda_index'] = rows
    dump(WORK / 'voo' / 'voo_summary.json', summary)
    OUT.mkdir(parents=True, exist_ok=True)
    _write(OUT / 'voo_rebuild_check.csv', [dict(series='rebuilt_full', **summary['rebuilt_vs_voo']),
                                            dict(series='rebuilt_ex_nvda', **summary['ex_nvda_vs_voo'])])
    _write(OUT / 'voo_ex_nvda_vs_round2_runs.csv', rows)
    return summary


def step_supplement(root):
    """Round 1 and round 2 scorecards with the new drawdown and risk columns appended; existing numbers untouched."""
    rates, issuer_of = load_dtb3(root), _issuers(root)
    rows = []
    for source, run, card in (('round1', R1_RUN, 'R1_mechanisms'), ('round2', R2_RUN, 'R2_robustness')):
        with (root / 'research/v05/experiments' / card / 'scorecard.csv').open(encoding='utf-8', newline='') as f:
            for r in csv.DictReader(f):
                folder = run / 'daily' / r['id'] / f'cost_{COST}bps'
                if folder.exists():
                    rows.append(dict(source=source, id=r['id'], **_profile_cells(risk_profile(folder, root, rates, issuer_of))))
    OUT.mkdir(parents=True, exist_ok=True)
    _write(OUT / 'scorecard_risk_supplement.csv', rows)
    return dict(rows=len(rows))


def step_drawdowns(root):
    """9.2 drawdown records for A90, SEC3, SIZE, G, WGT and VOO from the round 2 daily runs."""
    rates, issuer_of = load_dtb3(root), _issuers(root)
    rows = []
    for name, pid in OBJECTS.items():
        prof = risk_profile(R2_RUN / 'daily' / pid / f'cost_{COST}bps', root, rates, issuer_of)
        rows.append(dict(series=name, **{k: v for k, v in prof.items() if not k.startswith('voo_') and k != 'top_issuer'}))
        if name == 'A90':
            rows.append(dict(series='VOO', **{k[4:]: v for k, v in prof.items() if k.startswith('voo_')}))
    OUT.mkdir(parents=True, exist_ok=True)
    _write(OUT / 'drawdown_records.csv', rows)
    return dict(rows=len(rows))


def step_early(root):
    from .p3_external import early_history
    return early_history(root, WORK / 'early')


def _workers(text):
    if text != 'auto':
        return int(text)
    return auto_workers(available_physical(), WORKER_PEAK_BYTES['explore'], os.cpu_count() or 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description='P3 diagnostics steps (runs and tables; never registrations)')
    ap.add_argument('step', choices=['match', 'matched', 'matched-summary', 'issuers', 'issuers-summary', 'attribution',
                                     'voo', 'supplement', 'drawdowns', 'early'])
    ap.add_argument('--workers', default='1')
    args = ap.parse_args(argv)
    root = Path('.').resolve()
    workers = _workers(args.workers)
    steps = {
        'match': lambda: step_match(root),
        'matched': lambda: step_matched(root, workers),
        'matched-summary': step_matched_summary,
        'issuers': lambda: step_issuers(root, workers),
        'issuers-summary': lambda: step_issuers_summary(root),
        'attribution': lambda: step_attribution(root),
        'voo': lambda: step_voo(root),
        'supplement': lambda: step_supplement(root),
        'drawdowns': lambda: step_drawdowns(root),
        'early': lambda: step_early(root),
    }
    result = steps[args.step]()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str)[:3000])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
