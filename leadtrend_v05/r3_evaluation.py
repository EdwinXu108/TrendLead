"""Round 3 robustness runs and evaluation (plan 10.4, 10.5): noise ruler, timing, top-3 exclusion, experiment-level random
controls, paired bootstrap, the 10.5 criteria and the discussion pack. Numbers and mechanical checks only.

Inputs: the committed registration research/v05/experiments/R3_structure.json and the run folder written by
run-experiment (work/runs/experiments/R3_structure); robustness runs go to work/runs/r3_robustness. Outputs go to
research/v05/experiments/R3_structure/robustness/ and discussion_pack.md beside them.

    python -X utf8 -m leadtrend_v05.r3_evaluation runs --out work/runs/r3_robustness --workers auto
    python -X utf8 -m leadtrend_v05.r3_evaluation evaluate --run work/runs/experiments/R3_structure \
           --robust work/runs/r3_robustness --out research/v05/experiments/R3_structure
"""
import argparse
import json
from pathlib import Path

import numpy as np

from .experiments import A90_ID, _metrics_or_none, load_spec, registered_points, run_metrics
from .perf_artifacts import read_table
from .perf_bench import dump
from .r1_evaluation import _write, bootstrap
from .robustness import _workers, run_issuers, run_offsets
from .tranche_merge import result_folder

SPEC = 'research/v05/experiments/R3_structure.json'
NAMES = ['T4', 'N20', 'CAP15', 'T4_SEC3', 'T4_G']
ANCHOR_NAME = {'T4': 'A90', 'N20': 'A90', 'CAP15': 'A90', 'T4_SEC3': 'SEC3', 'T4_G': 'G'}
FIVE_BP = ['T4', 'N20', 'CAP15']  # structures that also run at 5 bp at starts 1 to 4 (10.4 (1))
BASE_NAMES = ['T4', 'N20', 'CAP15']  # structures that may become the round-4 base (10.5, first criterion set)
ADD_NAMES = ['T4_SEC3', 'T4_G']  # SEC3 or G that may join the base (10.5, second criterion set)
OFFSET_KS = (1, 2, 3, 4)
TOP = 3
INITIAL = 100000.0
THRESHOLD = 0.01  # 1 percentage point, as in 10.5


def _pc(v, nd=1):
    return '' if v in (None, '') else f'{100 * float(v):.{nd}f}%'


def _f(v):
    return None if v in (None, '') else float(v)


def _rows(path):
    if not Path(path).exists():
        return []
    with Path(path).open(encoding='utf-8', newline='') as f:
        import csv
        return list(csv.DictReader(f))


def _ids(root):
    """Names to IDs: the five structures of the registration, and the anchors they are compared with."""
    loaded = load_spec(root, SPEC)
    ids = {p['name']: p['id'] for p in loaded['grid']}
    anchors = {p['name']: p['anchor'] for p in loaded['grid']}
    ids.update({'A90': A90_ID, 'SEC3': 'L090_N10_b2_R2_sector_cap_dcda083d', 'G': 'Sa1d1bf7bce5a_L090_N10_b2_R2'})
    return loaded, ids, anchors


def _tranche(root, pid):
    p = registered_points(root).get(pid)
    if p is None and pid == A90_ID:
        return 0
    return (p or {}).get('tranche') or 0


def _result(folder, K):
    return result_folder(folder, dict(tranche=K))


def _folder(base, pid, cost, phase='explore'):
    return Path(base) / phase / pid / f'cost_{cost}bps'


# ---------------------------------------------------------------- runs (plan 10.4)

