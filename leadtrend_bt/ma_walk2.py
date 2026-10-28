"""Second walk-forward round: pre-warmed eligibility, five control lines, and the frozen holdout.

On top of wf_v1:

* a combination can be chosen only when both of its lines are already valid on the scoring start S
  (301 sessions after the stock's first observation, and not before 2014-02-28), so a long-warm-up
  average cannot win by sitting in cash while it warms up;
* every stock is paired against five controls: common parameters (median pick across stocks), borrowed
  parameters (another stock's pick, reassigned at random), fixed SMA 50/200, buy-and-hold, and a
  constant-fraction mix of the stock and cash at the own-pick exposure;
* the holdout freezes the 2024-06-28 picks and runs them, unchanged, over 2024-07-01..2026-06-30.

`ma_walk` is left untouched so the wf_v1 numbers stay reproducible.
"""
from __future__ import annotations

import math
import warnings
from collections import Counter

import numpy as np

from leadtrend_bt import ma_lab, ma_walk
from leadtrend_db import api

TYPES = ma_walk.TYPES
FASTS = ma_walk.FASTS
SLOWS = ma_walk.SLOWS
COMBOS = ma_walk.COMBOS
COMBOS_INDEX = {combo: i for i, combo in enumerate(COMBOS)}
SELECTION_YEARS = ma_walk.SELECTION_YEARS
MIN_HISTORY = ma_walk.MIN_HISTORY
FIXED = ma_walk.FIXED
FIXED_INDEX = COMBOS_INDEX[FIXED]
TRADING_DAYS = ma_walk.TRADING_DAYS
WARMUP = 301
COVERAGE = 0.9
SEEDS = 20
MAX_DRAWS = 100
HOLDOUT_ANCHOR = '2024-06-28'
HOLDOUT_END = ma_lab.HOLDOUT_END
LINES = ('a', 'b', 'c', 'd', 'e', 'f')
CONTROLS = ('b', 'c', 'd', 'e', 'f')

stock_list = ma_walk.stock_list


