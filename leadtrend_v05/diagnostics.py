"""Diagnostics D0: where the negative excess comes from. Measurements only; nothing is registered or ranked.

Development segment only: every date used is 2024-06-28 or earlier. A forward quantity whose end falls after that
date is dropped, never read. The diagnostics do not change any existing module; they read the development context
built by perf_cache.build_context, read finished stage 4 runs, and (D5) run random_matched controls under
DiagnosticContext, which admits only registered stage 4 configurations. None of this counts as an attempt.
"""
import csv
import math
import multiprocessing
import os
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median

import numpy as np

from . import paths
from .experiments import ExperimentContext, _metrics_or_none, null_band, reference_exit_counts, run_metrics
from .perf_artifacts import expanded_bytes, read_table
from .perf_bench import Timer, dump
from .signals import normalize
from .stage4_registry import points as stage4_points

DEV_START = '2019-11-29'
DEV_END = '2024-06-28'
END_DATE = date(2024, 6, 28)
WINDOWS = [10, 20, 40, 60, 90, 126]
HORIZONS = [1, 4, 8]
MIN_POOL = 10          # a week enters an IC or a quintile only with at least this many securities
TOP_N = 8
D3_CONFIGS = ['L090_N08_b2_R2', 'L090_N08_b2_R1', 'L010_N08_b2_R2', 'L040_N08_b2_R2']
D5_CONFIGS = [f'L{L:03d}_N08_b2_R2' for L in WINDOWS]
D5_RUNS = 100
SECTORS = ['信息技术', '通信服务', '可选消费', '必需消费', '医疗保健', '金融', '工业', '能源', '材料', '公用事业', '房地产']
CORPORATE = {'split', 'reverse_split', 'cash_dividend', 'spinoff'}
DIAG_EXPERIMENT = {'id': 'D0_diagnostic_null', 'registration_commit': None, 'spec_sha256': None}


# ---------------------------------------------------------------- statistics

