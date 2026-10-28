"""Fast path for the dashboard: the same 462 combination rows as ma_lab.run_lab, in one pass.

ma_lab.run_lab rebuilds the day index and the daily state for every combination, walks the drawdown in
Python, and the dashboard then recomputes the state again for the win rate. Here the index, the cash-rate
series and the signal positions are built once, the daily state is a repeat over signal periods, and the
drawdown is a running maximum. The numbers are identical to run_lab's rows (tests_bt/test_dashboard.py
compares them combination by combination) and roughly seven times faster.
"""
from __future__ import annotations

import numpy as np

from leadtrend_bt import ma_lab

import winrate

TRADING_DAYS = 252


def statistics(days: list[str], equity: np.ndarray, rates: np.ndarray) -> dict:
    """Same numbers as ma_lab.statistics; `rates` is the cash rate aligned with days[1:]."""
    values = np.asarray(equity, dtype=float)
    returns = values[1:] / values[:-1] - 1
    sessions = len(returns)
    cagr = (values[-1] / values[0]) ** (TRADING_DAYS / sessions) - 1 if sessions else float('nan')
    vol = float(np.std(returns, ddof=1)) * np.sqrt(TRADING_DAYS) if sessions > 1 else 0.0
    excess = returns - rates[:sessions]
    ex_std = float(np.std(excess, ddof=1)) if len(excess) > 1 else 0.0
    sharpe = float(np.mean(excess)) / ex_std * np.sqrt(TRADING_DAYS) if ex_std > 0 else float('nan')

    running = np.maximum.accumulate(values)
    drawdown = values / running - 1
    worst = float(drawdown.min())
    trough_i = int(np.argmin(drawdown))
    peak_value = float(running[trough_i])
    peak_i = int(np.flatnonzero(values[:trough_i + 1] >= peak_value)[-1])
    after = np.flatnonzero(values[trough_i + 1:] >= peak_value)
    below = values < running
    edges = np.diff(np.concatenate(([0], below.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return dict(cagr=float(cagr), total_return=float(values[-1] / values[0] - 1), volatility=float(vol),
                max_drawdown=worst, peak_date=days[peak_i], trough_date=days[trough_i],
                recovery_date=days[trough_i + 1 + int(after[0])] if len(after) else '',
                longest_underwater_sessions=int((ends - starts).max()) if len(starts) else 0,
                cagr_over_dd=(float(cagr) / abs(worst) if worst else float('nan')), sharpe=sharpe)


class Fast:
    """One security and window, with the shared arrays the 462 combinations reuse."""

    def __init__(self, lab):
        self.days = lab.days
        self.values = lab.values
        self.signal_days = lab.signal_days
        self.frequency = lab.frequency
        self.positions = np.array([lab.index[d] for d in self.signal_days])
        self.start = int(self.positions[0])
        self.window_days = self.days[self.start:]
        self.returns = self.values[1:] / self.values[:-1] - 1
        if lab.cash == 'dtb3':
            self.rates = np.array([lab.rf.get(d, 0.0) for d in self.days[1:]])
        else:
            self.rates = np.zeros(len(self.days) - 1)
        # the risk-free series for Sharpe is independent of the cash rule
        self.rf_daily = np.array([lab.rf.get(d, 0.0) for d in self.days[1:]])
        self.periods_per_week = 1.0 if lab.frequency == 'weekly' else 0.2

    def simulate(self, state: np.ndarray) -> np.ndarray:
        lengths = np.diff(np.append(self.positions, len(self.days)))
        daily = np.concatenate([np.zeros(self.start, dtype=bool), np.repeat(state, lengths)])
        growth = np.where(daily[:-1], 1.0 + self.returns, 1.0 + self.rates)
        return np.concatenate([[1.0], np.cumprod(growth[self.start:])])

    def segments(self, state: np.ndarray) -> list[dict]:
        out, entry = [], None
        for k, on in enumerate(state):
            if on and entry is None:
                entry = k
            elif not on and entry is not None:
                out.append(self._segment(entry, k, False))
                entry = None
        if entry is not None:
            out.append(self._segment(entry, len(state) - 1, True))
        return out

    def _segment(self, first: int, last: int, is_open: bool) -> dict:
        i = int(self.positions[first])
        j = len(self.days) - 1 if is_open else int(self.positions[last])
        return dict(entry=self.signal_days[first], exit=self.days[j], open_end=is_open,
                    bars=last - first + (1 if is_open else 0), ret=float(self.values[j] / self.values[i] - 1))

    def hold_row(self) -> dict:
        state = np.ones(len(self.signal_days), dtype=bool)
        equity = self.simulate(state)
        row = statistics(self.window_days, equity, self.rf_daily[self.start:])
        row.update(kind='buy_hold', type='buy_hold', fast='', slow='', invested_share=1.0, buys=1,
                   average_holding_weeks=len(self.signal_days) * self.periods_per_week,
                   first_valid_signal_day='', warmup_short=0, frequency=self.frequency)
        return row

    def combo_rows(self, lab, types, fasts, slows) -> list[dict]:
        rows = []
        for kind in types:
            for fast in fasts:
                for slow in slows:
                    if int(fast) >= int(slow):
                        continue
                    line_fast, line_slow = lab.average(kind, fast), lab.average(kind, slow)
                    valid = np.isfinite(line_fast) & np.isfinite(line_slow)
                    picked = (valid & (line_fast > line_slow))[self.positions]
                    equity = self.simulate(picked)
                    row = statistics(self.window_days, equity, self.rf_daily[self.start:])
                    segments = self.segments(picked)
                    valid_days = [d for d, ok in zip(self.signal_days, valid[self.positions]) if ok]
                    first_valid = valid_days[0] if valid_days else ''
                    buys = int(np.count_nonzero(picked & ~np.concatenate(([False], picked[:-1]))))
                    row.update(winrate.stats(segments))
                    row.update(kind=kind, type=kind, fast=int(fast), slow=int(slow),
                               invested_share=float(picked.mean()), buys=buys,
                               average_holding_weeks=(float(picked.sum()) / buys * self.periods_per_week
                                                      if buys else 0.0),
                               first_valid_signal_day=first_valid,
                               warmup_short=int(bool(first_valid) and first_valid > self.signal_days[0]),
                               frequency=self.frequency, name=f'{kind}_{int(fast)}_{int(slow)}')
                    rows.append(row)
        return rows