def top_issuers(root, folder, n=TOP):
    """The issuers with the largest profit over a run (or its merged account), largest first: (issuer, profit, share)."""
    issuer_of = {r['security_id']: r.get('issuer_id', '') for r in read_table(Path(root) / 'data/v05/normalized/securities.csv')}
    profit = {}
    for r in read_table(Path(folder) / 'daily_contributions.csv'):
        key = issuer_of.get(r['security_id'], r['security_id'])
        profit[key] = profit.get(key, 0.0) + float(r['profit_dollars'])
    total = sum(profit.values())
    ranked = sorted(profit.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return [dict(issuer=k, profit=v, share=v / total if total else None) for k, v in ranked]


def run_all(root, run, base, workers=1):
    """Offsets 1 to 4 (10 bp) for the five structures and the three anchors; 5 bp for the structures and A90; then the
    top-3 exclusion sets (each owner's own top three, applied to the owner, its anchor and A90)."""
    root, run, base = Path(root), Path(run), Path(base)
    loaded, ids, anchors = _ids(root)
    chain = [ids[n] for n in NAMES] + [ids['A90'], ids['SEC3'], ids['G']]
    chain = list(dict.fromkeys(chain))
    summary = dict(offsets10=run_offsets(root, base / 'offsets10', chain, offsets=OFFSET_KS, costs=(10,), workers=workers))
    five = [ids[n] for n in FIVE_BP] + [ids['A90']]
    summary['offsets5'] = run_offsets(root, base / 'offsets5', list(dict.fromkeys(five)), offsets=OFFSET_KS, costs=(5,), workers=workers)
    sets = {}
    for name in NAMES + ['A90']:
        owner = ids[name]
        K = _tranche(root, owner)
        sets[name] = top_issuers(root, _result(_folder(run, owner, 10, 'daily'), K))
    for name, rows in sets.items():
        issuers = [r['issuer'] for r in rows]
        owner = ids[name]
        members = [owner] + ([ids[ANCHOR_NAME[name]]] if name in ANCHOR_NAME else []) + [ids['A90']]
        members = list(dict.fromkeys(members))
        summary[f'issuer_{name}'] = run_issuers(root, base / 'issuer' / name, members, issuers, costs=(10,), workers=workers)
    dump(base / 'run_summary.json', summary)
    dump(base / 'top_sets.json', {name: rows for name, rows in sets.items()})
    return summary


# ---------------------------------------------------------------- evaluation (plan 10.5)

def _excess(folder, initial=INITIAL):
    m = _metrics_or_none(folder)
    return m, (m['annualized_excess'] if m else None)


def _series(run, pid, K, weeks, cost=10):
    eq = {r['session_date']: float(r['total_equity']) for r in read_table(_result(_folder(run, pid, cost), K) / 'daily_equity.csv')}
    e = np.array([eq[w] for w in weeks])
    return e[1:] / e[:-1] - 1


def noise_table(root, run, robust, ids, anchors):
    """Per object: 10 bp excess at starts k = 0..4, the spread, paired differences against the anchor (same start)."""
    rows = []
    objects = NAMES + ['A90', 'SEC3', 'G']
    for name in objects:
        pid = ids[name]
        K = _tranche(root, pid)
        vals = []
        for k in range(5):
            if k == 0:
                _, v = _excess(_result(_folder(run, pid, 10), K))
            else:
                _, v = _excess(_result(Path(robust) / 'offsets10' / f'offset_{k}' / pid / 'cost_10bps', K))
            vals.append(v)
        arr = [v for v in vals if v is not None]
        row = dict(object=name, id=pid, mean_k0_4=float(np.mean(arr)) if len(arr) == 5 else None,
                   min_k0_4=min(arr) if arr else None, max_k0_4=max(arr) if arr else None)
        row['range_k0_4'] = row['max_k0_4'] - row['min_k0_4'] if arr else None
        for k, v in enumerate(vals):
            row[f'excess10_k{k}'] = v
        anchor = anchors.get(name) if name in NAMES else None
        if anchor:
            aname = ANCHOR_NAME[name] if name in ANCHOR_NAME else anchor
            aid = ids[aname]
            aK = _tranche(root, aid)
            avals = []
            for k in range(5):
                folder = _result(_folder(run, aid, 10), aK) if k == 0 else _result(Path(robust) / 'offsets10' / f'offset_{k}' / aid / 'cost_10bps', aK)
                _, a = _excess(folder)
                avals.append(a)
            paired = [None if v is None or a is None else v - a for v, a in zip(vals, avals)]
            for k, p in enumerate(paired):
                row[f'paired10_k{k}'] = p
            ok = [p for p in paired if p is not None]
            row['paired10_mean'] = float(np.mean(ok)) if len(ok) == 5 else None
            row['anchor'] = aname
        rows.append(row)
    # 5 bp for T4, N20, CAP15 and A90 (the 10.5 first criterion needs five starts)
    for name in FIVE_BP + ['A90']:
        pid = ids[name]
        K = _tranche(root, pid)
        vals5 = []
        for k in range(5):
            folder = _result(_folder(run, pid, 5), K) if k == 0 else _result(Path(robust) / 'offsets5' / f'offset_{k}' / pid / 'cost_5bps', K)
            _, v = _excess(folder)
            vals5.append(v)
        for row in rows:
            if row['object'] == name:
                row.update({f'excess5_k{k}': v for k, v in enumerate(vals5)})
                aname = ANCHOR_NAME.get(name, None)
                if name == 'A90':
                    row['excess5_mean'] = float(np.mean(vals5)) if all(v is not None for v in vals5) else None
                    row['paired5_mean'] = None
                    continue
                aid = ids[ANCHOR_NAME[name]]
                aK = _tranche(root, aid)
                a5 = []
                for k in range(5):
                    folder = _result(_folder(run, aid, 5), aK) if k == 0 else _result(Path(robust) / 'offsets5' / f'offset_{k}' / aid / 'cost_5bps', aK)
                    _, a = _excess(folder)
                    a5.append(a)
                paired5 = [None if v is None or a is None else v - a for v, a in zip(vals5, a5)]
                row.update({f'paired5_k{k}': p for k, p in enumerate(paired5)})
                ok = [p for p in paired5 if p is not None]
                row['paired5_mean'] = float(np.mean(ok)) if len(ok) == 5 else None
                row['excess5_mean'] = float(np.mean(vals5)) if all(v is not None for v in vals5) else None
    return rows


def timing_table(root, run):
    """The four sub-accounts of each tranche structure at 10 bp (start k = 0): own return, own VOO benchmark, own excess."""
    rows = []
    for name in ['T4', 'T4_SEC3', 'T4_G']:
        pid = _ids(root)[1][name]
        K = _tranche(root, pid)
        for j in range(K):
            folder = _folder(run, pid, 10) / f'sub_{j}'
            m = _metrics_or_none(folder)
            m = run_metrics(folder, initial=INITIAL / K) if m else None
            rows.append(dict(structure=name, sub_account=j, annualized_return=m['annualized_return'] if m else None,
                             benchmark_annualized=m['benchmark_annualized'] if m else None,
                             annualized_excess=m['annualized_excess'] if m else None,
                             max_drawdown=m['max_drawdown'] if m else None,
                             first_trade=json.loads((folder / 'summary.json').read_text(encoding='utf-8'))['comparison_start_session']))
    return rows


def exclusion_table(root, robust, ids, anchors):
    """Each owner's own top three issuers, removed together with the owner, its anchor and A90 (10 bp, explore)."""
    rows = []
    sets = json.loads((Path(robust) / 'top_sets.json').read_text(encoding='utf-8'))
    for name in NAMES + ['A90']:
        owner = ids[name]
        members = [owner, ids[ANCHOR_NAME[name]] if name in ANCHOR_NAME else None, ids['A90']]
        base_folder = Path(robust) / 'issuer' / name
        a90_val = None
        for pid in dict.fromkeys(m for m in members if m):
            K = _tranche(root, pid)
            _, v = _excess(_result(base_folder / pid / 'cost_10bps', K))
            if pid == ids['A90']:
                a90_val = v
            rows.append(dict(owner=name, excluded=';'.join(r['issuer'] for r in sets[name]), id=pid, excess10_excluded=v))
        for row in rows:
            if row['owner'] == name:
                row['A90_excluded'] = a90_val
                row['paired_vs_A90_excluded'] = None if row['excess10_excluded'] is None or a90_val is None else row['excess10_excluded'] - a90_val
    return rows


def random_table(root, run, ids, exc10, seeds=100):
    """Experiment-level random controls: per seed one draw per structure; the best of five, and the best paired."""
    best_max, best_pair = [], []
    actual = max(exc10[ids[n]] for n in NAMES)
    actual_pair = max(exc10[ids[n]] - exc10[ids[ANCHOR_NAME[n]]] for n in NAMES)
    rows = []
    for s in range(seeds):
        vals, paired = [], []
        for n in NAMES:
            pid = ids[n]
            K = _tranche(root, pid)
            m = _metrics_or_none(_result(Path(run) / 'null' / pid / f'NULL_{pid}_s{s:03d}' / 'cost_10bps', K))
            if m is not None:
                vals.append(m['annualized_excess'])
                paired.append(m['annualized_excess'] - exc10[ids[ANCHOR_NAME[n]]])
        full = len(vals) == len(NAMES)
        best_max.append(max(vals) if full else None)
        best_pair.append(max(paired) if full else None)
        rows.append(dict(seed=s, controls_completed=len(vals), best_null_excess=max(vals) if vals else None,
                         best_null_paired=max(paired) if paired else None))
    full_max = [v for v in best_max if v is not None]
    full_pair = [v for v in best_pair if v is not None]
    summary = dict(seeds=seeds, seeds_with_all_5_controls=len(full_max), actual_best_excess_10bps=actual,
                   percentile_of_actual_best=100 * sum(v <= actual for v in full_max) / len(full_max) if full_max else None,
                   actual_best_paired_10bps=actual_pair,
                   percentile_of_actual_best_paired=100 * sum(v <= actual_pair for v in full_pair) / len(full_pair) if full_pair else None,
                   null_best_excess_median=float(np.median(full_max)) if full_max else None,
                   null_best_paired_median=float(np.median(full_pair)) if full_pair else None)
    return rows, summary


def pair_bootstrap(root, run, ids, weeks):
    """Paired block-bootstrap of weekly differences: each structure against its anchor; T4_SEC3 and T4_G against T4."""
    rows = []
    comparisons = [(n, ANCHOR_NAME[n]) for n in NAMES if n in ANCHOR_NAME] + [('T4_SEC3', 'T4'), ('T4_G', 'T4')]
    for name, anchor in comparisons:
        diff = _series(run, ids[name], _tranche(root, ids[name]), weeks) - _series(run, ids[anchor], _tranche(root, ids[anchor]), weeks)
        draws = bootstrap(diff)
        rows.append(dict(name=name, anchor=anchor, weeks=len(diff), point_annualized_diff=float(np.mean(diff)) * 52,
                         p05=float(np.percentile(draws, 5)), p95=float(np.percentile(draws, 95)),
                         method='moving block, block 8 weeks, 2000 draws, seed 20261026; annualised as weekly mean x 52; exploratory'))
    return rows


def criteria(root, run, robust, ids, noise, exclusion):
    """Plan 10.5, mechanically: the three base conditions for T4, N20, CAP15 against A90; the base; the SEC3 / G conditions."""
    by = {r['object']: r for r in noise}
    ex = {(r['owner'], r['id']): r for r in exclusion}
    base_rows = []
    for name in BASE_NAMES:
        r = by[name]
        mean10_paired = r['paired10_mean']
        mean5_paired = r['paired5_mean']
        a90 = by['A90']
        range_ok = r['range_k0_4'] < a90['range_k0_4']
        excl = ex.get((name, ids['A90']))
        pair3 = excl['paired_vs_A90_excluded'] if excl else None
        c1 = mean10_paired is not None and mean5_paired is not None and mean10_paired >= -THRESHOLD and mean5_paired >= -THRESHOLD
        c3 = pair3 is not None and pair3 > 0
        base_rows.append(dict(structure=name, mean10_paired_vs_A90=mean10_paired, mean5_paired_vs_A90=mean5_paired,
                              c1=c1, range10=r['range_k0_4'], range10_A90=a90['range_k0_4'], c2=range_ok,
                              paired_excluded=pair3, c3=c3, all_three=c1 and range_ok and c3,
                              mean10_excess_k0_4=r['mean_k0_4']))
    passing = [b for b in base_rows if b['all_three']]
    base = max(passing, key=lambda b: b['mean10_excess_k0_4'])['structure'] if passing else 'A90'
    add_rows = []
    for name in ADD_NAMES:
        r = by[name]
        t4 = by['T4']
        paired = [r[f'paired10_k{k}'] for k in range(5)]
        ok = [p for p in paired if p is not None]
        s1 = r['paired10_mean'] is not None and r['paired10_mean'] >= THRESHOLD
        s2 = sum(1 for p in ok if p > 0) >= 4
        p_a = _period_diff(root, run, ids, name, 'high_missing_excess')
        p_b = _period_diff(root, run, ids, name, 'lower_missing_excess')
        s3 = p_a is not None and p_b is not None and (p_a > 0) == (p_b > 0)
        add_rows.append(dict(structure=name, anchor='T4', mean10_paired_vs_T4=r['paired10_mean'], positive_starts=sum(1 for p in ok if p > 0),
                             period_a_paired=p_a, period_b_paired=p_b, s1=s1, s2=s2, s3=s3, all_three=s1 and s2 and s3,
                             base_is_T4=base == 'T4'))
    admitted = None
    if base == 'T4':
        both = [a for a in add_rows if a['all_three']]
        if both:
            admitted = max(both, key=lambda a: a['mean10_paired_vs_T4'])['structure']
    return dict(base_rows=base_rows, base=base, add_rows=add_rows, admitted=admitted)


def _period_diff(root, run, ids, name, key):
    a = _metrics_or_none(_result(_folder(run, ids[name], 10), _tranche(root, ids[name])))
    b = _metrics_or_none(_result(_folder(run, ids['T4'], 10), _tranche(root, ids['T4'])))
    if not a or not b:
        return None
    return a[key] - b[key]


def cap_events(root, run, ids):
    """Single-stock cap trims of CAP15: number of trims and the sold notional, from the daily run's trades."""
    folder = _result(_folder(run, ids['CAP15'], 10, 'daily'), 0)
    trims = [r for r in read_table(folder / 'trades.csv') if r['reason'] == 'cap_trim']
    notional = sum(float(r['shares']) * float(r['fill_price']) for r in trims)
    weeks = sorted({r['signal_session_date'] for r in trims})
    return dict(trims=len(trims), weeks_with_trims=len(weeks), sold_notional=notional,
                first_trim=min((r['session_date'] for r in trims), default=None),
                last_trim=max((r['session_date'] for r in trims), default=None))


def main(argv=None):
    ap = argparse.ArgumentParser(description='Round 3 robustness runs and evaluation')
    sub = ap.add_subparsers(dest='mode', required=True)
    runs = sub.add_parser('runs', help='offsets, 5 bp starts and top-3 exclusion runs')
    runs.add_argument('--run', default='work/runs/experiments/R3_structure')
    runs.add_argument('--out', required=True)
    runs.add_argument('--workers', default='1')
    ev = sub.add_parser('evaluate', help='tables and the discussion pack')
    ev.add_argument('--run', required=True)
    ev.add_argument('--robust', required=True)
    ev.add_argument('--out', required=True)
    args = ap.parse_args(argv)
    root = Path('.').resolve()
    if args.mode == 'runs':
        from .perf_cache import identity
        from .perf_runner import safe_output
        base = safe_output(root, args.out)
        result = run_all(root, root / args.run, base, _workers(args.workers))
        print(json.dumps({k: str(v)[:200] for k, v in result.items()}, ensure_ascii=False, indent=2))
        return 0
    from .r3_pack import evaluate
    result = evaluate(root, root / args.run, root / args.robust, root / args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