class Walk2(ma_walk.Walk):
    """A `ma_walk.Walk` with a scoring start and the combinations that may be chosen."""

    def __init__(self, days, values, signal_days, cash_rate='zero', frequency='daily', rf=None,
                 start='2014-02-28'):
        super().__init__(days, values, signal_days, cash_rate, frequency, rf)
        first_ge_start = next((i for i, day in enumerate(self.days) if day >= start), len(self.days) - 1)
        self.s_index = min(len(self.days) - 1, max(WARMUP, first_ge_start))
        self.s_col = int(self.s_index - self.start)
        if self.s_col < 0:
            raise ValueError('the scoring start is before the first signal day')
        self.eligible = np.array([self._valid_at_start(kind, fast, slow) for kind, fast, slow in COMBOS],
                                 dtype=bool)
        self._valid = None
        self._analysis = None

    def _valid_at_start(self, kind: str, fast: int, slow: int) -> bool:
        return bool(np.isfinite(self.line(kind, fast)[self.s_index]) and
                    np.isfinite(self.line(kind, slow)[self.s_index]))

    def valid_matrix(self) -> np.ndarray:
        if self._valid is None:
            out = np.zeros((len(COMBOS), len(self.signal_days)), dtype=bool)
            for i, (kind, fast, slow) in enumerate(COMBOS):
                line_fast, line_slow = self.line(kind, fast), self.line(kind, slow)
                out[i] = (np.isfinite(line_fast) & np.isfinite(line_slow))[self.positions]
            self._valid = out
        return self._valid

    # -- selection -----------------------------------------------------------------------------------------------

    def ratios_at(self, anchor: str | None = None, year: int | None = None) -> dict | None:
        """Lookback ratios of every eligible combination up to one selection day."""
        if anchor is None:
            indices = [k for k, day in enumerate(self.signal_days) if day[:4] == str(year)]
            if not indices:
                return None
            sig = indices[-1]
        else:
            indices = [k for k, day in enumerate(self.signal_days) if day <= anchor]
            if not indices:
                return None
            sig = indices[-1]
        col = int(self.positions[sig] - self.start)
        sessions = col - self.s_col
        if sessions < MIN_HISTORY:
            return None
        curves = self.curves()
        running = np.maximum.accumulate(curves[:, self.s_col:], axis=1)
        ratio = ma_walk.Walk._ratio(curves[:, self.s_col:col + 1], running[:, :sessions + 1], sessions)
        ratio[~self.eligible] = np.nan
        return dict(signal_index=sig, column=col, date=self.signal_days[sig], sessions=sessions, ratio=ratio)

    def analyse(self) -> dict:
        if self._analysis is None:
            ratios, picks = {}, []
            for year in SELECTION_YEARS:
                item = self.ratios_at(year=year)
                if item is None or not np.isfinite(item['ratio']).any():
                    continue
                ratios[year] = item
                best = int(np.nanargmax(item['ratio']))
                kind, fast, slow = COMBOS[best]
                picks.append(dict(year=year, signal_index=item['signal_index'], column=item['column'],
                                  date=item['date'], sessions=item['sessions'],
                                  combo=best, ratio=float(item['ratio'][best]),
                                  type=kind, fast=fast, slow=slow))
            self._analysis = dict(ratios=ratios, picks=picks)
        return self._analysis

    # -- lines ---------------------------------------------------------------------------------------------------

    def assignment_state(self, assignment: dict[int, int], first_sig: int) -> np.ndarray:
        picks = self.analyse()['picks']
        states = self.states()
        out = np.zeros(len(self.signal_days), dtype=bool)
        for j, pick in enumerate(picks):
            combo = assignment.get(pick['year'])
            if combo is None:
                raise ValueError(f"no combination assigned for {pick['year']}")
            stop = picks[j + 1]['signal_index'] if j + 1 < len(picks) else len(self.signal_days)
            out[pick['signal_index']:stop] = states[int(combo), pick['signal_index']:stop]
        return out[first_sig:]

    def empty_counts(self, assignment: dict[int, int], first_sig: int) -> dict:
        """Split the flat days into 'no signal yet' and 'a valid signal that says flat'."""
        picks = self.analyse()['picks']
        states, valid = self.states(), self.valid_matrix()
        no_signal = flat = 0
        for j, pick in enumerate(picks):
            combo = int(assignment[pick['year']])
            stop = picks[j + 1]['signal_index'] if j + 1 < len(picks) else len(self.signal_days)
            seg = slice(pick['signal_index'], stop)
            ok = valid[combo, seg]
            on = states[combo, seg]
            no_signal += int(np.count_nonzero(~ok))
            flat += int(np.count_nonzero(ok & ~on))
        return dict(no_signal=no_signal, flat_signal=flat)

    def line_metrics(self, state: np.ndarray, first_sig: int) -> tuple[dict, np.ndarray, list[str]]:
        first_day = int(self.positions[first_sig])
        curve = self.simulate(state, first_sig)
        days = self.days[first_day:]
        rf = self.rf_by_day[first_day + 1:]
        row = ma_walk.statistics(days, curve, rf)
        buys = int(np.count_nonzero(state & ~np.concatenate(([False], state[:-1]))))
        row.update(invested_share=float(np.mean(state)) if len(state) else 0.0, buys=buys,
                   annual=ma_walk.annual_returns(days, curve))
        return row, curve, days

    def mix_metrics(self, share: float, first_sig: int) -> tuple[dict, np.ndarray]:
        """Constant fraction `share` in the stock, the rest in cash at zero, rebalanced every session."""
        first_day = int(self.positions[first_sig])
        growth = 1.0 + share * self.returns[first_day:]
        curve = np.concatenate([[1.0], np.cumprod(growth)])
        days = self.days[first_day:]
        rf = self.rf_by_day[first_day + 1:]
        row = ma_walk.statistics(days, curve, rf)
        row.update(invested_share=float(share), buys=1 if share > 0 else 0,
                   annual=ma_walk.annual_returns(days, curve))
        return row, curve

    def holdout_state(self, combo: int, anchor: str = HOLDOUT_ANCHOR) -> tuple[np.ndarray, int]:
        indices = [k for k, day in enumerate(self.signal_days) if day <= anchor]
        if not indices:
            raise ValueError(f'no signal day at or before {anchor}')
        sig = indices[-1]
        return self.states()[int(combo), sig:], sig

    def empty_for(self, combo: int, first_sig: int) -> dict:
        valid = self.valid_matrix()[int(combo), first_sig:]
        on = self.states()[int(combo), first_sig:]
        return dict(no_signal=int(np.count_nonzero(~valid)),
                    flat_signal=int(np.count_nonzero(valid & ~on)))


