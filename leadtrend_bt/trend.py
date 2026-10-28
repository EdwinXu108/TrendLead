"""Per-security moving-average trend signals and the trend-mode rule set.

Each security follows its own signal: hold while the fast line is strictly above the slow line, otherwise hold
cash. The lines are computed from the security's own total-return index over the whole stored history, not from
the window in which it joined the pool. A missing session carries the previous value forward, so one gap does not
invalidate the line.

Registered in research/trend_v1/spec.md. Nothing here is used unless mode='trend'.
"""
from __future__ import annotations

import numpy as np

SMA_RULES = ((10, 50), (10, 100), (10, 150), (10, 200), (20, 50), (20, 100), (20, 150), (20, 200),
             (50, 100), (50, 150), (50, 200))
MACD = (12, 26, 9)
RULES = tuple(('sma', f, s) for f, s in SMA_RULES) + tuple(('ema', f, s) for f, s in SMA_RULES) + (('macd', 12, 26),)


def rule_label(ma: str, fast: int, slow: int) -> str:
    if ma == 'always':
        return 'always'
    return 'macd' if ma == 'macd' else f'{ma}{fast}_{slow}'


def matrix(tri: dict[str, dict[str, float]], calendar: list[str], sids: list[str]) -> np.ndarray:
    """(sessions x securities) total-return matrix over the full stored history."""
    index = {day: i for i, day in enumerate(calendar)}
    out = np.full((len(calendar), len(sids)), np.nan)
    for j, sid in enumerate(sids):
        for day, value in tri.get(sid, {}).items():
            i = index.get(day)
            if i is not None:
                out[i, j] = value
    return out


def carry_forward(values: np.ndarray) -> np.ndarray:
    """A missing session keeps the previous value; rows before the first observation stay NaN."""
    index = np.where(np.isfinite(values), np.arange(values.shape[0])[:, None], 0)
    np.maximum.accumulate(index, axis=0, out=index)
    return values[index, np.arange(values.shape[1])[None, :]]


def sma(values: np.ndarray, n: int) -> np.ndarray:
    """Simple mean of the last n observations; NaN until n observations exist."""
    rows, cols = values.shape
    out = np.full((rows, cols), np.nan)
    if rows < n:
        return out
    valid = np.isfinite(values)
    total = np.cumsum(np.where(valid, values, 0.0), axis=0)
    count = np.cumsum(valid.astype(float), axis=0)
    sums = total[n - 1:].copy()
    sums[1:] = total[n:] - total[:-n]
    counts = count[n - 1:].copy()
    counts[1:] = count[n:] - count[:-n]
    with np.errstate(invalid='ignore', divide='ignore'):
        out[n - 1:] = np.where(counts == n, sums / n, np.nan)
    return out


def ema(values: np.ndarray, n: int) -> np.ndarray:
    """EMA seeded with the simple mean of the first n observations, then alpha = 2/(n+1)."""
    rows, cols = values.shape
    out = np.full((rows, cols), np.nan)
    if rows < n:
        return out
    alpha = 2.0 / (n + 1.0)
    valid = np.isfinite(values)
    total = np.cumsum(np.where(valid, values, 0.0), axis=0)
    count = np.cumsum(valid.astype(float), axis=0)
    current = np.full(cols, np.nan)
    started = np.zeros(cols, dtype=bool)
    for i in range(n - 1, rows):
        row = values[i]
        if i == n - 1:
            seed = count[i] == n
        else:
            seed = (~started) & (count[i] == n)
        if seed.any():
            window = total[i] - (total[i - n] if i >= n else 0.0)
            current[seed] = window[seed] / n
            started |= seed
        live = started & valid[i] & ~seed
        if live.any():
            current[live] = alpha * row[live] + (1.0 - alpha) * current[live]
        out[i] = np.where(started, current, np.nan)
    return out


def macd_line(values: np.ndarray) -> np.ndarray:
    fast, slow, _ = MACD
    return ema(values, fast) - ema(values, slow)


def bullish(values: np.ndarray, ma: str, fast: int, slow: int) -> np.ndarray:
    """True where the rule is long on that session; an insufficient history is False."""
    if ma == 'always':
        return np.ones(values.shape, dtype=bool)
    if ma == 'sma':
        f, s = sma(values, fast), sma(values, slow)
    elif ma == 'ema':
        f, s = ema(values, fast), ema(values, slow)
    elif ma == 'macd':
        line = macd_line(values)
        f, s = line, ema(line, MACD[2])
    else:
        raise ValueError(f'unknown moving average: {ma}')
    with np.errstate(invalid='ignore'):
        return np.where(np.isfinite(f) & np.isfinite(s), f > s, False)


def signal_table(backtest, ma: str, fast: int, slow: int) -> dict[str, dict[str, bool]]:
    """{signal day: {security: long?}} on the backtest's full stored calendar."""
    sids = sorted(backtest.tri)
    values = carry_forward(matrix(backtest.tri, backtest.full_calendar, sids))
    long = bullish(values, ma, fast, slow)
    index = {day: i for i, day in enumerate(backtest.full_calendar)}
    out: dict[str, dict[str, bool]] = {}
    for day in backtest.signal_days:
        i = index.get(day)
        if i is None:
            continue
        row = long[i]
        out[day] = {sid: bool(row[j]) for j, sid in enumerate(sids)}
    return out
