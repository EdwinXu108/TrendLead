"""Walk-forward selection of one moving-average combination per stock, year by year.

Every year from 2016 to 2023 the combination with the best CAGR over drawdown on the history up to that
year's last signal day is chosen and used for the following year; nothing later than the selection day
enters the choice. The resulting line is compared with buying and holding the stock and with a fixed
SMA 50/200, plus an in-sample best that is a reference only.

Each combination's daily state and equity curve are built once for the whole window; lookback and
evaluation windows are slices of that curve, so the eight selections cost a few matrix operations rather
than eight full runs. Registered in research/wf_v1/spec.md.
"""
from __future__ import annotations

import math
from collections import Counter

import numpy as np

from leadtrend_bt import ma_lab
from leadtrend_db import api

TYPES = ('sma', 'ema', 'wma', 'dema', 'tema', 'hma', 'kama')
FASTS = (3, 5, 8, 10, 13, 20, 30, 50)
SLOWS = (20, 30, 50, 80, 100, 150, 200, 250, 300)
COMBOS = tuple((kind, int(fast), int(slow))
               for kind in TYPES for fast in FASTS for slow in SLOWS if int(fast) < int(slow))
SELECTION_YEARS = tuple(range(2016, 2024))
MIN_HISTORY = 252
FIXED = ('sma', 50, 200)
TRADING_DAYS = ma_lab.TRADING_DAYS
LAST_ALLOWED_END = ma_lab.LAST_ALLOWED_END


# -- the stock list ------------------------------------------------------------------------------------------------

def stock_list(db_path=None, as_of: str = LAST_ALLOWED_END, legacy_union: bool = False) -> list[dict]:
    """Unambiguous tickers whose security has at least one strategy-eligible row.

    By default only securities that were already eligible in a quarterly version whose effective date
    (the first session of its coverage window) is at or before `as_of` are kept, so a name that only
    enters the top 200 later is not used before it could be known. `legacy_union=True` restores the old
    behaviour of taking any version at all; that is the flawed convention the committed wf_v1 and wf_v2
    results were produced under, kept only so those results can be reproduced.

    A ticker that maps to more than one security_id is skipped, as before. Each row carries
    `first_effective` (the effective date of the first version the security was eligible in) and
    `first_report_period` (that version's report period).
    """
    conn = api.connect(db_path) if db_path else api.connect()
    effective = {row[0]: row[1] for row in conn.execute(
        'select version_id, min(active_from) from price_quarter_coverage group by version_id')}
    periods = {row[0]: row[1] for row in conn.execute('select version_id, report_period from pool_versions')}
    rows = conn.execute("""select ss.security_id as sid, ss.current_ticker as ticker, ss.version_id as version,
                                  ss.strategy_eligible as eligible, coalesce(i.name, '') as name
                           from strategy_securities ss left join issuers i on i.issuer_id = ss.issuer_id
                           where ss.current_ticker <> ''""").fetchall()
    ids_by_ticker: dict[str, set] = {}
    first: dict[str, tuple] = {}
    for row in rows:
        ids_by_ticker.setdefault(row['ticker'], set()).add(row['sid'])
        if not row['eligible'] or row['version'] not in effective:
            continue
        key = (effective[row['version']], periods.get(row['version'], ''), row['version'])
        if row['sid'] not in first or key < first[row['sid']]:
            first[row['sid']] = key
    out, seen = [], set()
    for row in rows:
        sid = row['sid']
        if sid in seen or len(ids_by_ticker[row['ticker']]) != 1:
            continue
        seen.add(sid)
        entry = first.get(sid)
        if entry is None:
            continue
        if not legacy_union and entry[0] > as_of:
            continue
        out.append(dict(ticker=row['ticker'], security_id=sid, name=row['name'],
                        first_effective=entry[0], first_report_period=entry[1]))
    return sorted(out, key=lambda item: item['ticker'])


# -- statistics ----------------------------------------------------------------------------------------------------