def split_returns(days: list[str], curve) -> list[dict]:
    """The holdout split: 2024 second half, 2025, 2026 first half, from one curve starting at the anchor."""
    values = np.asarray(curve, dtype=float)
    out, base = [], 0
    for label, low, high in (('2024H2', '2024-06-28', '2024-12-31'), ('2025', '2025-01-01', '2025-12-31'),
                             ('2026H1', '2026-01-01', '2026-06-30')):
        indices = [i for i, day in enumerate(days) if low <= day <= high]
        if not indices:
            out.append(dict(period=label, value=None, start=None, end=None))
            continue
        end = indices[-1]
        out.append(dict(period=label, value=float(values[end] / values[base] - 1),
                        start=days[base], end=days[end]))
        base = end
    return out


# -- helpers -----------------------------------------------------------------------------------------------------

def median(values):
    values = [value for value in values if value is not None]
    return float(np.median(values)) if values else None


def median_row(rows: list[dict]) -> dict:
    """Median of every numeric field; dates and annual tables come from the representative row."""
    keys = [key for key, value in rows[0].items() if isinstance(value, (int, float)) and not isinstance(value, bool)]
    out = {}
    for key in keys:
        values = [row[key] for row in rows if row.get(key) is not None]
        out[key] = median(values)
    return out


def pair_diff(own: dict | None, other: dict | None) -> dict:
    out = {}
    for field in ('cagr', 'max_drawdown', 'cagr_over_dd'):
        if not own or not other or own.get(field) is None or other.get(field) is None:
            out[field] = None
        else:
            out[field] = float(own[field] - other[field])
    return out


