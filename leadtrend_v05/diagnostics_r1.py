"""Round-1 minimum diagnostics (plan 3.4): aligned D3, D7 rank segments, D8 weight exposure.

Development segment only: inputs are the reviewed stage 4 runs, the cached signal weeks and the price series up to
END_DATE. Nothing after END_DATE is read or written.
"""
import math
from datetime import date
from pathlib import Path
from collections import Counter, defaultdict

import numpy as np

from . import paths
from .diagnostics import END_DATE, SECTORS, reference_weights, run_positions
from .perf_artifacts import read_table
from .sectors import sector_key

D3_CONFIGS6 = ['L090_N08_b2_R2', 'L090_N08_b2_R1', 'L010_N08_b2_R2', 'L040_N08_b2_R2', 'L090_N10_b2_R2', 'L090_N10_b2_R1']
D5R_CONFIGS = ['L010_N08_b2_R2', 'L020_N08_b2_R2', 'L040_N08_b2_R2', 'L060_N08_b2_R2', 'L090_N08_b2_R2', 'L126_N08_b2_R2',
               'L090_N10_b2_R2', 'L126_N10_b2_R2', 'L060_N10_b2_R2']
A90 = 'L090_N10_b2_R2'
D7_LENGTHS = (90, 60, 126)


def sector_label(industry, sid, mapping):
    """Large sector of a security; an unmapped or UNKNOWN industry is reported as UNKNOWN (never merged)."""
    sector, known = sector_key(industry.get(sid, ''), mapping)
    return sector if known else 'UNKNOWN'


def d3r_active(config, records, weights, industry, tri_fn, mapping):
    """Aligned active contribution in points.

    Strategy and CW weights are both taken at the execution close e_i, and the return runs from e_i to e_{i+1}.
    CW is the normalised snapshot issuer weight of the names that have a price at both closes (reference_weights).
    """
    contrib = defaultdict(float)
    strategy_sector = defaultdict(float)
    cw_sector = defaultdict(float)
    weeks = 0
    missing = 0
    for i in range(len(records) - 1):
        rec = records[i]
        e, e_next = rec['exec'], records[i + 1]['exec']
        if e is None or e_next is None or e_next > END_DATE:
            continue
        weeks += 1
        ret, weight = {}, {}
        for sid in rec['pool']:
            a, b = tri_fn(sid, e), tri_fn(sid, e_next)
            if a is None or b is None:
                continue
            ret[sid] = b / a - 1
            weight[sid] = rec['issuer_weight'][sid]
        cw = reference_weights(ret, weight)['CW']
        held = weights.get(e.isoformat(), {})
        for sid, w in held.items():
            strategy_sector[sector_label(industry, sid, mapping)] += w
        for sid, w in cw.items():
            cw_sector[sector_label(industry, sid, mapping)] += w
        for sid in sorted(set(held) | set(cw)):
            a, b = tri_fn(sid, e), tri_fn(sid, e_next)
            if a is None or b is None:
                if held.get(sid, 0) > 0:
                    missing += 1
                continue
            contrib[sid] += (held.get(sid, 0.0) - cw.get(sid, 0.0)) * (b / a - 1)
    by_sector = defaultdict(float)
    for sid, v in contrib.items():
        by_sector[sector_label(industry, sid, mapping)] += v * 100
    ranked = sorted(contrib, key=lambda sid: (-contrib[sid], sid))
    rows = [dict(config=config, rank='top', security_id=sid, contribution_pp=contrib[sid] * 100) for sid in ranked[:10]]
    rows += [dict(config=config, rank='bottom', security_id=sid, contribution_pp=contrib[sid] * 100) for sid in ranked[-5:]]
    sector_rows = [dict(config=config, sector=s, contribution_pp=by_sector.get(s, 0.0),
                        strategy_mean_weight=strategy_sector.get(s, 0.0) / weeks if weeks else 0.0,
                        cw_mean_weight=cw_sector.get(s, 0.0) / weeks if weeks else 0.0)
                   for s in SECTORS + ['UNKNOWN']]
    summary = dict(config=config, total_active_pp=sum(contrib.values()) * 100, weeks=weeks,
                   missing_return_position_weeks=missing)
    return rows, sector_rows, summary


