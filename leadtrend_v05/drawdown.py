"""Drawdown record, calendar-year and Sharpe columns for the scorecard (plan 9.2).

Every function takes ISO session dates and the values on those sessions, already cut to the development segment by
the caller. The strategy and VOO are measured the same way: the path starts on the first comparison session.

Conventions (written into the report):
- drawdown: value / running peak - 1, peak taken from the first session on; the record gives the largest drawdown's
  peak, trough and recovery dates (recovery = first later session at or above the peak) and the longest underwater
  spell (peak to recovery, or to the last session when unrecovered), in calendar days.
- 2020 and 2022 drawdowns: largest drawdown with the peak inside that calendar year only.
- worst calendar year: value at the last session of the year over the last session of the previous year (the first
  session for the first year); partial years are included and labelled.
- Sharpe net of DTB3: (CAGR - mean DTB3 over the window / 100) / annualised volatility of daily returns (252 sessions).
  DTB3 is the FRED 3-month Treasury bill rate in percent a year, as downloaded to data/v05/raw/external/DTB3.csv.
"""
import csv
import json
import math
from datetime import date
from pathlib import Path
from statistics import mean

from .perf_artifacts import read_table

DEV_END = '2024-06-28'
DTB3_FILE = 'data/v05/raw/external/DTB3.csv'
TRADING_DAYS = 252


def load_dtb3(root, end=DEV_END):
    """{ISO date: percent a year} for the DTB3 observations up to end; missing observations ('.') are skipped."""
    path = Path(root) / DTB3_FILE
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            d, v = r.get('observation_date') or r.get('DATE'), r.get('DTB3') or r.get('VALUE')
            if d and v not in (None, '', '.') and d <= end:
                out[d] = float(v)
    return out


def _worst_drawdown(values):
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst


def drawdown_record(days, values):
    """Largest drawdown with its dates, and the longest underwater spell (calendar days)."""
    peak_i, peak = 0, values[0]
    worst, worst_peak_i, worst_trough_i = 0.0, 0, 0
    spell_start, longest = None, 0
    for i, v in enumerate(values):
        if v >= peak:
            if spell_start is not None:
                longest = max(longest, (date.fromisoformat(days[i]) - date.fromisoformat(days[spell_start])).days)
                spell_start = None
            peak, peak_i = v, i
            continue
        if spell_start is None:
            spell_start = peak_i
        draw = v / peak - 1
        if draw < worst:
            worst, worst_peak_i, worst_trough_i = draw, peak_i, i
    if spell_start is not None:
        longest = max(longest, (date.fromisoformat(days[-1]) - date.fromisoformat(days[spell_start])).days)
    recovery = None
    if worst < 0:
        peak_value = values[worst_peak_i]
        for j in range(worst_trough_i + 1, len(values)):
            if values[j] >= peak_value:
                recovery = days[j]
                break
    return dict(max_drawdown=worst, dd_peak_date=days[worst_peak_i] if worst < 0 else '',
                dd_trough_date=days[worst_trough_i] if worst < 0 else '', dd_recovery_date=recovery or '',
                longest_underwater_days=longest, unrecovered_at_end=int(worst < 0 and recovery is None))


def calendar_year_drawdown(days, values, year):
    idx = [i for i, d in enumerate(days) if d.startswith(f'{year}-')]
    if not idx:
        return None
    return _worst_drawdown([values[i] for i in idx])


def worst_calendar_year(days, values):
    """(worst year return, year label) over every calendar year present; the last session of the prior year is the base."""
    years = sorted({d[:4] for d in days})
    best = None
    for y in years:
        idx = [i for i, d in enumerate(days) if d.startswith(f'{y}-')]
        last = values[idx[-1]]
        prior = [i for i, d in enumerate(days) if d < f'{y}-01-01']
        base = values[prior[-1]] if prior else values[idx[0]]
        ret = last / base - 1
        partial = y == years[-1] or not prior
        label = f'{y}（部分年）' if partial else y
        if best is None or ret < best[0]:
            best = (ret, label)
    return best


def sharpe_net_of_dtb3(days, values, rates):
    if len(days) < 3:
        return None
    years = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days / 365.25
    cagr = (values[-1] / values[0]) ** (1 / years) - 1 if years > 0 else None
    rs = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    m = sum(rs) / len(rs)
    vol = math.sqrt(sum((x - m) ** 2 for x in rs) / (len(rs) - 1)) * math.sqrt(TRADING_DAYS)
    window = [rates[d] for d in days if d in rates]
    if cagr is None or not vol or not window:
        return None
    return (cagr - mean(window) / 100) / vol


def path_profile(days, values, rates=None):
    """Drawdown record, calendar-year drawdowns, worst calendar year, CAGR, return over drawdown and Sharpe net of DTB3."""
    record = drawdown_record(days, values)
    years = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days / 365.25
    cagr = (values[-1] / values[0]) ** (1 / years) - 1 if years > 0 else None
    worst_ret, worst_label = worst_calendar_year(days, values)
    dd = record['max_drawdown']
    return dict(record, dd_2020=calendar_year_drawdown(days, values, 2020), dd_2022=calendar_year_drawdown(days, values, 2022),
                worst_year_return=worst_ret, worst_year=worst_label, cagr=cagr,
                ret_over_dd=(cagr / abs(dd)) if (cagr is not None and dd < 0) else None,
                sharpe_dtb3=sharpe_net_of_dtb3(days, values, rates or {}))


def top_issuer_share(folder, issuer_of):
    """Largest issuer's share of total profit (daily_contributions), and that issuer."""
    profit, total = {}, 0.0
    for r in read_table(Path(folder) / 'daily_contributions.csv'):
        v = float(r['profit_dollars'])
        total += v
        k = issuer_of.get(r['security_id'], r['security_id'])
        profit[k] = profit.get(k, 0.0) + v
    if not profit or total == 0:
        return None, None
    top = max(profit, key=lambda k: (profit[k], k))
    return profit[top] / total, top


def risk_profile(folder, root, rates=None, issuer_of=None):
    """Strategy and VOO profiles of one daily-verified run folder (the scorecard's new columns)."""
    folder = Path(folder)
    summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
    first = summary['comparison_start_session']
    eq = [(r['session_date'], float(r['total_equity'])) for r in read_table(folder / 'daily_equity.csv')
          if first <= r['session_date'] <= DEV_END]
    bm = [(r['session_date'], float(r['benchmark_value'])) for r in read_table(folder / 'benchmark.csv')
          if first <= r['session_date'] <= DEV_END]
    out = {}
    s = path_profile([d for d, _ in eq], [v for _, v in eq], rates)
    v = path_profile([d for d, _ in bm], [x for _, x in bm], rates)
    for k, val in s.items():
        out[k] = val
    for k, val in v.items():
        out[f'voo_{k}'] = val
    out['top_issuer_share'], out['top_issuer'] = top_issuer_share(folder, issuer_of or {})
    return out