def build_stock(walk: Walk2, b_assignment: dict[int, int], c_assignments: dict[int, dict[int, int | None]],
                mix_share: float | None = None) -> dict:
    """Six lines on the stock's own evaluation window, plus the pairwise differences against A."""
    analysis = walk.analyse()
    picks = analysis['picks']
    if not picks:
        return dict(computable=False, reason=f'no selection day has {MIN_HISTORY} sessions of history')
    first_sig = picks[0]['signal_index']
    own = {pick['year']: pick['combo'] for pick in picks}
    lines, curves, empty = {}, {}, {}

    state_a = walk.assignment_state(own, first_sig)
    lines['a'], curves['a'], days = walk.line_metrics(state_a, first_sig)
    empty['a'] = walk.empty_counts(own, first_sig)

    state_b = walk.assignment_state(b_assignment, first_sig)
    lines['b'], curves['b'], _ = walk.line_metrics(state_b, first_sig)
    empty['b'] = walk.empty_counts(b_assignment, first_sig)

    seed_rows, seed_curves, seed_empty, seed_ratios, used_seeds = [], [], [], [], []
    for seed in sorted(c_assignments):
        assignment = c_assignments[seed]
        if any(assignment.get(year) is None for year in own):
            continue
        state = walk.assignment_state(assignment, first_sig)
        row, curve, _ = walk.line_metrics(state, first_sig)
        seed_rows.append(row)
        seed_curves.append(curve)
        seed_empty.append(walk.empty_counts(assignment, first_sig))
        seed_ratios.append(row['cagr_over_dd'])
        used_seeds.append(seed)
    if seed_rows:
        finite = [(abs(value), i) for i, value in enumerate(seed_ratios)
                  if value is not None and math.isfinite(value)]
        representative = min(finite)[1] if finite else 0
        lines['c'] = median_row(seed_rows)
        lines['c']['annual'] = seed_rows[representative]['annual']
        lines['c']['peak_date'] = seed_rows[representative]['peak_date']
        lines['c']['trough_date'] = seed_rows[representative]['trough_date']
        lines['c']['recovery_date'] = seed_rows[representative]['recovery_date']
        curves['c'] = seed_curves[representative]
        empty['c'] = dict(no_signal=int(np.median([item['no_signal'] for item in seed_empty])),
                          flat_signal=int(np.median([item['flat_signal'] for item in seed_empty])))
        dispersion = dict(seeds=len(seed_rows), min_ratio=min(seed_ratios) if seed_ratios else None,
                          max_ratio=max(seed_ratios) if seed_ratios else None,
                          representative_seed=used_seeds[representative])
    else:
        lines['c'], curves['c'], empty['c'] = None, None, None
        dispersion = dict(seeds=0, min_ratio=None, max_ratio=None, representative_seed=None)

    state_d = walk.states()[FIXED_INDEX][first_sig:]
    lines['d'], curves['d'], _ = walk.line_metrics(state_d, first_sig)
    empty['d'] = walk.empty_counts({year: FIXED_INDEX for year in own}, first_sig)

    state_e = np.ones(len(walk.signal_days) - first_sig, dtype=bool)
    lines['e'], curves['e'], _ = walk.line_metrics(state_e, first_sig)

    share = lines['a']['invested_share'] if mix_share is None else float(mix_share)
    lines['f'], curves['f'] = walk.mix_metrics(share, first_sig)

    diffs = {key: pair_diff(lines['a'], lines.get(key)) for key in CONTROLS}
    return dict(
        computable=True,
        evaluation_start=days[0], evaluation_end=days[-1], evaluation_sessions=len(days) - 1,
        from2016=days[0][:4] == '2016',
        lines=lines, curves=curves, empty=empty, diffs=diffs, picks=picks,
        eligible_by_year={int(year): int(np.count_nonzero(np.isfinite(item['ratio'])))
                          for year, item in analysis['ratios'].items()},
        b={int(year): int(combo) for year, combo in b_assignment.items()},
        c={int(seed): {int(year): (None if combo is None else int(combo))
                       for year, combo in assignment.items()}
           for seed, assignment in c_assignments.items()},
        c_dispersion=dispersion,
        mix_share=float(share),
        never_eligible=int(np.count_nonzero(~walk.eligible)),
        eligible_count=int(np.count_nonzero(walk.eligible)),
    )