def _segment_labels(n):
    """Segment name for each 1-based rank among n scored names; ranks outside the named segments are omitted."""
    top_quintile = math.ceil(n / 5)
    labels = {}
    for r in range(1, n + 1):
        if r <= 8:
            labels[r] = '1-8'
        elif r <= 16:
            labels[r] = '9-16'
        elif r <= 32:
            labels[r] = '17-32'
        elif r <= top_quintile:
            labels[r] = '33-top-quintile'
        elif math.floor((r - 1) * 5 / n) == 3:
            labels[r] = 'fourth-quintile'
        elif math.floor((r - 1) * 5 / n) == 4:
            labels[r] = 'bottom-quintile'
    return labels


def vol60(engine, sid, d):
    """Sample standard deviation of the last 60 daily log returns of TRI up to session d; None when unavailable."""
    tri = engine.tri.get(sid)
    index = engine.pos.get(d)
    if tri is None or index is None or index < 60:
        return None
    values = [tri.tri.get(engine.sessions[j]) for j in range(index - 60, index + 1)]
    if any(v is None or v <= 0 for v in values):
        return None
    sigma = float(np.std(np.diff(np.log(np.array(values, dtype=float))), ddof=1))
    return sigma if sigma > 1e-12 else None


def _summary(values):
    ex = [v[0] for v in values]
    sn = [v[1] for v in values]
    return dict(weeks=len(values), mean_excess=float(np.mean(ex)), median_excess=float(np.median(ex)),
                positive_share=float(np.mean([x > 0 for x in ex])), mean_sector_neutral=float(np.mean(sn)),
                median_sector_neutral=float(np.median(sn)))


def d7_segments(weeks, records, engine, mapping, L):
    """Forward excess (open-based, 4 and 8 weeks) by rank segment of the L-window score, plus sector-neutral views.

    weeks: output of diagnostics.d2_forward. Excess is against the pool mean; sector-neutral subtracts the mean of the
    same sector in the pool. Each week contributes one value per segment (the mean over its names), and the summary
    is taken over weeks. Inside ranks 1-32 the segment is also split by VOO-weight and 60-day-volatility tertiles.
    """
    issuer = {rec['t']: rec['issuer_weight'] for rec in records}
    industry = {rec['t']: rec['industry'] for rec in records}
    buckets = defaultdict(list)  # (horizon, segment) -> one (excess, sector-neutral) per week
    top32 = defaultdict(list)    # horizon -> per week, per name in ranks 1-32
    for w in weeks:
        d = w['t']
        scores = w['scores'][L]
        for h in (4, 8):
            fwd = w['fwd'].get(h)
            if not fwd:
                continue
            scored = sorted(((s, scores[s]) for s in fwd if scores.get(s) is not None), key=lambda x: (-x[1], x[0]))
            n = len(scored)
            if n < 10:
                continue
            pool_mean = float(np.mean([fwd[s] for s, _ in scored]))
            sector = {s: sector_label(industry[d], s, mapping) for s, _ in scored}
            by_sector = defaultdict(list)
            for s, _ in scored:
                by_sector[sector[s]].append(fwd[s])
            labels = _segment_labels(n)
            week_values = defaultdict(list)
            for r, (s, _) in enumerate(scored, 1):
                label = labels.get(r)
                if label is None:
                    continue
                excess = fwd[s] - pool_mean
                neutral = fwd[s] - float(np.mean(by_sector[sector[s]]))
                week_values[label].append((excess, neutral))
                if r <= 32:
                    top32[h].append((d, excess, neutral, issuer[d].get(s, 0.0), vol60(engine, s, d)))
            for label, values in week_values.items():
                buckets[(h, label)].append((float(np.mean([v[0] for v in values])), float(np.mean([v[1] for v in values]))))
    rows = [dict(L=L, horizon=h, segment=label, **_summary(values)) for (h, label), values in sorted(buckets.items())]
    for h, items in sorted(top32.items()):
        for name, index in (('voo_weight_tertile', 3), ('vol60_tertile', 4)):
            cut = [x for x in items if x[index] is not None]
            if len(cut) < 3:
                continue
            lo, hi = np.percentile([x[index] for x in cut], [100 / 3, 200 / 3])
            groups = {'low': [x for x in cut if x[index] <= lo],
                      'middle': [x for x in cut if lo < x[index] <= hi],
                      'high': [x for x in cut if x[index] > hi]}
            for bucket, members in groups.items():
                if not members:
                    continue
                per_week = defaultdict(list)
                for x in members:
                    per_week[x[0]].append((x[1], x[2]))
                values = [(float(np.mean([v[0] for v in vs])), float(np.mean([v[1] for v in vs])))
                          for _, vs in sorted(per_week.items())]
                rows.append(dict(L=L, horizon=h, segment=f'top32 {name} {bucket}', **_summary(values)))
    return rows