def _drawdown(days: list[str], equity: np.ndarray) -> dict:
    peak = np.maximum.accumulate(equity)
    drop = equity / peak - 1
    trough_i = int(np.argmin(drop))
    worst = float(drop[trough_i])
    peak_value = float(peak[trough_i])
    peak_i = int(np.flatnonzero(equity[:trough_i + 1] >= peak_value)[-1])
    after = np.flatnonzero(equity[trough_i + 1:] >= peak_value)
    below = equity < peak
    edges = np.diff(np.concatenate(([0], below.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return dict(max_drawdown=worst, peak_date=days[peak_i], trough_date=days[trough_i],
                recovery_date=days[trough_i + 1 + int(after[0])] if len(after) else '',
                longest_underwater_sessions=int((ends - starts).max()) if len(starts) else 0)


def statistics(days: list[str], equity, rf: np.ndarray) -> dict:
    """Same numbers as ma_lab.statistics; `rf` is the daily risk-free rate aligned with days[1:]."""
    values = np.asarray(equity, dtype=float)
    rets = values[1:] / values[:-1] - 1
    sessions = len(rets)
    cagr = float((values[-1] / values[0]) ** (TRADING_DAYS / sessions) - 1) if sessions else float('nan')
    vol = float(np.std(rets, ddof=1)) * math.sqrt(TRADING_DAYS) if sessions > 1 else 0.0
    excess = rets - rf[:sessions]
    ex_std = float(np.std(excess, ddof=1)) if len(excess) > 1 else 0.0
    sharpe = float(np.mean(excess)) / ex_std * math.sqrt(TRADING_DAYS) if ex_std > 0 else float('nan')
    dd = _drawdown(days, values)
    return dict(cagr=cagr, total_return=float(values[-1] / values[0] - 1), volatility=vol,
                cagr_over_dd=(cagr / abs(dd['max_drawdown']) if dd['max_drawdown'] else float('nan')),
                sharpe=sharpe, **dd)


def annual_returns(days: list[str], equity) -> list[dict]:
    """Calendar-year returns; a first year that starts after January and a last year that ends before
    December are marked partial. The year runs from the previous year's last close."""
    values = np.asarray(equity, dtype=float)
    out, first, total = [], 0, len(days)
    for i in range(1, total + 1):
        if i == total or days[i][:4] != days[first][:4]:
            partial = (first == 0 and days[0][5:7] != '01') or (i == total and days[-1][5:7] != '12')
            base = values[first - 1] if first > 0 else values[0]
            out.append(dict(year=days[first][:4], value=float(values[i - 1] / base - 1), partial=bool(partial)))
            first = i
    return out


def _segment(curve: np.ndarray, first: int, last: int) -> dict:
    seg = curve[first:last + 1]
    return dict(ret=float(seg[-1] / seg[0] - 1),
                max_drawdown=float((seg / np.maximum.accumulate(seg) - 1).min()))


# -- the walk ------------------------------------------------------------------------------------------------------

class Walk:
    """One security, one window, one frequency and cash rule; every combination's curve built once."""

    def __init__(self, days, values, signal_days, cash_rate='zero', frequency='daily', rf=None):
        self.days = list(days)
        self.values = np.asarray(values, dtype=float)
        self.signal_days = list(signal_days)
        self.cash_rate = cash_rate
        self.frequency = frequency
        self.rf = rf or {}
        self.index = {d: i for i, d in enumerate(self.days)}
        self.positions = np.array([self.index[d] for d in self.signal_days], dtype=int)
        self.start = int(self.positions[0])
        self.returns = self.values[1:] / self.values[:-1] - 1
        self.rf_by_day = np.array([self.rf.get(d, 0.0) for d in self.days])
        self.rates_by_day = self.rf_by_day if cash_rate == 'dtb3' else np.zeros(len(self.days))
        self.periods_per_week = 1.0 if frequency == 'weekly' else 0.2
        self._lines: dict[tuple[str, int], np.ndarray] = {}
        self._states: np.ndarray | None = None
        self._curves: np.ndarray | None = None
        self._hold: np.ndarray | None = None

    def line(self, kind: str, n: int) -> np.ndarray:
        key = (kind, int(n))
        if key not in self._lines:
            self._lines[key] = ma_lab.MOVING_AVERAGES[kind](self.values, int(n))
        return self._lines[key]

    def states(self) -> np.ndarray:
        """One boolean row per combination over the signal days."""
        if self._states is None:
            out = np.zeros((len(COMBOS), len(self.signal_days)), dtype=bool)
            for i, (kind, fast, slow) in enumerate(COMBOS):
                line_fast, line_slow = self.line(kind, fast), self.line(kind, slow)
                valid = np.isfinite(line_fast) & np.isfinite(line_slow)
                out[i] = (valid & (line_fast > line_slow))[self.positions]
            self._states = out
        return self._states

    def curves(self) -> np.ndarray:
        """Equity of every combination from the first signal day, normalised to 1 there."""
        if self._curves is None:
            lengths = np.diff(np.append(self.positions, len(self.days)))
            daily = np.repeat(self.states(), lengths, axis=1)
            growth = np.where(daily[:, :-1], 1.0 + self.returns[self.start:],
                              1.0 + self.rates_by_day[self.start + 1:])
            self._curves = np.concatenate([np.ones((len(COMBOS), 1)), np.cumprod(growth, axis=1)], axis=1)
        return self._curves

    def hold_curve(self) -> np.ndarray:
        if self._hold is None:
            growth = 1.0 + self.returns[self.start:]
            self._hold = np.concatenate([[1.0], np.cumprod(growth)])
        return self._hold

    def simulate(self, state: np.ndarray, first_signal: int) -> np.ndarray:
        """Equity from `first_signal`'s close; `state` covers signal days from `first_signal` on."""
        first_day = int(self.positions[first_signal])
        lengths = np.diff(np.append(self.positions[first_signal:], len(self.days)))
        daily = np.repeat(np.asarray(state, dtype=bool), lengths)
        growth = np.where(daily[:-1], 1.0 + self.returns[first_day:],
                          1.0 + self.rates_by_day[first_day + 1:])
        return np.concatenate([[1.0], np.cumprod(growth)])

    # -- selection -----------------------------------------------------------------------------------------------

    @staticmethod
    def _ratio(segment: np.ndarray, running: np.ndarray, sessions: int) -> np.ndarray:
        """CAGR over |max drawdown| on one window; NaN when there is no drawdown to divide by."""
        cagr = (segment[:, -1] / segment[:, 0]) ** (TRADING_DAYS / sessions) - 1
        drop = (segment / running - 1).min(axis=1)
        with np.errstate(invalid='ignore', divide='ignore'):
            return np.where(drop < 0, cagr / np.abs(drop), np.nan)

    def selections(self, curves: np.ndarray) -> list[dict]:
        """The yearly pick, using only the prefix that ends on the selection day."""
        running = np.maximum.accumulate(curves, axis=1)
        picks = []
        for year in SELECTION_YEARS:
            indices = [k for k, day in enumerate(self.signal_days) if day[:4] == str(year)]
            if not indices:
                continue
            sig = indices[-1]
            col = int(self.positions[sig] - self.start)
            if col < MIN_HISTORY:
                continue
            ratio = self._ratio(curves[:, :col + 1], running[:, :col + 1], col)
            if not np.isfinite(ratio).any():
                continue
            best = int(np.nanargmax(ratio))
            picks.append(dict(year=year, signal_index=sig, column=col, combo=best, ratio=float(ratio[best])))
        return picks

    # -- the result ----------------------------------------------------------------------------------------------

    def result(self, with_curves: bool = False) -> dict:
        curves = self.curves()
        picks = self.selections(curves)
        if not picks:
            return dict(computable=False, reason=f'no selection day has {MIN_HISTORY} sessions of history',
                        selections=[], stability=dict(changes=0, distinct=0, selections=0, types={}))
        states = self.states()
        first_sig = picks[0]['signal_index']
        first_day = int(self.positions[first_sig])
        eval_start, eval_end = self.days[first_day], self.days[-1]
        eval_days = self.days[first_day:]
        rf_eval = self.rf_by_day[first_day + 1:]

        # the walk-forward state: the latest pick at or before each signal day
        wf_state = np.zeros(len(self.signal_days), dtype=bool)
        for j, pick in enumerate(picks):
            stop = picks[j + 1]['signal_index'] if j + 1 < len(picks) else len(self.signal_days)
            wf_state[pick['signal_index']:stop] = states[pick['combo'], pick['signal_index']:stop]
        wf_curve = self.simulate(wf_state[first_sig:], first_sig)

        # every other line on the same evaluation window, normalised to 1 at its start
        hold_full = self.hold_curve()[first_day - self.start:]
        hold_curve = hold_full / hold_full[0]
        fixed_index = COMBOS.index(FIXED)
        fixed_full = curves[fixed_index, first_day - self.start:]
        fixed_curve = fixed_full / fixed_full[0]
        eval_column = first_day - self.start
        eval_segment = curves[:, eval_column:]
        running_eval = np.maximum.accumulate(eval_segment, axis=1)
        ratio_eval = self._ratio(eval_segment, running_eval, eval_segment.shape[1] - 1)
        if np.isfinite(ratio_eval).any():
            ex_index = int(np.nanargmax(ratio_eval))
            ex_full = curves[ex_index, eval_column:]
            ex_curve = ex_full / ex_full[0]
        else:
            ex_index, ex_curve = None, None

        def line_of(curve, state, invested=None, buys=None) -> dict:
            row = statistics(eval_days, curve, rf_eval)
            if invested is None:
                invested = float(np.mean(state)) if len(state) else 0.0
            if buys is None:
                buys = int(np.count_nonzero(state & ~np.concatenate(([False], state[:-1]))))
            row.update(invested_share=float(invested), buys=int(buys),
                       annual=annual_returns(eval_days, curve))
            return row

        lines = dict(
            wf=line_of(wf_curve, wf_state[first_sig:]),
            hold=line_of(hold_curve, np.ones(len(self.signal_days) - first_sig, dtype=bool), 1.0, 1),
            fixed=line_of(fixed_curve, states[fixed_index, first_sig:]),
        )
        if ex_curve is not None:
            lines['ex_post'] = line_of(ex_curve, states[ex_index, first_sig:])
            lines['ex_post'].update(type=COMBOS[ex_index][0], fast=COMBOS[ex_index][1],
                                    slow=COMBOS[ex_index][2])
        else:
            lines['ex_post'] = dict(type=None, fast=None, slow=None)

        # one row per selection: what was picked, and what the next period delivered
        rows = []
        for j, pick in enumerate(picks):
            kind, fast, slow = COMBOS[pick['combo']]
            row = dict(year=pick['year'], date=self.signal_days[pick['signal_index']],
                       type=kind, fast=fast, slow=slow, lookback_cagr_over_dd=pick['ratio'],
                       lookback_sessions=int(pick['column']))
            nxt = picks[j + 1] if j + 1 < len(picks) else None
            stop_day = int(self.positions[nxt['signal_index']]) if nxt else len(self.days) - 1
            here_day = int(self.positions[pick['signal_index']])
            period = dict(start=self.days[here_day], end=self.days[stop_day],
                          sessions=stop_day - here_day)
            period['wf'] = _segment(wf_curve, here_day - first_day, stop_day - first_day)
            period['hold'] = _segment(hold_curve, here_day - first_day, stop_day - first_day)
            period['fixed'] = _segment(fixed_curve, here_day - first_day, stop_day - first_day)
            row['period'] = period
            rows.append(row)

        changes = sum(1 for a, b in zip(picks, picks[1:]) if a['combo'] != b['combo'])
        out = dict(
            computable=True,
            evaluation_start=eval_start, evaluation_end=eval_end,
            evaluation_sessions=len(eval_days) - 1,
            complete=picks[0]['year'] == SELECTION_YEARS[0],
            selections=rows,
            stability=dict(selections=len(picks), changes=int(changes),
                           distinct=len({p['combo'] for p in picks}),
                           types=dict(Counter(COMBOS[p['combo']][0] for p in picks))),
            lines=lines,
            fixed=dict(type=FIXED[0], fast=FIXED[1], slow=FIXED[2]),
            ex_post=dict(type=COMBOS[ex_index][0], fast=COMBOS[ex_index][1], slow=COMBOS[ex_index][2])
            if ex_index is not None else None,
        )
        if with_curves:
            out['curves'] = dict(dates=eval_days, wf=wf_curve, hold=hold_curve, fixed=fixed_curve)
        return out


# -- public entry point --------------------------------------------------------------------------------------------

def walk(security: str, start: str = '2014-02-28', end: str = LAST_ALLOWED_END, cash_rate: str = 'zero',
         frequency: str = 'daily', db_path=None, with_curves: bool = False) -> dict:
    if end > LAST_ALLOWED_END:
        raise ValueError(f'end {end} is after {LAST_ALLOWED_END}: the reserved segment must not be used')
    if start < ma_lab.FIRST_DAY:
        raise ValueError(f'start {start} is before {ma_lab.FIRST_DAY}')
    if start >= end:
        raise ValueError('start must be before end')
    if cash_rate not in ('zero', 'dtb3'):
        raise ValueError("cash_rate must be 'zero' or 'dtb3'")
    if frequency not in ('daily', 'weekly'):
        raise ValueError("frequency must be 'daily' or 'weekly'")
    sid = ma_lab.resolve_security(security, db_path)
    days, values = ma_lab._total_return(sid, end, db_path)
    available = set(days)
    if frequency == 'daily':
        signal_days = [d for d in days if start <= d <= end]
    else:
        signal_days = [d for d in api.friday_signal_days(start, end, db_path) if d >= start and d in available]
    if not signal_days:
        raise ValueError('no signal day in the window')
    rf = api.rf_daily(signal_days[0], end, db_path)
    out = Walk(days, values, signal_days, cash_rate, frequency, rf).result(with_curves=with_curves)
    out.update(security=sid, start=start, end=end, cash_rate=cash_rate, frequency=frequency,
               signal_days=len(signal_days))
    conn = api.connect(db_path) if db_path else api.connect()
    row = conn.execute('select current_ticker from strategy_securities where security_id=? limit 1',
                       (sid,)).fetchone()
    out['ticker'] = row[0] if row else sid
    return out