def holdout_lines(walk: Walk2, combo_a: int, combo_b: int, c_combos: dict[int, int | None],
                  mix_share: float) -> dict:
    """The six lines over the holdout window, with every parameter fixed before the window starts."""
    state_a, sig = walk.holdout_state(combo_a)
    lines, curves, empty = {}, {}, {}
    lines['a'], curves['a'], days = walk.line_metrics(state_a, sig)
    empty['a'] = walk.empty_for(combo_a, sig)

    state_b, _ = walk.holdout_state(combo_b)
    lines['b'], curves['b'], _ = walk.line_metrics(state_b, sig)
    empty['b'] = walk.empty_for(combo_b, sig)

    seed_rows, seed_ratios, seed_curves = [], [], []
    for seed in sorted(c_combos):
        combo = c_combos[seed]
        if combo is None:
            continue
        state, _ = walk.holdout_state(combo)
        row, curve, _ = walk.line_metrics(state, sig)
        seed_rows.append(row)
        seed_ratios.append(row['cagr_over_dd'])
        seed_curves.append(curve)
    if seed_rows:
        finite = [(abs(value), i) for i, value in enumerate(seed_ratios)
                  if value is not None and math.isfinite(value)]
        representative = min(finite)[1] if finite else 0
        lines['c'] = median_row(seed_rows)
        for field in ('annual', 'peak_date', 'trough_date', 'recovery_date'):
            lines['c'][field] = seed_rows[representative][field]
        curves['c'] = seed_curves[representative]
        dispersion = dict(seeds=len(seed_rows), min_ratio=min(seed_ratios), max_ratio=max(seed_ratios),
                          representative_seed=sorted(c_combos)[representative])
    else:
        lines['c'], curves['c'] = None, None
        dispersion = dict(seeds=0, min_ratio=None, max_ratio=None, representative_seed=None)

    state_d, _ = walk.holdout_state(FIXED_INDEX)
    lines['d'], curves['d'], _ = walk.line_metrics(state_d, sig)
    empty['d'] = walk.empty_for(FIXED_INDEX, sig)

    state_e = np.ones(len(walk.signal_days) - sig, dtype=bool)
    lines['e'], curves['e'], _ = walk.line_metrics(state_e, sig)

    lines['f'], curves['f'] = walk.mix_metrics(mix_share, sig)
    diffs = {key: pair_diff(lines['a'], lines.get(key)) for key in CONTROLS}
    return dict(computable=True, evaluation_start=days[0], evaluation_end=days[-1],
                evaluation_sessions=len(days) - 1, lines=lines, curves=curves, dates=days,
                empty=empty, diffs=diffs, c_dispersion=dispersion, mix_share=float(mix_share),
                assignments=dict(a=int(combo_a), b=int(combo_b),
                                 c={int(seed): (None if combo is None else int(combo))
                                    for seed, combo in c_combos.items()},
                                 d=FIXED_INDEX, f=float(mix_share)),
                splits={key: split_returns(days, curve) for key, curve in curves.items()})


# -- global selection ---------------------------------------------------------------------------------------------

def common_combos(phase1: list[dict]) -> dict[int, dict]:
    """Per year, the combination with the best median lookback ratio across the stocks that can be scored."""
    out = {}
    for year in SELECTION_YEARS:
        stocks = [stock for stock in phase1 if year in stock['ratios']]
        if not stocks:
            continue
        eligible = np.array([stock['eligible'] for stock in stocks], dtype=bool)
        ratios = np.array([stock['ratios'][year] for stock in stocks], dtype=float)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', category=RuntimeWarning)
            medians = np.nanmedian(ratios, axis=0)
        coverage = eligible.sum(axis=0) / len(stocks)
        usable = (coverage >= COVERAGE) & np.isfinite(medians)
        if not usable.any():
            continue
        best = int(np.argmax(np.where(usable, medians, -np.inf)))
        out[year] = dict(combo=best, median=float(medians[best]),
                         coverage=float(coverage[best]), stocks=len(stocks))
    return out


def _derangement(size: int, rng) -> np.ndarray:
    base = np.arange(size)
    for _ in range(1000):
        candidate = rng.permutation(size)
        if np.all(candidate != base):
            return candidate
    return np.roll(base, 1)