def _decile(weights, sid):
    ordered = sorted(weights, key=lambda s: (-weights[s], s))
    return min(10, 1 + ordered.index(sid) * 10 // len(ordered))


def d8_rows(label, pools, buys, holdings):
    """Counts and shares by VOO-weight decile of the signal week's pool (decile 1 = largest issuer weight)."""
    counts = Counter()
    totals = Counter()
    for kind, rows in (('new_buys', buys), ('holdings', holdings)):
        for week, sid in rows:
            weights = pools.get(week)
            if not weights or sid not in weights:
                continue
            counts[(kind, _decile(weights, sid))] += 1
            totals[kind] += 1
    out = []
    for kind in ('new_buys', 'holdings'):
        for dec in range(1, 11):
            c = counts.get((kind, dec), 0)
            out.append(dict(source=label, kind=kind, decile=dec, count=c, share=c / totals[kind] if totals[kind] else 0.0))
    return out


def a90_exposure(root, records, pools):
    """A90 actual buys and holdings, and the same for its random controls (pooled over the 100 runs)."""
    exec_week = {rec['exec'].isoformat(): rec['t'].isoformat() for rec in records if rec['exec'] is not None}
    run = root / paths.STAGE4_RUNS / A90 / 'cost_10bps'
    trades = read_table(run / 'trades.csv')
    actual_buys = [(t['signal_session_date'], t['security_id']) for t in trades
                   if t['side'] == 'buy' and float(t['shares_before']) == 0]
    actual_hold = [(exec_week[p['session_date']], p['security_id']) for p in read_table(run / 'positions.csv')
                   if p.get('asset_type') == 'actual_shares' and p['session_date'] in exec_week]
    return actual_buys, actual_hold


def random_exposure(folder_list, records):
    """Pooled random-control buys and holdings at each execution session, reconstructed from the trades."""
    exec_dates = sorted(rec['exec'].isoformat() for rec in records if rec['exec'] is not None)
    exec_week = {rec['exec'].isoformat(): rec['t'].isoformat() for rec in records if rec['exec'] is not None}
    buys, holds = [], []
    for folder in folder_list:
        trades = read_table(folder / 'trades.csv')
        by_day = defaultdict(list)
        for t in trades:
            by_day[t['session_date']].append(t)
            if t['side'] == 'buy' and float(t['shares_before']) == 0:
                buys.append((t['signal_session_date'], t['security_id']))
        shares = defaultdict(float)
        days = sorted(by_day)
        pointer = 0
        for day in exec_dates:
            while pointer < len(days) and days[pointer] <= day:
                for t in by_day[days[pointer]]:
                    shares[t['security_id']] += float(t['shares_after']) - float(t['shares_before'])
                pointer += 1
            for sid, q in shares.items():
                if q > 0:
                    holds.append((exec_week[day], sid))
    return buys, holds


D3_LEDGER_CHECK = 1e-6


def d3_ledger(config, run, records, tri_fn, industry, mapping, initial=100000.0):
    """Active contribution from the ledger (v1 of the D3 method, replacing the aligned weight method of D3R).

    Strategy side: daily_contributions, each security's dollar profit on each day.
    CW side: on each session, the CW weights set at the latest signal close before the previous session, times the
    previous session's total equity, times the session's TRI return (total return, dividends reinvested).
    Active = strategy minus CW, by security and by large sector. Reconciliation: the strategy side must sum to the
    final equity minus the initial capital, within D3_LEDGER_CHECK dollars.
    """
    equity = {r['session_date']: float(r['total_equity']) for r in read_table(Path(run) / 'daily_equity.csv')}
    dates = sorted(equity)
    strat = defaultdict(float)
    for r in read_table(Path(run) / 'daily_contributions.csv'):
        strat[r['security_id']] += float(r['profit_dollars'])
    strat_sum = sum(strat.values())
    recon = strat_sum - (equity[dates[-1]] - initial)
    cw_weeks = []
    for rec in sorted(records, key=lambda r: r['t']):
        names = {sid: 0.0 for sid in rec['pool'] if tri_fn(sid, rec['t']) is not None}
        if names:
            weight = {sid: rec['issuer_weight'][sid] for sid in names}
            cw_weeks.append((rec['t'].isoformat(), reference_weights(names, weight)['CW']))
    cw = defaultdict(float)
    cw_profit = 0.0
    missing = 0
    latest = -1
    for j in range(1, len(dates)):
        today, prev = dates[j], dates[j - 1]
        while latest + 1 < len(cw_weeks) and cw_weeks[latest + 1][0] <= prev:
            latest += 1
        if latest < 0:
            continue
        base = equity[prev]
        d0, d1 = date.fromisoformat(prev), date.fromisoformat(today)
        for sid, w in cw_weeks[latest][1].items():
            a, b = tri_fn(sid, d0), tri_fn(sid, d1)
            if a is None or b is None:
                missing += 1
                continue
            profit = w * base * (b / a - 1)
            cw[sid] += profit
            cw_profit += profit
    active = {sid: strat[sid] - cw[sid] for sid in set(strat) | set(cw)}
    total = strat_sum - cw_profit
    totals = dict(config=config, strategy_profit_dollars=strat_sum, equity_end_minus_initial=equity[dates[-1]] - initial,
                  reconciliation_error=recon, cw_profit_dollars=cw_profit, total_active_pp=total / initial * 100,
                  strategy_pp=strat_sum / initial * 100, cw_pp=cw_profit / initial * 100, missing_tri_name_days=missing,
                  status='PASS' if abs(recon) < D3_LEDGER_CHECK else 'FAILED_RECONCILIATION')
    by_sector = defaultdict(lambda: [0.0, 0.0])
    securities = []
    for sid in sorted(active, key=lambda s: (-active[s], s)):
        sector = sector_label(industry, sid, mapping)
        by_sector[sector][0] += strat[sid]
        by_sector[sector][1] += cw[sid]
        securities.append(dict(config=config, security_id=sid, sector=sector, strategy_pp=strat[sid] / initial * 100,
                               cw_pp=cw[sid] / initial * 100, active_pp=active[sid] / initial * 100))
    sectors = [dict(config=config, sector=s, strategy_pp=by_sector[s][0] / initial * 100, cw_pp=by_sector[s][1] / initial * 100,
                    active_pp=(by_sector[s][0] - by_sector[s][1]) / initial * 100) for s in SECTORS + ['UNKNOWN']]
    return totals, securities, sectors
