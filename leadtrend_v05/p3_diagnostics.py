"""P3 diagnostics (plan 9.3(1)-(4)): exposure-matched controls, style attribution, per-issuer exclusion, VOO rebuild.

Read-only on the committed registrations and the run folders. Runs and intermediate files go to work/; the caller
copies the final tables to research/v05/diagnostics/P3/. None of this is a registration or an attempt, and nothing
here chooses a configuration.

Objects: A90, SEC3, SIZE, G (exposure-matched controls and issuer exclusion), with WGT as the reference (attribution).
"""
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from . import paths
from .experiments import _metrics_or_none, null_band, reference_exit_counts
from .diagnostics import benchmark_tri, make_tri_fn, weekly_records
from .drawdown import load_dtb3
from .perf_artifacts import read_table
from .perf_cache import build_context, ensure_signal_tables, identity
from .robustness import configurations, run_issuers, _context, _run_jobs
from .sectors import load_sector_map, sector_key

OBJECTS = {'A90': 'L090_N10_b2_R2', 'SEC3': 'L090_N10_b2_R2_sector_cap_dcda083d',
           'SIZE': 'Sd3dcdf21323b_L090_N10_b2_R2', 'G': 'Sa1d1bf7bce5a_L090_N10_b2_R2',
           'WGT': 'S7a7dc02d2f2e_L090_N10_b2_R2'}
MATCH_OBJECTS = ('A90', 'SEC3', 'SIZE', 'G')
ATTRIBUTION_OBJECTS = ('A90', 'SEC3', 'SIZE', 'G', 'WGT')
SECTOR_CAP_OBJECTS = {'SEC3': 3}  # the sector cap each object inherits (plan 9.3(1))
MODES = {'size': 'random_size_matched', 'sector': 'random_sector_matched'}
SEEDS = 100
NVDA = 'LEI:549300S4KLFTLO7GSQ80'
TECH = '信息技术'
TOP_N = (3, 5)
COST = 10
BOOT_NW_LAG = 4


# ---------------------------------------------------------------- 1. exposure-matched controls

