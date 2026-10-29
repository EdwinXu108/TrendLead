"""Weekly Bollinger "prediction points" and monthly/quarterly "zone" bands on one total-return series.

Prediction points. The mid line is a 20-week Hann-weighted mean of weekly total-return closes. Its first
derivative `s` says whether the line is still falling; the second derivative `a` says how fast the fall is
slowing. A week is a prediction point when the line still falls (`s < 0`) but one more week at the current
deceleration would flatten it (`s + a >= 0`). Every number at week `t` uses weeks up to `t` only.

Zones. Monthly (last session of each calendar month) and quarterly (last session of each calendar quarter)
closes get the same simple Bollinger treatment: `n`-period mean, population sigma, lower band at mean − 2σ.
Each week uses the bar dated on or before that week, so the lines are step functions and never look ahead.
A week is "in" a zone when its close is at or below that line plus `width` sigmas of the same period.

Registered in research/boll_v1/spec.md.

    python -X utf8 -m leadtrend_bt.boll_weekly --security META --quarter 8 --width 0.5
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import numpy as np

from leadtrend_bt import ma_lab, ma_walk
from leadtrend_db import api

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = str(ROOT / 'data' / 'db' / 'leadtrend_db_v2.sqlite')
SCOPE = 'top50_v1'
DEV_START, DEV_END = '2014-02-28', ma_lab.LAST_ALLOWED_END
WINDOW = 20
BAND_K = 2.0

# the zone lines: monthly is fixed at 20 bars, quarterly can be 20 (standard) or 8 (shorter history)
MONTHLY_N = 20
QUARTER_NS = (20, 8)
WIDTHS = (0.0, 0.5, 1.0)
DEFAULT_QUARTER_N = 20
DEFAULT_WIDTH = 0.5

# (key, Chinese name, strength, strength name); 3 最强, 2 次强, 1 较弱
ZONES = (('q_lower', '季线下轨', 3, '最强'),
         ('m_lower', '月线下轨', 2, '次强'),
         ('q_mid', '季线中轨', 2, '次强'),
         ('m_mid', '月线中轨', 1, '较弱'))


def weights(n: int = WINDOW) -> np.ndarray:
    """Hann weights, oldest week first: `sin²(π·(i+1)/(n+1))`, normalised to sum 1."""
    i = np.arange(n, dtype=float)
    w = np.sin(np.pi * (i + 1) / (n + 1)) ** 2
    return w / w.sum()


def weekly_closes(days: list[str], values) -> tuple[list[str], np.ndarray]:
    """The last session of each ISO week, labelled with that session's date."""
    out_days, out_values, last = [], [], None
    for day, value in zip(days, values):
        key = date.fromisoformat(day).isocalendar()[:2]
        if key != last:
            out_days.append(day)
            out_values.append(float(value))
            last = key
        else:
            out_days[-1] = day
            out_values[-1] = float(value)
    return out_days, np.asarray(out_values, dtype=float)


