"""ma_walk: the yearly pick against a full rerun, the stitched curve, and the no-look-ahead rule."""
from __future__ import annotations

import csv
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from leadtrend_bt import ma_lab, ma_walk
from leadtrend_db import api

ROOT = Path(__file__).resolve().parent.parent
DB = api.DB_PATH
def synthetic(days: int = 2900, seed: int = 7):
    """A weekday calendar from 2012-12-03 and a random walk with a small drift."""
    rng = np.random.default_rng(seed)
    values = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.012, days)))
    calendar, day = [], date(2012, 12, 3)
    while len(calendar) < days:
        if day.weekday() < 5:
            calendar.append(day.isoformat())
        day += timedelta(days=1)
    return calendar, values


def walk_of(values, calendar) -> ma_walk.Walk:
    signal_days = [d for d in calendar if d >= '2014-02-28']
    return ma_walk.Walk(calendar, values, signal_days, 'zero', 'daily', {})


def test_every_yearly_pick_matches_a_full_rerun():
    """For each of the eight selection days, rerun all 462 combinations up to that day and pick again."""
    if not DB.exists():
        pytest.skip('released database is not present')
    out = ma_walk.walk('MSFT', cash_rate='zero', frequency='daily', db_path=DB)
    assert [row['year'] for row in out['selections']] == list(ma_walk.SELECTION_YEARS)
    for pick in out['selections']:
        ref = ma_lab.run_lab('MSFT', start='2014-02-28', end=pick['date'], types=ma_walk.TYPES,
                             fasts=ma_walk.FASTS, slows=ma_walk.SLOWS, cash_rate='zero',
                             db_path=DB, frequency='daily')
        rows = [row for row in ref['rows'] if math.isfinite(row['cagr_over_dd'])]
        best = max(rows, key=lambda row: row['cagr_over_dd'])
        assert (best['type'], best['fast'], best['slow']) == (pick['type'], pick['fast'], pick['slow'])
        assert abs(best['cagr_over_dd'] - pick['lookback_cagr_over_dd']) <= 1e-9


def test_a_constant_pick_reproduces_that_combination():
    calendar, values = synthetic()
    walk = walk_of(values, calendar)
    curves = walk.curves()
    real = walk.selections(curves)
    assert len(real) == 8
    combo = real[0]['combo']
    walk.selections = lambda _curves: [dict(pick, combo=combo, ratio=1.0) for pick in real]
    out = walk.result(with_curves=True)
    column = int(walk.positions[real[0]['signal_index']] - walk.start)
    expected = curves[combo, column:] / curves[combo, column]
    assert np.allclose(out['curves']['wf'], expected, rtol=0, atol=1e-12)


def test_a_switch_keeps_the_old_combination_until_the_selection_day():
    calendar, values = synthetic()
    walk = walk_of(values, calendar)
    curves = walk.curves()
    real = walk.selections(curves)
    old, new = 0, 1
    picks = [dict(pick, combo=old if index < 4 else new, ratio=1.0) for index, pick in enumerate(real)]
    walk.selections = lambda _curves: picks
    out = walk.result(with_curves=True)
    wf = out['curves']['wf']
    first_day = int(walk.positions[picks[0]['signal_index']])
    switch_day = int(walk.positions[picks[4]['signal_index']])
    column = first_day - walk.start
    assert np.allclose(wf[:switch_day - first_day],
                       curves[old, column:switch_day - walk.start] / curves[old, column], rtol=0, atol=1e-12)
    for i in range(switch_day - first_day, len(wf) - 1):
        absolute = first_day + i - walk.start
        assert wf[i + 1] / wf[i] == pytest.approx(curves[new, absolute + 1] / curves[new, absolute], rel=1e-12)