def borrow_year(stocks: list[dict], year: int, seed: int) -> dict[str, int | None]:
    """Hand every stock another stock's pick for that year, redrawing while the receiver cannot use it."""
    if len(stocks) < 2:
        return {stock['ticker']: None for stock in stocks}
    picks = [int(stock['picks_by_year'][year]) for stock in stocks]
    eligible = [stock['eligible'] for stock in stocks]
    rng = np.random.default_rng(seed)
    order = _derangement(len(stocks), rng)
    assigned = [picks[int(order[i])] for i in range(len(stocks))]
    for i in range(len(stocks)):
        if eligible[i][assigned[i]]:
            continue
        pool = [picks[j] for j in range(len(stocks)) if j != i]
        for _ in range(MAX_DRAWS):
            combo = pool[int(rng.integers(len(pool)))]
            if eligible[i][combo]:
                assigned[i] = combo
                break
        else:
            assigned[i] = None
    return {stocks[i]['ticker']: assigned[i] for i in range(len(stocks))}


def borrow_assignments(phase1: list[dict]) -> dict[int, dict[str, dict[int, int | None]]]:
    """{year: {ticker: {seed: combo}}} for every selection year."""
    out = {}
    for year in SELECTION_YEARS:
        stocks = [stock for stock in phase1 if year in stock['ratios']]
        if len(stocks) < 2:
            continue
        per_seed = {}
        for seed in range(SEEDS):
            per_seed[seed] = borrow_year(stocks, year, seed)
        out[year] = {stock['ticker']: {seed: per_seed[seed][stock['ticker']] for seed in per_seed}
                     for stock in stocks}
    return out


# -- loading -----------------------------------------------------------------------------------------------------

def load_walk(security: str, db_path, start: str = '2014-02-28', end: str = ma_lab.LAST_ALLOWED_END,
              allow_holdout: bool = False, authorization_path=None) -> Walk2:
    if end > ma_lab.HOLDOUT_END:
        raise ValueError(f'end {end} is after {ma_lab.HOLDOUT_END}')
    if allow_holdout:
        ma_lab.check_holdout(end, db_path, authorization_path)
    elif end > ma_lab.LAST_ALLOWED_END:
        raise ValueError(f'end {end} is after {ma_lab.LAST_ALLOWED_END}: the reserved segment must not be used')
    sid = ma_lab.resolve_security(security, db_path)
    days, values = ma_lab._total_return(sid, end, db_path)
    signal_days = [day for day in days if start <= day <= end]
    if not signal_days:
        raise ValueError('no signal day in the window')
    rf = api.rf_daily(signal_days[0], end, db_path)
    walk = Walk2(days, values, signal_days, 'zero', 'daily', rf, start=start)
    walk.sid = sid
    row = api.connect(db_path).execute(
        'select current_ticker from strategy_securities where security_id=? limit 1', (sid,)).fetchone()
    walk.ticker = row[0] if row else sid
    return walk


def walk2(security: str, start: str = '2014-02-28', end: str = ma_lab.LAST_ALLOWED_END, db_path=None,
          allow_holdout: bool = False, authorization_path=None, b_assignment=None,
          c_assignments=None, mix_share=None, with_curves: bool = False) -> dict:
    """One stock, six lines. Assignments come from the batch; without them only A, D, E and F are defined."""
    walk = load_walk(security, db_path, start, end, allow_holdout, authorization_path)
    analysis = walk.analyse()
    if not analysis['picks']:
        return dict(computable=False, reason=f'no selection day has {MIN_HISTORY} sessions of history',
                    ticker=walk.ticker, security_id=walk.sid)
    own = {pick['year']: pick['combo'] for pick in analysis['picks']}
    if b_assignment is None:
        b_assignment = own
    if c_assignments is None:
        c_assignments = {0: own}
    out = build_stock(walk, b_assignment, c_assignments, mix_share)
    out.update(ticker=walk.ticker, security_id=walk.sid, start=start, end=end,
               scoring_start=walk.days[walk.s_index], warmup_sessions=walk.s_index,
               never_eligible=out['never_eligible'])
    if with_curves:
        first_day = int(walk.positions[analysis['picks'][0]['signal_index']])
        out['dates'] = walk.days[first_day:]
        out['curves'] = {key: out['curves'][key] for key in LINES}
    return out
