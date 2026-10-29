"""Weekly Bollinger "prediction points": weeks where the smoothed mid line is about to stop falling.

The mid line is a 20-week Hann-weighted mean of weekly total-return closes. Its first derivative `s` says
whether the line is still falling; the second derivative `a` says how fast the fall is slowing. A week is a
prediction point when the line still falls (`s < 0`) but one more week at the current deceleration would
flatten it (`s + a >= 0`). Every number at week `t` uses weeks up to `t` only; the weighted mean already
satisfies that, so nothing is refitted afterwards.

The standard 20-week Bollinger bands (simple mean, population standard deviation) are computed only as a
reference for the chart. Registered in research/boll_v1/spec.md.

    python -X utf8 -m leadtrend_bt.boll_weekly --security META
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


def bollinger(closes, n: int = WINDOW, k: float = BAND_K) -> dict:
    """Simple mean and ±k population standard deviations, for reference only."""
    values = np.asarray(closes, dtype=float)
    mid = np.full(values.size, np.nan)
    sd = np.full(values.size, np.nan)
    for t in range(n - 1, values.size):
        window = values[t - n + 1:t + 1]
        mid[t] = float(window.mean())
        sd[t] = float(window.std(ddof=0))
    return dict(mid=mid, upper=mid + k * sd, lower=mid - k * sd)


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


def series(security: str, db_path: str | None = None, end: str = DEV_END,
           start: str = DEV_START) -> dict:
    """One security's weekly closes, mid line, derivatives, prediction points and reference bands.

    Prices are the total-return index rescaled so the first week of the window shows that week's real close;
    `meta['price_scale']` is the factor. Dates, derivatives and ratios are unaffected.
    """
    if end > DEV_END:
        raise ValueError(f'end {end} is after {DEV_END}: the reserved segment must not be used')
    db = db_path or DEFAULT_DB
    sid = ma_lab.resolve_security(security, db)
    rows = api.tri_series([sid], '1900-01-01', end, db)[sid]
    if not rows:
        raise ValueError(f'{security} has no total-return series up to {end}')
    weeks_all, closes_all = weekly_closes([r[0] for r in rows], [r[1] for r in rows])
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
    return dict(
        meta=dict(ticker=security.upper(), security_id=sid, start=start, end=end, weeks=len(weeks),
                  first_week=weeks[0] if weeks else None, last_week=weeks[-1] if weeks else None,
                  price_scale=factor),
        weeks=weeks, close=close, mid=mid, s=s, s_pct=s_pct,
        band=dict(upper=scaled(cut(band_all['upper'])), lower=scaled(cut(band_all['lower'])),
                  mid=scaled(cut(band_all['mid']))),
        marks=[dict(date=weeks[k], mid=mid[k], close=close[k])
               for k, i in enumerate(keep) if is_prediction(s_all, a_all)[i]],
        points=points)


def main():
    parser = argparse.ArgumentParser(description='weekly Bollinger prediction points for one security')
    parser.add_argument('--security', default='META')
    parser.add_argument('--db', default=DEFAULT_DB)
    parser.add_argument('--out', default='')
    args = parser.parse_args()
    data = series(args.security, args.db)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding='utf8', newline='\n')
    else:
        print(text)


if __name__ == '__main__':
    main()