def test_changing_prices_after_a_selection_day_leaves_earlier_picks_alone():
    calendar, values = synthetic()
    walk = walk_of(values, calendar)
    base = walk.selections(walk.curves())
    cut = next(pick for pick in base if pick['year'] == 2018)
    cut_day = int(walk.positions[cut['signal_index']])
    rng = np.random.default_rng(11)
    changed = values.copy()
    changed[cut_day + 1:] = changed[cut_day] * np.exp(
        np.cumsum(rng.normal(0.0, 0.02, len(values) - cut_day - 1)))
    moved = walk_of(changed, calendar)
    other = moved.selections(moved.curves())
    assert len(other) == len(base)
    for before, after in zip(base, other):
        if before['year'] > 2018:
            break
        assert (before['combo'], before['ratio']) == (after['combo'], after['ratio'])
    # the years after the cut are allowed to change, so at least the input really moved
    assert not np.allclose(values[cut_day + 1:], changed[cut_day + 1:])


def test_fixed_and_hold_match_a_rerun_on_the_evaluation_window():
    if not DB.exists():
        pytest.skip('released database is not present')
    out = ma_walk.walk('MSFT', cash_rate='zero', frequency='daily', db_path=DB)
    ref = ma_lab.run_lab('MSFT', start=out['evaluation_start'], end='2024-06-28', types=['sma'],
                         fasts=[50], slows=[200], cash_rate='zero', db_path=DB, frequency='daily')
    for name, expected in (('fixed', ref['rows'][0]), ('hold', ref['buy_hold'])):
        got = out['lines'][name]
        for key in ('cagr', 'total_return', 'volatility', 'max_drawdown', 'cagr_over_dd', 'sharpe'):
            assert got[key] == pytest.approx(expected[key], abs=1e-9), (name, key)
        for key in ('peak_date', 'trough_date', 'recovery_date'):
            assert got[key] == expected[key], (name, key)


def test_the_stock_list_is_eligible_and_excludes_the_excluded_company():
    if not DB.exists():
        pytest.skip('released database is not present')
    stocks = ma_walk.stock_list(DB)
    assert len(stocks) == 279
    ids = {row['security_id'] for row in stocks}
    assert 'CUSIP:H1467J104' not in ids
    assert 'CB' not in {row['ticker'] for row in stocks}
    assert len({row['ticker'] for row in stocks}) == len(stocks)
    assert all(row['first_effective'] <= '2024-06-28' for row in stocks)
    conn = api.connect(DB)
    for row in stocks:
        eligible = conn.execute('select max(strategy_eligible) from strategy_securities where security_id=?',
                                (row['security_id'],)).fetchone()[0]
        assert eligible == 1, row
        # every listed ticker must also be resolvable, not just eligible
        assert ma_lab.resolve_security(row['ticker'], DB) == row['security_id'], row['ticker']
    legacy = ma_walk.stock_list(DB, legacy_union=True)
    assert len(legacy) == 318
    assert {row['ticker'] for row in stocks} < {row['ticker'] for row in legacy}


def test_the_registered_weekly_demo_still_matches_its_archive():
    """S's 168-combination MSFT demo, cell by cell, against research/ma_lab_v1/results_zero.csv."""
    if not DB.exists():
        pytest.skip('released database is not present')
    archive = ROOT / 'research' / 'ma_lab_v1' / 'results_zero.csv'
    out = ma_lab.run_lab('MSFT', start='2014-02-28', end='2024-06-28', types=ma_walk.TYPES,
                         fasts=(5, 10, 20, 30, 50), slows=(50, 100, 150, 200, 250), cash_rate='zero',
                         db_path=DB)
    rows = {(row['type'], row['fast'], row['slow']): row for row in out['rows']}
    assert len(rows) == 168
    numeric = ('cagr', 'total_return', 'volatility', 'max_drawdown', 'cagr_over_dd', 'sharpe',
               'invested_share', 'average_holding_weeks')
    with archive.open(encoding='utf8') as handle:
        for archived in csv.DictReader(handle):
            if archived['type'] == 'buy_hold':
                row = out['buy_hold']
            else:
                row = rows[(archived['type'], int(archived['fast']), int(archived['slow']))]
            for key in numeric:
                assert row[key] == pytest.approx(float(archived[key]), abs=1e-8), (archived['type'], key)
            assert row['buys'] == int(archived['buys'])