def pool_table(ctx, records, sector_map):
    """One row per scorable pool name per signal week: VOO-weight quintile (1 = highest), mapped sector, issuer weight."""
    rows = []
    for rec in records:
        d = rec['t']
        cands = ctx.frames[d]['windows'][90]['candidates']
        w = {c.security_id: rec['issuer_weight'].get(c.security_id, 0.0) for c in cands}
        ordered = sorted(cands, key=lambda c: (-w[c.security_id], c.security_id))
        n = len(ordered)
        for i, c in enumerate(ordered):
            industry = rec['industry'][c.security_id]
            rows.append(dict(signal_session_date=d.isoformat(), security_id=c.security_id, quintile=str(i * 5 // n + 1),
                             sector=sector_key(industry, sector_map)[0], issuer_weight=w[c.security_id]))
    return rows


def match_files(pools, reference_buys, out):
    """Write one match file per object and mode: pool groups and whether the object's reference bought the name that week."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    paths_by = {}
    for obj in MATCH_OBJECTS:
        buys = reference_buys[obj]
        for mode, col in (('size', 'quintile'), ('sector', 'sector')):
            path = out / f'match_{obj}_{mode}.csv'
            with path.open('w', encoding='utf-8', newline='') as f:
                w = csv.writer(f)
                w.writerow(['signal_session_date', 'security_id', 'group', 'buy'])
                for r in pools:
                    buy = int((r['signal_session_date'], r['security_id']) in buys)
                    w.writerow([r['signal_session_date'], r['security_id'], r[col], buy])
            paths_by[(obj, mode)] = path
    return paths_by


def reference_buys(run, pid):
    """(signal date, security) of every buy the reference configuration made, from its own 10 bp explore run."""
    out = set()
    for r in read_table(Path(run) / 'explore' / pid / f'cost_{COST}bps' / 'trades.csv'):
        if r['side'] == 'buy':
            out.add((r['signal_session_date'], r['security_id']))
    return out


def control_point(obj_point, mode, seed, exit_counts, match_file, sector_k=None):
    params = dict(seed=seed, exit_counts=exit_counts, match_file=str(match_file))
    if sector_k is not None:
        params['sector_k'] = sector_k
    return dict(id=f"{obj_point['id']}__{MODES[mode]}_s{seed:03d}", signal=obj_point['signal'], L=obj_point['L'],
                N=obj_point['N'], k=obj_point['k'], b=obj_point['b'], rebalance=obj_point['rebalance'],
                selection=MODES[mode], selection_params=params, name=f"{mode}_{obj_point['id']}")


def run_matched(root, base, run, match_paths, workers=1):
    """800 controls: 4 objects x 2 modes x 100 seeds, each with the object's own exit counts and sector cap."""
    root, base, run = Path(root), Path(base), Path(run)
    doc = identity(root, 'uniform')
    points = configurations(root, [OBJECTS[o] for o in MATCH_OBJECTS])
    by_obj = {o: p for o, p in zip(MATCH_OBJECTS, points)}
    ensure_signal_tables(root, doc, [p['signal'] for p in points])
    controls, jobs = [], []
    for obj in MATCH_OBJECTS:
        exits = reference_exit_counts(run / 'explore' / OBJECTS[obj] / f'cost_{COST}bps')
        for mode in MODES:
            for seed in range(SEEDS):
                cp = control_point(by_obj[obj], mode, seed, exits, match_paths[(obj, mode)], SECTOR_CAP_OBJECTS.get(obj))
                controls.append(cp)
                folder = base / mode / obj / f'seed_{seed:03d}' / f'cost_{COST}bps'
                jobs.append((dict(cp, experiment=dict(id='P3_MATCHED', kind='diagnostic_control', object=obj, mode=mode)),
                             COST, str(folder), 'explore', False, 'explore'))
    payload = dict(points=controls, costs=[COST], experiment=dict(id='P3_MATCHED', kind='diagnostic_control'))
    ctx = _context(root, doc, payload)
    executed = _run_jobs(jobs, ctx, workers, payload, None)
    return dict(controls=len(controls), executed=executed)


def matched_summary(run, base):
    """Percentiles and medians of each object's 10 bp excess among 100 controls, next to the current random control."""
    rows = []
    for obj in MATCH_OBJECTS:
        pid = OBJECTS[obj]
        actual = _metrics_or_none(run / 'explore' / pid / f'cost_{COST}bps')['annualized_excess']
        current = [m['annualized_excess'] for s in range(SEEDS) if (m := _metrics_or_none(
            run / 'null' / pid / f'NULL_{pid}_s{s:03d}' / f'cost_{COST}bps'))]
        rows.append(_band_row(obj, 'current_random', actual, current))
        for mode in MODES:
            vals, fallbacks = [], Counter()
            for s in range(SEEDS):
                folder = base / mode / obj / f'seed_{s:03d}' / f'cost_{COST}bps'
                m = _metrics_or_none(folder)
                if m is None:
                    continue
                vals.append(m['annualized_excess'])
                manifest = json.loads((folder / 'run_manifest.json').read_text(encoding='utf-8'))
                for key, count in manifest.get('selection_counts', {}).get(f'{COST}bps', {}).items():
                    fallbacks[key] += count
            row = _band_row(obj, mode + '_matched', actual, vals)
            row.update({k: round(v / max(1, len(vals)), 3) for k, v in fallbacks.items()})
            rows.append(row)
    return rows


def _band_row(obj, kind, actual, values):
    point, low, high = null_band(actual, values, SEEDS)
    return dict(object=obj, control=kind, actual_excess_10bps=actual, completed=len(values), registered=SEEDS,
                percentile_point=point, percentile_low=low, percentile_high=high,
                median_control_excess_10bps=float(np.median(values)) if values else '',
                mean_control_excess_10bps=float(np.mean(values)) if values else '')


# ---------------------------------------------------------------- 2. style attribution (descriptive)

def ols_newey_west(y, X, lag=BOOT_NW_LAG):
    """OLS with a constant; Newey-West (Bartlett kernel) errors with the given lag. Returns coefficients, t, R2."""
    y = np.asarray(y, dtype=float)
    X = np.column_stack([np.ones(len(y)), np.asarray(X, dtype=float)])
    xtx_inv = np.linalg.inv(X.T @ X)
    beta = xtx_inv @ (X.T @ y)
    e = y - X @ beta
    S = (X * e[:, None]).T @ (X * e[:, None])
    for lag_k in range(1, lag + 1):
        w = 1 - lag_k / (lag + 1)
        G = (X[lag_k:] * e[lag_k:, None]).T @ (X[:-lag_k] * e[:-lag_k, None])
        S = S + w * (G + G.T)
    V = xtx_inv @ S @ xtx_inv
    tvals = beta / np.sqrt(np.diag(V))
    r2 = 1 - float(e @ e) / float(((y - y.mean()) @ (y - y.mean())))
    return dict(coef=beta, t=tvals, r2=r2)


def _ret(tri_fn, sids, d0, d1):
    out = {}
    for sid in sids:
        a, b = tri_fn(sid, d0), tri_fn(sid, d1)
        if a and b and a > 0:
            out[sid] = b / a - 1
    return out


def attribution(root, ctx, records, run, out, voo_tri=None):
    """Weekly descriptive regression of each object's excess over VOO on four factors (9.3(2))."""
    from .eligibility import eligibility_index
    root, out = Path(root), Path(out)
    dtb3 = load_dtb3(root)
    sector_map = load_sector_map(root)
    elig = eligibility_index(ctx.full)
    voo_tri = voo_tri or benchmark_tri(ctx)
    tri_fn = make_tri_fn(ctx, voo_tri)
    series = {}
    for obj in ATTRIBUTION_OBJECTS:
        eq = {r['session_date']: float(r['total_equity']) for r in read_table(run / 'explore' / OBJECTS[obj] / f'cost_{COST}bps' / 'daily_equity.csv')}
        series[obj] = eq
    bm = {r['session_date']: float(r['benchmark_value']) for r in read_table(run / 'explore' / OBJECTS['A90'] / f'cost_{COST}bps' / 'benchmark.csv')}
    weeks = sorted({r['signal_session_date'] for r in read_table(run / 'explore' / OBJECTS['A90'] / f'cost_{COST}bps' / 'weekly_checks.csv')})
    recs = {r['t'].isoformat(): r for r in records}
    rows = []
    for i in range(len(weeks) - 1):
        d0, d1 = weeks[i], weeks[i + 1]
        if d0 not in recs or d1 not in recs:
            continue
        rec = recs[d0]
        days = (date_of(d1) - date_of(d0)).days
        rate = dtb3_on(dtb3, d0)
        rf = (rate / 100 / 360) * days if rate is not None else None
        voo_r = bm[d1] / bm[d0] - 1
        pool = [c for c in rec['pool']]
        iw = rec['issuer_weight']
        ret_all = _ret(tri_fn, pool, date_of(d0), date_of(d1))
        ew = float(np.mean(list(ret_all.values()))) if ret_all else None
        top = sorted({sid for sid in pool}, key=lambda s: (-iw.get(s, 0.0), s))
        mega_names, seen_issuers = [], set()
        issuer_of = {sid: elig[sid].issuer_id for sid in pool}
        for sid in top:
            key = issuer_of.get(sid, sid)
            if key in seen_issuers:
                continue
            seen_issuers.add(key)
            mega_names.append(sid)
            if len(mega_names) == 20:
                break
        mw = np.array([iw.get(s, 0.0) for s in mega_names], dtype=float)
        mr = np.array([ret_all.get(s, np.nan) for s in mega_names], dtype=float)
        ok = np.isfinite(mr) & (mw > 0)
        mega = float(np.sum(mw[ok] * mr[ok]) / np.sum(mw[ok])) if ok.any() else None
        tech = [s for s in pool if sector_key(rec['industry'][s], sector_map)[0] == TECH and s in ret_all]
        tw = np.array([iw.get(s, 0.0) for s in tech], dtype=float)
        tr = np.array([ret_all[s] for s in tech], dtype=float)
        tech_r = float(np.sum(tw * tr) / np.sum(tw)) if tech and np.sum(tw) > 0 else None
        row = dict(week_start=d0, week_end=d1, voo_week=voo_r, rf_week=rf, equal_weight_pool=ew, megacap20=mega, tech=tech_r)
        for obj in ATTRIBUTION_OBJECTS:
            eq = series[obj]
            row[f'{obj}_excess_week'] = (eq[d1] / eq[d0] - 1) - voo_r if d0 in eq and d1 in eq else None
        rows.append(row)
    usable = [r for r in rows if None not in (r['voo_week'], r['rf_week'], r['equal_weight_pool'], r['megacap20'], r['tech'])]
    factors = {
        'VOO_minus_rf': [r['voo_week'] - r['rf_week'] for r in usable],
        'megacap20_minus_VOO': [r['megacap20'] - r['voo_week'] for r in usable],
        'equal_weight_minus_VOO': [r['equal_weight_pool'] - r['voo_week'] for r in usable],
        'tech_minus_VOO': [r['tech'] - r['voo_week'] for r in usable],
    }
    X = np.column_stack(list(factors.values()))
    corr = np.corrcoef(X, rowvar=False)
    regs = []
    for obj in ATTRIBUTION_OBJECTS:
        pairs = [(r[f'{obj}_excess_week'], k) for r, k in zip(usable, range(len(usable))) if r.get(f'{obj}_excess_week') is not None]
        yy = [p[0] for p in pairs]
        XX = X[[p[1] for p in pairs]]
        res = ols_newey_west(yy, XX)
        regs.append(dict(object=obj, weeks=len(yy), alpha_annualised=float(res['coef'][0]) * 52, alpha_t=float(res['t'][0]),
                         **{f'coef_{name}': float(res['coef'][i + 1]) for i, name in enumerate(factors)},
                         **{f't_{name}': float(res['t'][i + 1]) for i, name in enumerate(factors)}, r2=float(res['r2'])))
    out.mkdir(parents=True, exist_ok=True)
    _write(out / 'attribution_weekly.csv', rows)
    _write(out / 'attribution_regressions.csv', regs)
    _write(out / 'attribution_factor_correlation.csv',
           [dict(factor=a, **{b: float(corr[i, j]) for j, b in enumerate(factors)}) for i, a in enumerate(factors)])
    return dict(weeks_used=len(usable), regressions=regs, correlation=corr.tolist(), factors=list(factors))


def date_of(iso):
    from datetime import date
    return date.fromisoformat(iso)


def dtb3_on(rates, iso):
    """The latest DTB3 observation on or before iso, in percent a year."""
    best = None
    for d in sorted(rates):
        if d <= iso:
            best = rates[d]
        else:
            break
    return best


# ---------------------------------------------------------------- 3. per-issuer exclusion

def held_issuers(root, run, pid):
    """Issuers the object held in its 10 bp daily run (positions with shares), from positions.csv."""
    issuer_of = {r['security_id']: r.get('issuer_id', '') for r in read_table(Path(root) / paths.NORMALIZED / 'securities.csv')}
    held = set()
    for r in read_table(Path(run) / 'daily' / pid / f'cost_{COST}bps' / 'positions.csv'):
        if r.get('asset_type', 'actual_shares') == 'actual_shares' and float(r['shares']) > 0:
            held.add(issuer_of.get(r['security_id'], r['security_id']))
    return held


def issuer_profits(root, run, pid):
    """Issuer -> profit dollars over the whole run (daily_contributions), largest first."""
    issuer_of = {r['security_id']: r.get('issuer_id', '') for r in read_table(Path(root) / paths.NORMALIZED / 'securities.csv')}
    profit = defaultdict(float)
    for r in read_table(Path(run) / 'daily' / pid / f'cost_{COST}bps' / 'daily_contributions.csv'):
        profit[issuer_of.get(r['security_id'], r['security_id'])] += float(r['profit_dollars'])
    total = sum(profit.values())
    ordered = sorted(profit.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered, total


def _slug(text):
    return ''.join(c if c.isalnum() else '_' for c in text)


def run_issuer_exclusions(root, base, run, workers=1):
    """Each issuer any of the four objects held (in its daily run) removed once; the object and A90 both, paired.
    Then the top-3 and top-5 contributors of each object removed together, with A90 too."""
    root, base, run = Path(root), Path(base), Path(run)
    held = {obj: held_issuers(root, run, OBJECTS[obj]) for obj in MATCH_OBJECTS}
    union = sorted(set().union(*held.values()))
    done = []
    for issuer in union:
        ids = list(dict.fromkeys([OBJECTS['A90']] + [OBJECTS[o] for o in MATCH_OBJECTS if issuer in held[o]]))
        r = run_issuers(root, base / 'per_issuer' / _slug(issuer), ids, [issuer], costs=(COST,), workers=workers)
        done.append(dict(issuer=issuer, configurations=len(ids), executed=r['executed']))
    tops = {}
    for obj in MATCH_OBJECTS:
        ordered, total = issuer_profits(root, run, OBJECTS[obj])
        for n in TOP_N:
            names = [k for k, _ in ordered[:n]]
            tops[(obj, n)] = names
            ids = list(dict.fromkeys([OBJECTS[obj], OBJECTS['A90']]))
            run_issuers(root, base / 'top' / f'{obj}_top{n}', ids, names, costs=(COST,), workers=workers)
    return dict(issuers=len(union), per_issuer=done, tops={f'{k[0]}_top{k[1]}': v for k, v in tops.items()})


def exclusion_table(root, run, base):
    """Per object: the paired difference against A90 (same issuer removed) across the issuers it held."""
    root, base, run = Path(root), Path(base), Path(run)
    rows = []
    for obj in MATCH_OBJECTS:
        pid = OBJECTS[obj]
        before = _metrics_or_none(run / 'explore' / pid / f'cost_{COST}bps')['annualized_excess']
        a90_before = _metrics_or_none(run / 'explore' / OBJECTS['A90'] / f'cost_{COST}bps')['annualized_excess']
        per = []
        for issuer in sorted(held_issuers(root, run, pid)):
            folder = base / 'per_issuer' / _slug(issuer)
            after = _metrics_or_none(folder / pid / f'cost_{COST}bps')
            a90 = _metrics_or_none(folder / OBJECTS['A90'] / f'cost_{COST}bps')
            if after is None or a90 is None:
                continue
            per.append((issuer, after['annualized_excess'], after['annualized_excess'] - a90['annualized_excess']))
        if not per:
            continue
        paired = [p[2] for p in per]
        worst = min(per, key=lambda p: p[2])
        rows.append(dict(object=obj, issuers_tested=len(per), excess_before=before, paired_before=before - a90_before,
                         paired_median=float(np.median(paired)), paired_p10=float(np.percentile(paired, 10)),
                         paired_min=worst[2], worst_issuer=worst[0], excess_median_after=float(np.median([p[1] for p in per])),
                         excess_min_after=min(p[1] for p in per)))
    return rows


def top_exclusion_table(root, run, base):
    rows = []
    for obj in MATCH_OBJECTS:
        for n in TOP_N:
            folder = base / 'top' / f'{obj}_top{n}'
            after = _metrics_or_none(folder / OBJECTS[obj] / f'cost_{COST}bps')
            a90 = _metrics_or_none(folder / OBJECTS['A90'] / f'cost_{COST}bps')
            before = _metrics_or_none(run / 'explore' / OBJECTS[obj] / f'cost_{COST}bps')
            a90b = _metrics_or_none(run / 'explore' / OBJECTS['A90'] / f'cost_{COST}bps')
            if None in (after, a90, before, a90b):
                continue
            rows.append(dict(object=obj, removed_top=n, excess_before=before['annualized_excess'],
                             excess_after=after['annualized_excess'], paired_before=before['annualized_excess'] - a90b['annualized_excess'],
                             paired_after=after['annualized_excess'] - a90['annualized_excess']))
    return rows


# ---------------------------------------------------------------- 4. VOO rebuilt from N-PORT weights

def voo_rebuild(root, ctx, records, exclude_issuer=None):
    """Weekly VOO-like index: the snapshot weights of each week times the names' TRI returns to the next signal week."""
    root = Path(root)
    voo_tri = benchmark_tri(ctx)
    tri_fn = make_tri_fn(ctx, voo_tri)
    issuer_of = {r['security_id']: r.get('issuer_id', '') for r in read_table(root / paths.NORMALIZED / 'securities.csv')}
    rows = []
    for i in range(len(records) - 1):
        rec, nxt = records[i], records[i + 1]
        d0, d1 = rec['t'], nxt['t']
        weights = {sid: w for sid, w in rec['member_weight'].items() if w > 0}
        if exclude_issuer:
            weights = {sid: w for sid, w in weights.items() if issuer_of.get(sid) != exclude_issuer}
        rets = {}
        for sid, w in weights.items():
            a, b = tri_fn(sid, d0), tri_fn(sid, d1)
            if a and b and a > 0:
                rets[sid] = (w, b / a - 1)
        total_w = sum(w for w, _ in rets.values())
        r_idx = sum(w * r for w, r in rets.values()) / total_w if total_w else None
        a, b = voo_tri.get(d0), voo_tri.get(d1)
        r_voo = b / a - 1 if a and b else None
        rows.append(dict(week_start=d0.isoformat(), week_end=d1.isoformat(), index_week=r_idx, voo_week=r_voo,
                         names_used=len(rets), weight_used=total_w))
    return rows


def rebuild_summary(rows):
    usable = [r for r in rows if r['index_week'] is not None and r['voo_week'] is not None]
    years = len(usable) / 52
    idx_ann = math.prod(1 + r['index_week'] for r in usable) ** (1 / years) - 1
    voo_ann = math.prod(1 + r['voo_week'] for r in usable) ** (1 / years) - 1
    diff = [r['index_week'] - r['voo_week'] for r in usable]
    te = float(np.std(diff, ddof=1)) * math.sqrt(52)
    return dict(weeks=len(usable), index_annualised=idx_ann, voo_annualised=voo_ann,
                annualised_difference=idx_ann - voo_ann, tracking_error=te, above_half_point=abs(idx_ann - voo_ann) > 0.005,
                mean_names=float(np.mean([r['names_used'] for r in usable])))


def _write(path, rows):
    rows = list(rows)
    if not rows:
        Path(path).write_text('', encoding='utf-8')
        return
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with Path(path).open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