def period_closes(days: list[str], values, kind: str) -> tuple[list[str], np.ndarray]:
    """The last session of each calendar month ('month') or quarter ('quarter'), labelled with its date."""
    if kind not in ('month', 'quarter'):
        raise ValueError("kind must be 'month' or 'quarter'")
    out_days, out_values, last = [], [], None
    for day, value in zip(days, values):
        d = date.fromisoformat(day)
        key = (d.year, d.month) if kind == 'month' else (d.year, (d.month - 1) // 3)
        if key != last:
            out_days.append(day)
            out_values.append(float(value))
            last = key
        else:
            out_days[-1] = day
            out_values[-1] = float(value)
    return out_days, np.asarray(out_values, dtype=float)


def hann_mid(closes, n: int = WINDOW) -> np.ndarray:
    """`m_t = Σ w_i · P_{t−n+1+i}`; the first `n-1` weeks are NaN."""
    values = np.asarray(closes, dtype=float)
    out = np.full(values.size, np.nan)
    w = weights(n)
    for t in range(n - 1, values.size):
        out[t] = float(np.dot(w, values[t - n + 1:t + 1]))
    return out


def derivatives(mid) -> tuple[np.ndarray, np.ndarray]:
    """First difference `s_t = m_t − m_{t−1}` and its own difference `a_t = s_t − s_{t−1}`."""
    values = np.asarray(mid, dtype=float)
    s = np.full(values.size, np.nan)
    s[1:] = values[1:] - values[:-1]
    a = np.full(values.size, np.nan)
    a[1:] = s[1:] - s[:-1]
    return s, a


def is_prediction(s, a) -> np.ndarray:
    """Still falling, but flattening within one week at this week's deceleration."""
    s = np.asarray(s, dtype=float)
    a = np.asarray(a, dtype=float)
    return np.isfinite(s) & np.isfinite(a) & (s < 0) & (s + a >= 0)


def band_stats(closes, n: int, k: float = BAND_K) -> dict:
    """Simple mean, population sigma and the mean ± k sigma bands over the last `n` periods."""
    values = np.asarray(closes, dtype=float)
    mid = np.full(values.size, np.nan)
    sd = np.full(values.size, np.nan)
    for t in range(n - 1, values.size):
        window = values[t - n + 1:t + 1]
        mid[t] = float(window.mean())
        sd[t] = float(window.std(ddof=0))
    return dict(mid=mid, sd=sd, upper=mid + k * sd, lower=mid - k * sd)


def bollinger(closes, n: int = WINDOW, k: float = BAND_K) -> dict:
    """The same bands without the sigma, kept for the weekly reference lines."""
    bands = band_stats(closes, n, k)
    return {key: bands[key] for key in ('mid', 'upper', 'lower')}


def line_for(weeks, period_days, bands) -> dict:
    """The mid, sigma and lower band in force each week: the last bar dated on or before that week."""
    index = np.searchsorted(np.asarray(period_days), np.asarray(weeks), side='right') - 1
    out = {}
    for key in ('mid', 'sd', 'lower'):
        values = np.asarray(bands[key], dtype=float)
        out[key] = np.array([values[i] if i >= 0 else np.nan for i in index], dtype=float)
    return out


def in_zone(close, line, sd, width: float) -> np.ndarray:
    """Close at or below the line plus `width` sigmas of the same period; a missing line never counts."""
    close = np.asarray(close, dtype=float)
    line = np.asarray(line, dtype=float)
    sd = np.asarray(sd, dtype=float)
    return np.isfinite(line) & (close <= line + width * sd)


def zone_flags(weeks, close, month: dict, quarter: dict, width: float = DEFAULT_WIDTH) -> dict:
    """Which of the four zones each week is in; `month`/`quarter` hold days plus their bands."""
    monthly = line_for(weeks, month['days'], month)
    quarterly = line_for(weeks, quarter['days'], quarter)
    return {'q_lower': in_zone(close, quarterly['lower'], quarterly['sd'], width),
            'm_lower': in_zone(close, monthly['lower'], monthly['sd'], width),
            'q_mid': in_zone(close, quarterly['mid'], quarterly['sd'], width),
            'm_mid': in_zone(close, monthly['mid'], monthly['sd'], width)}


def zone_levels(flags: dict) -> np.ndarray:
    """The strongest zone of each week: 3 最强, 2 次强, 1 较弱, 0 none."""
    out = np.zeros(len(next(iter(flags.values()))), dtype=int)
    for key, _, strength, _ in ZONES:
        out = np.maximum(out, np.where(flags[key], strength, 0))
    return out


def zone_names(flags: dict, week: int) -> list[str]:
    """Every zone name that week is in, strongest first."""
    return [name for key, name, _, _ in ZONES if bool(flags[key][week])]


def zone_rows(weeks: list[str], close, flags: dict) -> list[dict]:
    """One row per uninterrupted run of weeks in each zone, zones in strength order."""
    close = np.asarray(close, dtype=float)
    rows = []
    for key, name, _, _ in ZONES:
        on = np.asarray(flags[key])
        start = None
        for i, flag in enumerate(on):
            if flag and start is None:
                start = i
            elif not flag and start is not None:
                rows.append(_zone_row(key, name, weeks, close, start, i - 1))
                start = None
        if start is not None:
            rows.append(_zone_row(key, name, weeks, close, start, len(on) - 1))
    return rows


def _zone_row(key: str, name: str, weeks: list[str], close, first: int, last: int) -> dict:
    window = close[first:last + 1]
    return dict(zone=key, name=name, start=weeks[first], end=weeks[last], weeks=int(last - first + 1),
                start_close=float(close[first]), min_close=float(window.min()))


def _next_flat(s, t: int) -> int | None:
    """First week after `t` whose first derivative is back at or above zero."""
    for e in range(t + 1, len(s)):
        if np.isfinite(s[e]) and s[e] >= 0:
            return e
    return None


def details(weeks: list[str], closes, s, a, start: str = DEV_START, end: str = DEV_END) -> list[dict]:
    """Every prediction point inside the window, with what happened until the line turned back up."""
    s = np.asarray(s, dtype=float)
    values = np.asarray(closes, dtype=float)
    rows = []
    for t in np.flatnonzero(is_prediction(s, a)):
        if not (start <= weeks[t] <= end):
            continue
        e = _next_flat(s, int(t))
        row = dict(date=weeks[t], close=float(values[t]), recover_date=None, gap_weeks=None,
                   max_drop=None, recover_gain=None)
        if e is not None:
            row.update(recover_date=weeks[e], gap_weeks=int(e - t),
                       max_drop=float(values[t:e + 1].min() / values[t] - 1),
                       recover_gain=float(values[e] / values[t] - 1))
        rows.append(row)
    return rows


def series(security: str, db_path: str | None = None, end: str = DEV_END, start: str = DEV_START,
           quarter_n: int = DEFAULT_QUARTER_N, width: float = DEFAULT_WIDTH) -> dict:
    """One security's weekly closes, mid line, prediction points and the four zone lines.

    Prices are the total-return index rescaled so the first week of the window shows that week's real close;
    `meta['price_scale']` is the factor. Dates, derivatives and ratios are unaffected. The zones are decided
    on the raw index, which gives exactly the same weeks because the factor is positive and shared.
    """
    if end > DEV_END:
        raise ValueError(f'end {end} is after {DEV_END}: the reserved segment must not be used')
    if int(quarter_n) not in QUARTER_NS:
        raise ValueError(f'quarter_n must be one of {QUARTER_NS}')
    if float(width) not in WIDTHS:
        raise ValueError(f'width must be one of {WIDTHS}')
    db = db_path or DEFAULT_DB
    sid = ma_lab.resolve_security(security, db)
    rows = api.tri_series([sid], '1900-01-01', end, db)[sid]
    if not rows:
        raise ValueError(f'{security} has no total-return series up to {end}')
    days_all = [r[0] for r in rows]
    values_all = [r[1] for r in rows]
    weeks_all, closes_all = weekly_closes(days_all, values_all)
    mid_all = hann_mid(closes_all)
    s_all, a_all = derivatives(mid_all)
    band_all = bollinger(closes_all)
    keep = [i for i, week in enumerate(weeks_all) if start <= week <= end]
    weeks = [weeks_all[i] for i in keep]

    def cut(values):
        return [float(values[i]) if np.isfinite(values[i]) else None for i in keep]

    close = cut(closes_all)
    # the index is an abstract level; rescale it so the first week shows that week's real close, the same
    # convention the moving-average dashboard uses. Ratios and dates do not change.
    closes = api.close_series([sid], start, end, db)[sid]
    anchor = next(((k, closes[week]) for k, week in enumerate(weeks) if closes.get(week) and close[k]), None)
    factor = anchor[1] / close[anchor[0]] if anchor else 1.0

    def scaled(values):
        return [None if v is None else v * factor for v in values]

    mid = scaled(cut(mid_all))
    close = scaled(close)
    s = cut(s_all)
    s_pct = [None if (m is None or s_ is None) else s_ / mid_all[i - 1] * 100
             for m, s_, i in zip(mid, s, keep)]
    points = details(weeks_all, closes_all, s_all, a_all, start, end)
    for point in points:
        point['close'] *= factor

    # monthly and quarterly bars, then the step lines in force on each of the kept weeks
    month = dict(zip(('days', 'values'), period_closes(days_all, values_all, 'month')))
    quarter = dict(zip(('days', 'values'), period_closes(days_all, values_all, 'quarter')))
    month.update(band_stats(month.pop('values'), MONTHLY_N))
    quarter.update(band_stats(quarter.pop('values'), int(quarter_n)))
    close_raw = np.asarray([closes_all[i] for i in keep], dtype=float)
    flags = zone_flags(weeks, close_raw, month, quarter, float(width))
    levels = zone_levels(flags)
    rows_zones = zone_rows(weeks, close, flags)
    monthly_line = line_for(weeks, month['days'], month)
    quarterly_line = line_for(weeks, quarter['days'], quarter)

    def line_out(values):
        return [None if not np.isfinite(v) else float(v) * factor for v in values]

    return dict(
        meta=dict(ticker=security.upper(), security_id=sid, start=start, end=end, weeks=len(weeks),
                  first_week=weeks[0] if weeks else None, last_week=weeks[-1] if weeks else None,
                  price_scale=factor),
        params=dict(quarter_n=int(quarter_n), width=float(width), monthly_n=MONTHLY_N,
                    quarter_first=quarter['days'][0] if quarter['days'] else None,
                    month_first=month['days'][0] if month['days'] else None),
        weeks=weeks, close=close, mid=mid, s=s, s_pct=s_pct,
        band=dict(upper=scaled(cut(band_all['upper'])), lower=scaled(cut(band_all['lower'])),
                  mid=scaled(cut(band_all['mid']))),
        marks=[dict(date=weeks[k], mid=mid[k], close=close[k])
               for k, i in enumerate(keep) if is_prediction(s_all, a_all)[i]],
        points=points,
        lines=dict(m_lower=line_out(monthly_line['lower']), m_mid=line_out(monthly_line['mid']),
                   q_lower=line_out(quarterly_line['lower']), q_mid=line_out(quarterly_line['mid'])),
        levels=[int(x) for x in levels],
        zone_weeks=[zone_names(flags, k) for k in range(len(weeks))],
        zone_rows=rows_zones,
        zone_summary={key: dict(name=name, strength=strength, strength_name=label,
                                segments=sum(1 for r in rows_zones if r['zone'] == key),
                                weeks=int(np.asarray(flags[key]).sum()))
                      for key, name, strength, label in ZONES})


def main():
    parser = argparse.ArgumentParser(description='weekly Bollinger prediction points and zone bands')
    parser.add_argument('--security', default='META')
    parser.add_argument('--db', default=DEFAULT_DB)
    parser.add_argument('--quarter', type=int, default=DEFAULT_QUARTER_N, choices=QUARTER_NS)
    parser.add_argument('--width', type=float, default=DEFAULT_WIDTH, choices=WIDTHS)
    parser.add_argument('--out', default='')
    args = parser.parse_args()
    data = series(args.security, args.db, quarter_n=args.quarter, width=args.width)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding='utf8', newline='\n')
    else:
        print(text)


if __name__ == '__main__':
    main()