def average_ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(x, y):
    """Rank correlation with average ranks for ties; None when undefined."""
    if len(x) < 3 or len(x) != len(y):
        return None
    rx, ry = average_ranks(x), average_ranks(y)
    mx, my = mean(rx), mean(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def newey_west_t(series, lag):
    """t statistic of the mean of a time-ordered series; Newey-West variance with Bartlett weights, bandwidth lag."""
    n = len(series)
    if n < 3:
        return None
    m = sum(series) / n
    u = [x - m for x in series]

    def gamma(j):
        return sum(u[t] * u[t - j] for t in range(j, n)) / n
    var = gamma(0) + 2 * sum((1 - j / (lag + 1)) * gamma(j) for j in range(1, min(lag, n - 1) + 1))
    if var <= 0:
        return None
    return m / math.sqrt(var / n)


def annualize(cumulative, days):
    years = days / 365.25
    return (1 + cumulative) ** (1 / years) - 1 if years > 0 else None


def sector_stat(values, fn):
    return fn(values) if values else None


# ---------------------------------------------------------------- sector map

def load_sector_map(path):
    """industry value -> sector from config/sector_map_v1.csv (first line is a comment)."""
    lines = Path(path).read_text(encoding='utf-8').splitlines()
    rows = list(csv.DictReader(line for line in lines if not line.startswith('#')))
    return {r['industry_value']: r['sector'] for r in rows if not r['industry_value'].endswith(':*')}


def sector_of(industry, mapping):
    if industry.startswith('UNKNOWN'):
        return 'UNKNOWN'
    return mapping.get(industry, 'UNMAPPED')


# ---------------------------------------------------------------- weekly records (development context)

def weekly_records(ctx):
    """One record per signal week in the development segment, in time order; nothing after ctx.end."""
    from .calendar import Calendar
    from .eligibility import eligibility_index
    from .universe import choose_snapshot
    elig = eligibility_index(ctx.full)
    cal = Calendar(ctx.full.calendar)
    session_dates = [s.session_date for s in cal.sessions]
    records = []
    for s in cal.week_signal_sessions():
        d = s.session_date
        if not ctx.start <= d <= ctx.end:
            continue
        frame = ctx.frames[d]
        choice = choose_snapshot(ctx.full, s.close_time_utc)
        member_weight = {m.security_id: float(m.weight or 0.0) for m in choice.members}
        pool = sorted(frame['chosen'])
        i = session_dates.index(d)
        nxt_exec = session_dates[i + 1] if i + 1 < len(session_dates) and session_dates[i + 1] <= ctx.end else None
        issuer_of = {sid: elig[sid].issuer_id for sid in member_weight}
        records.append(dict(
            t=d, pool=pool, issuer_weight=pool_issuer_weights(member_weight, issuer_of, pool),
            industry={sid: elig[sid].industry for sid in pool},
            member_weight=member_weight, total_weight=sum(member_weight.values()),
            scores={L: {r['security_id']: r['score'] for r in frame['windows'][L]['scores']} for L in WINDOWS},
            exec=nxt_exec))
    return records


def pool_issuer_weights(member_weight, issuer_of, pool):
    """CW weight of each pool security: the snapshot weights of all classes of its issuer, carried by the selected class."""
    totals = defaultdict(float)
    for sid, w in member_weight.items():
        totals[issuer_of[sid]] += w
    return {sid: totals[issuer_of[sid]] for sid in pool}


def make_tri_fn(ctx, voo_tri):
    engine = ctx.prepared[-1]

    def tri(sid, d):
        if sid == ctx.full.config.benchmark_security_id:
            return voo_tri.get(d)
        result = engine.tri.get(sid)
        return None if result is None else result.tri.get(d)
    return tri


def benchmark_tri(ctx):
    from .scoring import build_tri
    from .calendar import Calendar
    bench = ctx.full.config.benchmark_security_id
    prices = [p for p in ctx.full.prices if p.security_id == bench]
    events = [e for e in ctx.full.events if e.security_id == bench]
    dates = [s.session_date for s in Calendar(ctx.full.calendar).sessions if s.session_date <= ctx.end]
    return build_tri(prices, events, dates).tri


# ---------------------------------------------------------------- D1 reference portfolios

def week_returns(rec, nxt, tri_fn):
    """Per security in the week's pool: simple return to the next signal week, its weight, and missing counts."""
    ret, weight, n_entry, n_exit = {}, {}, 0, 0
    for sid in rec['pool']:
        a = tri_fn(sid, rec['t'])
        if a is None:
            n_entry += 1
            continue
        b = tri_fn(sid, nxt)
        if b is None:
            n_exit += 1
            continue
        ret[sid] = b / a - 1
        weight[sid] = rec['issuer_weight'][sid]
    return ret, weight, n_entry, n_exit


def reference_weights(ret, weight):
    """CW (issuer-aggregated snapshot weight, normalised), EW, and the TOP-8 / TOP-10 equal-weight books."""
    cw_names = [s for s in ret if weight.get(s, 0) > 0]
    total = sum(weight[s] for s in cw_names)
    cw = {s: weight[s] / total for s in cw_names} if total > 0 else {}
    ew = {s: 1 / len(ret) for s in ret} if ret else {}
    ranked = sorted(cw, key=lambda s: (-cw[s], s))
    books = {'CW': cw, 'EW': ew}
    for n in (8, 10):
        top = ranked[:n]
        books[f'TOPW{n}'] = {s: 1 / len(top) for s in top} if top else {}
    return books


def d1_portfolios(records, tri_fn, voo_tri):
    weekly, counts = [], []
    for i in range(len(records) - 1):
        rec, nxt = records[i], records[i + 1]['t']
        if nxt > END_DATE:
            continue
        ret, weight, n_entry, n_exit = week_returns(rec, nxt, tri_fn)
        v_a, v_b = voo_tri.get(rec['t']), voo_tri.get(nxt)
        if v_a is None or v_b is None:
            raise ValueError('VOO TRI missing on a signal date')
        books = reference_weights(ret, weight)
        row = dict(t=rec['t'].isoformat(), t_next=nxt.isoformat(), voo=v_b / v_a - 1)
        for name, book in books.items():
            row[name] = sum(w * ret[s] for s, w in book.items()) if book else None
        weekly.append(row)
        counts.append(dict(t=rec['t'].isoformat(), n_pool=len(rec['pool']), no_tri_entry=n_entry,
                           no_tri_exit=n_exit, names_cw=len(books['CW'])))
    return weekly, counts


def period_summary(rows, key):
    """Annualised return and excess over VOO for the given weekly rows; yearly and two-segment excess."""
    rows = [r for r in rows if r.get(key) is not None]
    if not rows:
        return None

    def compound(xs):
        out = 1.0
        for x in xs:
            out *= 1 + x
        return out - 1
    days = (date.fromisoformat(rows[-1]['t_next']) - date.fromisoformat(rows[0]['t'])).days
    port = compound([r[key] for r in rows])
    voo = compound([r['voo'] for r in rows])
    out = dict(annualized=annualize(port, days), voo_annualized=annualize(voo, days))
    out['excess'] = out['annualized'] - out['voo_annualized']
    for y in range(2020, 2024):
        yr = [r for r in rows if r['t'].startswith(str(y))]
        out[f'excess_{y}'] = (compound([r[key] for r in yr]) - compound([r['voo'] for r in yr])) if yr else None
    for name, lo, hi in (('seg1', '0000', '2021-12-31'), ('seg2', '2022-01-01', '9999')):
        seg = [r for r in rows if lo <= r['t'] <= hi]
        if seg:
            sd = (date.fromisoformat(seg[-1]['t_next']) - date.fromisoformat(seg[0]['t'])).days
            sp = annualize(compound([r[key] for r in seg]), sd)
            sv = annualize(compound([r['voo'] for r in seg]), sd)
            out[f'excess_{name}'] = sp - sv
        else:
            out[f'excess_{name}'] = None
    return out


# ---------------------------------------------------------------- D2 signal layer

def d2_forward(records, ctx, tri_fn):
    """Per week and security: score by window, execution gap, and forward open-based returns (no later than end)."""
    px = ctx.prepared[1]
    corp_on = defaultdict(set)
    for e in ctx.full.events:
        if e.event_type in CORPORATE:
            corp_on[e.effective_session].add(e.security_id)
    stats = dict(excluded_corporate=0, excluded_price=0)
    weeks = []
    for i, rec in enumerate(records):
        e = rec['exec']
        if e is None:
            continue
        base = {}
        for sid in rec['pool']:
            if sid in corp_on[e]:
                stats['excluded_corporate'] += 1
                continue
            a, b = px.get((sid, rec['t'])), px.get((sid, e))
            if a is None or b is None:
                stats['excluded_price'] += 1
                continue
            base[sid] = dict(gap=b.open / a.close - 1)
        fwd = {}
        for h in HORIZONS:
            if i + h >= len(records):
                continue
            t_h = records[i + h]['t']
            if t_h > END_DATE:
                continue
            fwd[h] = {}
            for sid, item in base.items():
                c0, c1 = tri_fn(sid, rec['t']), tri_fn(sid, t_h)
                if c0 is None or c1 is None:
                    continue
                close_ret = c1 / c0 - 1
                fwd[h][sid] = (1 + close_ret) / (1 + item['gap']) - 1
        weeks.append(dict(t=rec['t'], base=base, fwd=fwd, pool=rec['pool'],
                          scores={L: rec['scores'][L] for L in WINDOWS}))
    return weeks, stats


def _sample(week, L, h):
    rows = []
    for sid, fo in week['fwd'].get(h, {}).items():
        score = week['scores'][L].get(sid)
        if score is None:
            continue
        rows.append((sid, score, fo, week['base'][sid]['gap']))
    return rows


def _ic_series(weeks, L, h, keep=None):
    series = []
    for w in weeks:
        rows = [r for r in _sample(w, L, h) if keep is None or r[0] in keep(w)]
        if len(rows) < MIN_POOL:
            continue
        ic = spearman([r[1] for r in rows], [r[2] for r in rows])
        if ic is not None:
            series.append((w['t'], ic))
    return series


def d2_tables(weeks):
    ic_rows, quint_rows, top_rows = [], [], []
    common_by_week = {}
    for w in weeks:
        common_by_week[w['t']] = {sid for sid in w['pool'] if all(w['scores'][L].get(sid) is not None for L in WINDOWS)}
    for L in WINDOWS:
        for h in HORIZONS:
            series = _ic_series(weeks, L, h)
            vals = [v for _, v in series]
            common = _ic_series(weeks, L, h, keep=lambda w: common_by_week[w['t']])
            cvals = [v for _, v in common]
            row = dict(L=L, h=h, n_weeks=len(vals), ic_mean=mean(vals) if vals else None,
                       nw_t=newey_west_t(vals, h), ic_positive_share=(sum(1 for v in vals if v > 0) / len(vals)) if vals else None,
                       common_n_weeks=len(cvals), common_ic_mean=mean(cvals) if cvals else None,
                       common_nw_t=newey_west_t(cvals, h))
            for y in range(2020, 2024):
                yv = [v for t, v in series if t.year == y]
                row[f'ic_{y}'] = mean(yv) if yv else None
            ic_rows.append(row)
            quint = [[] for _ in range(5)]
            top_excess, top_gap = [], []
            for w in weeks:
                rows = sorted(_sample(w, L, h), key=lambda r: (r[1], r[0]))
                n = len(rows)
                if n < MIN_POOL:
                    continue
                for k in range(5):
                    grp = rows[k * n // 5:(k + 1) * n // 5]
                    quint[k].append(mean(r[2] for r in grp))
                top = sorted(rows, key=lambda r: (-r[1], r[0]))[:TOP_N]
                top_excess.append(mean(r[2] for r in top) - mean(r[2] for r in rows))
                top_gap.append(mean(r[3] for r in top) - mean(r[3] for r in rows))
            qm = [mean(q) if q else None for q in quint]
            quint_rows.append(dict(L=L, h=h, **{f'Q{k + 1}_fwd_open': qm[k] for k in range(5)},
                                   spread_Q5_minus_Q1=(qm[4] - qm[0]) if qm[0] is not None else None))
            top_rows.append(dict(L=L, h=h, top8_excess_fwd_open=mean(top_excess) if top_excess else None,
                                 top8_gap_minus_pool=mean(top_gap) if top_gap else None, n_weeks=len(top_excess)))
    return ic_rows, quint_rows, top_rows


# ---------------------------------------------------------------- D3 concentration and active contribution

def run_positions(run):
    """date -> {security_id: weight of equity}; receivable rows are excluded; also daily total equity."""
    equity = {r['session_date']: float(r['total_equity']) for r in read_table(run / 'daily_equity.csv')}
    weights = defaultdict(dict)
    industry = {}
    for r in read_table(run / 'positions.csv'):
        if r.get('asset_type') != 'actual_shares':
            continue
        d = r['session_date']
        if d not in equity or equity[d] <= 0:
            continue
        weights[d][r['security_id']] = float(r['value']) / equity[d]
        industry[r['security_id']] = r['industry']
    return weights, industry


def concentration_rows(config, weights, industry, mapping):
    per_day, sector_day = [], defaultdict(list)
    sector_max = defaultdict(float)
    days_over_half = 0
    for d in sorted(weights):
        w = weights[d]
        if not w:
            continue
        s = sum(w.values())
        vals = sorted(w.values(), reverse=True)
        hhi = sum((v / s) ** 2 for v in vals)
        per_day.append(dict(max_w=vals[0], top3=sum(vals[:3]), eff_n=1 / hhi, n=len(vals)))
        by_sector = defaultdict(float)
        for sid, v in w.items():
            by_sector[sector_of(industry.get(sid, ''), mapping)] += v
        for sec, v in by_sector.items():
            sector_day[sec].append(v)
            sector_max[sec] = max(sector_max[sec], v)
        if max(by_sector.values()) > 0.5:
            days_over_half += 1
    days = len(per_day)
    maxw = [x['max_w'] for x in per_day]
    top3 = [x['top3'] for x in per_day]
    effn = [x['eff_n'] for x in per_day]
    rows = [
        dict(config=config, metric='single_max_weight_median', value=median(maxw)),
        dict(config=config, metric='single_max_weight_p90', value=float(np.percentile(maxw, 90))),
        dict(config=config, metric='single_max_weight_max', value=max(maxw)),
        dict(config=config, metric='top3_share_median', value=median(top3)),
        dict(config=config, metric='top3_share_max', value=max(top3)),
        dict(config=config, metric='effective_n_median', value=median(effn)),
        dict(config=config, metric='days', value=days),
        dict(config=config, metric='share_days_any_sector_over_50pct', value=days_over_half / days if days else None),
    ]
    sector_rows = []
    for sec in SECTORS + ['UNKNOWN', 'UNMAPPED']:
        vals = sector_day.get(sec, [])
        sector_rows.append(dict(config=config, sector=sec,
                                mean_weight=(sum(vals) / days) if days and vals else 0.0,
                                max_daily_weight=sector_max.get(sec, 0.0)))
    return rows, sector_rows


def active_rows(config, records, weights, industry, tri_fn, mapping, tickers):
    """Sum over weeks of (strategy weight at execution close - CW weight at signal) x security return, in points."""
    contrib = defaultdict(float)
    missing_return = 0
    for i in range(len(records) - 1):
        rec, nxt = records[i], records[i + 1]['t']
        e = rec['exec']
        if e is None or nxt > END_DATE:
            continue
        ret, weight, _, _ = week_returns(rec, nxt, tri_fn)
        cw = reference_weights(ret, weight)['CW']
        held = weights.get(e.isoformat(), {})
        for sid in set(held) | set(cw):
            a, b = tri_fn(sid, rec['t']), tri_fn(sid, nxt)
            if a is None or b is None:
                if held.get(sid, 0) > 0:
                    missing_return += 1
                continue
            contrib[sid] += (held.get(sid, 0.0) - cw.get(sid, 0.0)) * (b / a - 1)
    total = sum(contrib.values()) * 100
    by_sector = defaultdict(float)
    for sid, v in contrib.items():
        by_sector[sector_of(industry.get(sid, ''), mapping)] += v * 100
    ranked = sorted(contrib.items(), key=lambda kv: (-kv[1], kv[0]))
    top = [dict(config=config, rank='top', security_id=s, ticker=tickers.get(s, ''), contribution_pp=v * 100) for s, v in ranked[:10]]
    bottom = [dict(config=config, rank='bottom', security_id=s, ticker=tickers.get(s, ''), contribution_pp=v * 100)
              for s, v in ranked[-5:]]
    sector_rows = [dict(config=config, sector=sec, contribution_pp=by_sector.get(sec, 0.0)) for sec in SECTORS + ['UNKNOWN', 'UNMAPPED']]
    summary = dict(config=config, total_active_pp=total, missing_return_position_weeks=missing_return)
    return top + bottom, sector_rows, summary


# ---------------------------------------------------------------- D4 turnover sources

def turnover_rows(config, run):
    decisions = _decisions(run)
    trades = read_table(run / 'trades.csv')
    exits = defaultdict(int)
    exit_reason = {}
    for r in decisions:
        if r['action'] == 'exit':
            exits[r['reason']] += 1
            exit_reason[(r['signal_session_date'], r['security_id'])] = r['reason']
    sold = defaultdict(float)
    total_sell = 0.0
    unmatched = 0.0
    buys = 0
    for t in trades:
        if t['side'] == 'buy':
            buys += 1
            continue
        if t['side'] != 'sell':
            continue
        amount = float(t['cash_delta'])
        total_sell += amount
        reason = exit_reason.get((t['signal_session_date'], t['security_id']))
        if reason is None:
            unmatched += amount
        else:
            sold[reason] += amount
    reasons = sorted(set(exits) | set(sold))
    rows = []
    for reason in reasons:
        rows.append(dict(config=config, reason=reason, exits=exits.get(reason, 0),
                         sell_dollars=sold.get(reason, 0.0),
                         sell_share=(sold.get(reason, 0.0) / total_sell) if total_sell else None))
    if unmatched:
        rows.append(dict(config=config, reason='unmatched_sell', exits=0, sell_dollars=unmatched,
                         sell_share=unmatched / total_sell if total_sell else None))
    summary = dict(config=config, buy_trades=buys, sell_trades=sum(1 for t in trades if t['side'] == 'sell'),
                   total_sell_dollars=total_sell)
    return rows, summary


def _decisions(run):
    """decisions.csv is a projection of the lean tables; expanded_bytes restores its exact CSV rows."""
    import io
    text = expanded_bytes(run / 'decisions.csv').decode('utf-8')
    return list(csv.DictReader(io.StringIO(text)))


# ---------------------------------------------------------------- D6 members without prices

def missing_price_rows(records, engine_tri, historical, mapping, tri_fn, coverage):
    """Pool members with no price series (the stage 2 coverage rule); historical = classified unpriced members."""
    by_year = defaultdict(lambda: dict(counts=[], shares=[], sids=set()))
    by_sector = defaultdict(lambda: dict(shares=[], sids=set()))
    mismatches = 0
    for rec in records:
        missing = [sid for sid in rec['pool'] if not engine_tri.get(sid) or not engine_tri[sid].tri]
        hist = [sid for sid in missing if sid in historical]
        cov = coverage.get(rec['t'].isoformat())
        if cov is not None and int(cov['n_historical_no_price']) != len(hist):
            mismatches += 1
        total = rec['total_weight'] or 1.0
        share = sum(rec['member_weight'].get(sid, 0.0) for sid in hist) / total
        year = rec['t'].year
        by_year[year]['counts'].append(len(hist))
        by_year[year]['shares'].append(share)
        by_year[year]['sids'].update(hist)
        for sid in hist:
            by_sector[sector_of(rec['industry'].get(sid, ''), mapping)]['sids'].add(sid)
            by_sector[sector_of(rec['industry'].get(sid, ''), mapping)]['shares'].append(
                rec['member_weight'].get(sid, 0.0) / total)
    year_rows = []
    for y in sorted(by_year):
        v = by_year[y]
        year_rows.append(dict(year=y, weeks=len(v['counts']), mean_missing_members=mean(v['counts']),
                              mean_weight_share_pct=100 * mean(v['shares']), distinct_securities=len(v['sids'])))
    sector_rows = []
    weeks = len(records)
    for sec in SECTORS + ['UNKNOWN', 'UNMAPPED']:
        v = by_sector.get(sec, {'shares': [], 'sids': set()})
        sector_rows.append(dict(sector=sec, distinct_securities=len(v['sids']),
                                mean_weight_share_pct=100 * sum(v['shares']) / weeks if weeks else 0.0))
    return year_rows, sector_rows, mismatches


# ---------------------------------------------------------------- D5 random_matched controls under diagnostic authorisation

class DiagnosticContext(ExperimentContext):
    """Admits only diagnostic random_matched controls of registered stage 4 configurations, 10 bp, development segment."""

    def authorize(self, raw, end_override):
        if end_override is not None:
            raise ValueError('diagnostics use the development segment only')
        p = self.points.get(raw.get('configuration_id'))
        if p is None:
            raise ValueError('configuration is not a registered diagnostic control')
        reg = STAGE4_BY_ID.get(p.get('registry_id'))
        if reg is None:
            raise ValueError('diagnostic controls are stage 4 registered configurations only')
        for key in ('L', 'N', 'k', 'b', 'rebalance'):
            if raw.get(key) != reg[key]:
                raise ValueError('configuration does not match the stage 4 registration: ' + key)
        if raw.get('selection') != 'random_matched' or p.get('selection') != 'random_matched':
            raise ValueError('diagnostic controls use random_matched selection only')
        if raw.get('slippage_bps') != 10 or self.costs != [10]:
            raise ValueError('diagnostic controls are 10 bp only')
        if raw.get('start_date') != self.segment['start'] or raw.get('end_date') != self.segment['end']:
            raise ValueError('diagnostic controls use the development segment only')
        return super().authorize(raw, end_override)


STAGE4_BY_ID = {p['id']: p for p in stage4_points()}


def _init_diag_worker(cache_path, cache_key, payload):
    from . import perf_runner
    from .perf_cache import worker_cache
    ctx = worker_cache(cache_path, cache_key)
    ctx.__class__ = DiagnosticContext
    ctx.configure(**payload)
    perf_runner._TIMER = Timer()
    perf_runner._TIMER.instrument()
    perf_runner._CTX = ctx


def _run_diag_jobs(jobs, ctx, workers, payload):
    """Run the jobs not already accepted under the same key; in workers the context is a DiagnosticContext."""
    from . import perf_runner
    from .perf_runner import accepted, run_key
    todo = [j for j in jobs if not accepted(j[2], run_key(ctx.cache_key, j[0], j[1], j[3], j[4], j[5]))]
    if not todo:
        return 0
    if workers == 1:
        ctx.__class__ = DiagnosticContext
        perf_runner._CTX = ctx
        perf_runner._TIMER = Timer()
        perf_runner._TIMER.instrument()
        ctx.configure(**payload)
        for job in todo:
            perf_runner.one_job(job)
        return len(todo)
    spawn = multiprocessing.get_context('spawn')
    cache_path = str(ctx.cache_folder / 'prepared.pkl')
    with spawn.Pool(workers, initializer=_init_diag_worker, initargs=(cache_path, ctx.cache_key, payload)) as pool:
        list(pool.imap_unordered(perf_runner.one_job, todo))
    return len(todo)


def d5_controls(root, base, workers, ctx_loader):
    """100 random_matched controls per registered configuration at L in WINDOWS; reference exits from its own run."""
    from .perf_cache import identity
    doc = identity(root, 'uniform')
    ctx = ctx_loader(root, doc)
    points = []
    for rid in D5_CONFIGS:
        reg = STAGE4_BY_ID[rid]
        signal = normalize({'kind': 'momentum', 'L': reg['L'], 'skip': 0})
        exits = reference_exit_counts(root / paths.STAGE4_RUNS / rid / 'cost_10bps')
        for seed in range(D5_RUNS):
            points.append(dict(id=f'DIAG_{rid}_s{seed:03d}', registry_id=rid, signal=signal, L=reg['L'], N=reg['N'],
                               k=reg['k'], b=reg['b'], rebalance=reg['rebalance'], selection='random_matched',
                               selection_params=dict(seed=seed, exit_counts=exits)))
    payload = dict(points=points, costs=[10], experiment=DIAG_EXPERIMENT)
    ctx.configure(**payload)
    jobs = [(dict(p, experiment=DIAG_EXPERIMENT), 10, str(base / 'null' / p['registry_id'] / p['id'] / 'cost_10bps'),
             'explore', False, 'explore') for p in points]
    executed = _run_diag_jobs(jobs, ctx, workers, payload)
    rows = []
    for rid in D5_CONFIGS:
        orig = run_metrics(root / paths.STAGE4_RUNS / rid / 'cost_10bps')
        values, turns = [], []
        for p in points:
            if p['registry_id'] != rid:
                continue
            m = _metrics_or_none(base / 'null' / rid / p['id'] / 'cost_10bps')
            if m is not None:
                values.append(m['annualized_excess'])
                turns.append(m['turnover'])
        point, low, high = null_band(orig['annualized_excess'], values, D5_RUNS)
        arr = np.array(values) if values else None
        rows.append(dict(
            config=rid, L=STAGE4_BY_ID[rid]['L'], original_excess_10bp=orig['annualized_excess'],
            original_turnover=orig['turnover'], null_mean=float(arr.mean()) if values else None,
            null_median=float(np.median(arr)) if values else None,
            null_p10=float(np.percentile(arr, 10)) if values else None,
            null_p90=float(np.percentile(arr, 90)) if values else None,
            percentile_point=point, percentile_low=low, percentile_high=high,
            completed=len(values), registered=D5_RUNS, null_turnover_median=median(turns) if turns else None))
    return rows, executed


# ---------------------------------------------------------------- orchestration and outputs

def write_csv(path, rows):
    rows = list(rows)
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with Path(path).open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _fmt(x, pct=False, nd=2):
    if x is None or x == '':
        return '—'
    if isinstance(x, str):
        return x
    return f'{100 * x:.{nd}f}%' if pct else f'{x:.{nd}f}'


def _table(headers, rows):
    lines = ['| ' + ' | '.join(headers) + ' |', '|' + '---|' * len(headers)]
    lines += ['| ' + ' | '.join(str(c) for c in r) + ' |' for r in rows]
    return lines


def _tickers(root):
    rows = csv.DictReader(open(root / 'data/v05/normalized/securities.csv', encoding='utf-8', newline=''))
    return {r['security_id']: r['ticker'] for r in rows}


def _historical_set(root):
    path = root / paths.PREP / 'stage2_unpriced_members.csv'
    if not path.is_file():
        return set()
    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        return {r['security_id'] for r in csv.DictReader(handle) if r.get('classification') == 'historical_member_no_price'}


def run_diagnostics(root, output, workers=1):
    import time
    from .perf_cache import build_context, load_cache
    from .perf_bench import sha
    root = Path(root)
    out = Path(output) if Path(output).is_absolute() else root / output
    if (root / paths.RUNS).resolve() not in out.resolve().parents:
        raise ValueError('diagnostic output must be inside work/runs')
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    mapping = load_sector_map(root / 'config/sector_map_v1.csv')
    tickers = _tickers(root)
    historical = _historical_set(root)
    ctx = build_context(root, 'uniform')
    records = weekly_records(ctx)
    voo = benchmark_tri(ctx)
    tri_fn = make_tri_fn(ctx, voo)
    engine_tri = ctx.prepared[-1].tri
    # D1
    weekly, counts = d1_portfolios(records, tri_fn, voo)
    names = ['CW', 'EW', 'TOPW8', 'TOPW10', 'voo']
    d1 = [dict(portfolio=n, **period_summary(weekly, n)) for n in names]
    write_csv(out / 'D1_portfolio_summary.csv', d1)
    write_csv(out / 'D1_weekly_returns.csv', weekly)
    write_csv(out / 'D1_weekly_missing_tri.csv', counts)
    # D2
    weeks, d2stats = d2_forward(records, ctx, tri_fn)
    ic_rows, quint_rows, top_rows = d2_tables(weeks)
    write_csv(out / 'D2_ic.csv', ic_rows)
    write_csv(out / 'D2_quintiles.csv', quint_rows)
    write_csv(out / 'D2_top8.csv', top_rows)
    # D6 (before the context is released)
    coverage = {r['signal_session_date']: r for r in read_table(root / paths.STAGE4_RUNS / 'L090_N08_b2_R2/cost_10bps/weekly_coverage.csv')}
    d6_year, d6_sector, d6_mismatch = missing_price_rows(records, engine_tri, historical, mapping, tri_fn, coverage)
    write_csv(out / 'D6_missing_by_year.csv', d6_year)
    write_csv(out / 'D6_missing_by_sector.csv', d6_sector)
    # D3 and D4 from the finished stage 4 runs
    conc_rows, sector_rows, active_all, active_sector, active_sum, d4_rows, d4_sum = [], [], [], [], [], [], []
    for config in D3_CONFIGS:
        run = root / paths.STAGE4_RUNS / config / 'cost_10bps'
        weights, industry = run_positions(run)
        c, s = concentration_rows(config, weights, industry, mapping)
        conc_rows += c
        sector_rows += s
        a, asec, asum = active_rows(config, records, weights, industry, tri_fn, mapping, tickers)
        active_all += a
        active_sector += asec
        active_sum.append(asum)
        t4, t4sum = turnover_rows(config, run)
        d4_rows += t4
        d4_sum.append(t4sum)
    write_csv(out / 'D3_concentration.csv', conc_rows)
    write_csv(out / 'D3_sector_weights.csv', sector_rows)
    write_csv(out / 'D3_active_contribution_securities.csv', active_all)
    write_csv(out / 'D3_active_contribution_sectors.csv', active_sector)
    write_csv(out / 'D3_active_totals.csv', active_sum)
    write_csv(out / 'D4_exit_reasons.csv', d4_rows)
    write_csv(out / 'D4_summary.csv', d4_sum)
    del ctx
    # D5 (heaviest; runs last)
    d5_rows, executed = d5_controls(root, out, workers, lambda r, doc: _diag_context(r, doc, load_cache))
    write_csv(out / 'D5_random_null.csv', d5_rows)
    manifest = dict(
        end=DEV_END, windows=WINDOWS, horizons=HORIZONS, min_pool=MIN_POOL, top_n=TOP_N,
        signal_weeks=len(records), ic_excluded_corporate=d2stats['excluded_corporate'],
        ic_excluded_price=d2stats['excluded_price'], d6_coverage_mismatch_weeks=d6_mismatch,
        d5_jobs_executed=executed, sector_map_sha256=sha(root / 'config/sector_map_v1.csv'),
        seconds=time.perf_counter() - started)
    dump(out / 'diagnostics_manifest.json', manifest)
    write_summary(out, root, dict(d1=d1, weekly=weekly, counts=counts, ic=ic_rows, quint=quint_rows, top=top_rows,
                                  conc=conc_rows, sector=sector_rows, active_sum=active_sum, active=active_all,
                                  active_sector=active_sector, d4=d4_rows, d4_sum=d4_sum, d5=d5_rows,
                                  d6_year=d6_year, d6_sector=d6_sector, manifest=manifest, tickers=tickers))
    return out


def _diag_context(root, doc, load_cache):
    ctx, _ = load_cache(root, doc)
    ctx.__class__ = DiagnosticContext
    return ctx


def write_summary(out, root, t):
    lines = ['# D0 诊断摘要（开发段 2019-11-29 至 2024-06-28）', '',
             '只报告数字，不下策略结论。成本、零碎股、时间范围与剔除规则见文末“口径说明”。', '',
             '## D1 参照组合（不计成本，周收益为收盘到收盘，持有到下一个信号周）', '']
    rows = []
    for r in t['d1']:
        rows.append([r['portfolio'], _fmt(r.get('annualized'), True), _fmt(r.get('excess'), True),
                     _fmt(r.get('excess_2020'), True), _fmt(r.get('excess_2021'), True), _fmt(r.get('excess_2022'), True),
                     _fmt(r.get('excess_2023'), True), _fmt(r.get('excess_seg1'), True), _fmt(r.get('excess_seg2'), True)])
    lines += _table(['组合', '年化收益', '相对 VOO 年化超额', '2020 超额', '2021 超额', '2022 超额', '2023 超额',
                     '段1超额(至2021-12-31)', '段2超额(2022-01-01起)'], rows)
    miss = [c['no_tri_entry'] + c['no_tri_exit'] for c in t['counts']]
    names = [c['names_cw'] for c in t['counts']]
    lines += ['', f'缺 TRI 剔除（每周证券数）：均值 {mean(miss):.2f}，最大 {max(miss)}；'
              f'CW 持有名数：均值 {mean(names):.1f}，最少 {min(names)}。', '']
    lines += ['## D2 信号层（执行日 e 为 t 之后第一个交易日；前向收益按执行日开盘计）', '']
    ic_rows = [[x['L'], x['h'], x['n_weeks'], _fmt(x['ic_mean'], nd=4), _fmt(x['nw_t']),
                _fmt(x['ic_positive_share'], True), _fmt(x['common_ic_mean'], nd=4), _fmt(x['common_nw_t'])]
               for x in t['ic']]
    lines += _table(['窗口 L', 'h', '周数', 'IC 均值', 'NW t', 'IC>0 周占比', '共同池 IC 均值', '共同池 NW t'], ic_rows)
    qrows = []
    for q, tp in zip(t['quint'], t['top']):
        qrows.append([q['L'], q['h'], _fmt(q['Q1_fwd_open'], True), _fmt(q['Q5_fwd_open'], True),
                      _fmt(q['spread_Q5_minus_Q1'], True), _fmt(tp['top8_excess_fwd_open'], True),
                      _fmt(tp['top8_gap_minus_pool'], True)])
    lines += ['', *_table(['L', 'h', 'Q1 平均前向开盘收益', 'Q5 平均', 'Q5−Q1', '前8名超额', '前8名跳空差'], qrows), '']
    lines += ['## D3 集中度与主动贡献（第四阶段 10 bp 运行）', '']
    by = defaultdict(dict)
    for r in t['conc']:
        by[r['config']][r['metric']] = r['value']
    rows = [[c, _fmt(m['single_max_weight_median'], True), _fmt(m['single_max_weight_p90'], True),
             _fmt(m['single_max_weight_max'], True), _fmt(m['top3_share_median'], True), _fmt(m['top3_share_max'], True),
             _fmt(m['effective_n_median'], nd=1), _fmt(m['share_days_any_sector_over_50pct'], True)]
            for c, m in by.items()]
    lines += _table(['配置', '单股最大权重 中位', 'p90', '最大', '前三大 中位', '前三大 最大', '有效持股数 中位', '任一板块>50%天数'], rows)
    lines += ['', '主动贡献（百分点，累计）与板块：']
    for r in t['active_sum']:
        lines.append(f"- {r['config']}：总计 {r['total_active_pp']:.2f} pp；未计入的持仓周（无收益）{r['missing_return_position_weeks']}。")
    lines += ['', *_table(['配置', '板块', '主动贡献(pp)'],
                          [[r['config'], r['sector'], f"{r['contribution_pp']:.2f}"] for r in t['active_sector']
                           if abs(r['contribution_pp']) > 0]), '']
    lines += ['## D4 退出原因与卖出金额占比', '']
    reasons = sorted({r['reason'] for r in t['d4']})
    cfgs = list(dict.fromkeys(r['config'] for r in t['d4']))
    rows = []
    for rs in reasons:
        row = [rs]
        for c in cfgs:
            m = next((r for r in t['d4'] if r['config'] == c and r['reason'] == rs), None)
            row.append(f"{m['exits']} / {_fmt(m['sell_share'], True, 1)}" if m else '0 / —')
        rows.append(row)
    lines += _table(['退出原因'] + [f'{c}（次数 / 卖出金额占比）' for c in cfgs], rows)
    lines += ['', '; '.join(f"{s['config']}：买入 {s['buy_trades']} 次，卖出 {s['sell_trades']} 次" for s in t['d4_sum']), '']
    lines += ['## D5 随机对照（random_matched，N=8、b=2、R2、10 bp，seed 0–99，探索档）', '']
    rows = [[r['config'], _fmt(r['original_excess_10bp'], True), _fmt(r['null_mean'], True), _fmt(r['null_median'], True),
             _fmt(r['null_p10'], True), _fmt(r['null_p90'], True),
             f"{_fmt(r['percentile_point'])}%（{_fmt(r['percentile_low'])}%–{_fmt(r['percentile_high'])}%）",
             f"{r['completed']}/{r['registered']}", _fmt(r['original_turnover']), _fmt(r['null_turnover_median'])]
            for r in t['d5']]
    lines += _table(['配置', '原策略超额', '随机均值', '随机中位', 'p10', 'p90', '原策略百分位（点估计及界）',
                     '完成数', '原策略换手', '随机换手中位'], rows)
    lines += ['', '## D6 缺价成员（weekly_coverage 的 n_historical_no_price 口径）', '']
    lines += _table(['年份', '周数', '平均每周缺价成员数', '其快照权重平均占比', '涉及证券数'],
                    [[r['year'], r['weeks'], _fmt(r['mean_missing_members'], nd=2), _fmt(r['mean_weight_share_pct'] / 100, True),
                      r['distinct_securities']] for r in t['d6_year']])
    lines += ['', '按板块（涉及证券数；权重平均占比）：' + '；'.join(
        f"{r['sector']} {r['distinct_securities']} / {_fmt(r['mean_weight_share_pct'] / 100, True)}" for r in t['d6_sector']
        if r['distinct_securities'] or r['mean_weight_share_pct']), '']
    lines += ['## 口径说明', '',
              '- 数据：perf_cache.build_context(root, "uniform")，全部日期不晚于 2024-06-28；向后收益所需日期晚于此的样本剔除。',
              '- 成本：D1–D3 为理论计算，不计成本，可买零碎股，与账本无关；D5 为 10 bp 探索档运行。',
              '- 股票池：每周 eligible=1 且 class_selected=1 的证券。CW 权重为快照中同一发行人各类别权重之和，记在被选中的类别上，再在有 TRI 的池内归一化。',
              '- 缺价剔除：入组时无 TRI 或持有到下一周时无 TRI 的证券不计入该周组合（分别计数）；这会使组合收益偏乐观，是已知偏差。',
              '- D2：执行日 e 有拆股、分红或分拆的证券不计入；gap = 开盘(e)/收盘(t)−1；fwd_open = (1+fwd_close)/(1+gap)−1；IC 需至少 10 只；NW 滞后取 h。',
              '- D3：权重为持仓市值/当日总权益（不含现金与应收）；有效持股数 = 1/Σ(w/Σw)²；主动贡献按执行日收盘权重减周初 CW 权重，乘以该周收益。',
              '- D4：decisions.csv 的退出行与卖出按（信号日，证券）对应；卖出金额取现金变动。',
              '- D6：缺价口径与 weekly_coverage 一致（无价格序列，且在 stage2_unpriced_members.csv 中分类为 historical_member_no_price）。']
    (out / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
