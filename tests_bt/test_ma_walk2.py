"""ma_walk2: eligibility at the scoring start, common and borrowed parameters, the mix, the frozen anchor."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from leadtrend_bt import ma_lab, ma_walk, ma_walk2

ROOT = Path(__file__).resolve().parent.parent
DEV_DB = ROOT / 'data' / 'db' / 'leadtrend_db_v1.sqlite'
FULL_DB = ROOT / 'data' / 'db' / 'leadtrend_db_full_v1.sqlite'
AUTH = ROOT / 'research' / 'holdout' / 'authorization.json'


def synthetic(days: int = 3400, seed: int = 7, drift: float = 0.0005, vol: float = 0.012):
    rng = np.random.default_rng(seed)
    values = 100.0 * np.exp(np.cumsum(rng.normal(drift, vol, days)))
    calendar, day = [], date(2012, 12, 3)
    while len(calendar) < days:
        if day.weekday() < 5:
            calendar.append(day.isoformat())
        day += timedelta(days=1)
    return calendar, values


def walk_of(values, calendar, start='2014-02-28') -> ma_walk2.Walk2:
    signal_days = [day for day in calendar if start <= day]
    return ma_walk2.Walk2(calendar, values, signal_days, 'zero', 'daily', {}, start=start)


def phase1_entry(ticker: str, year: int, ratios: dict[int, float], eligible: list[bool]):
    vector = np.full(len(ma_walk2.COMBOS), np.nan)
    flags = np.zeros(len(ma_walk2.COMBOS), dtype=bool)
    for index, value in ratios.items():
        vector[index] = value
        flags[index] = eligible[index]
    return dict(ticker=ticker, ratios={year: vector}, eligible=flags,
                picks_by_year={year: max(ratios, key=ratios.get)})


# -- 1. eligibility -------------------------------------------------------------------------------------------------

def test_a_combination_invalid_at_the_scoring_start_is_never_chosen():
    calendar, values = synthetic()
    walk = walk_of(values, calendar)
    assert walk.days[walk.s_index] == '2014-02-28'
    long_sma = ma_walk2.COMBOS.index(('sma', 3, 300))
    long_tema = ma_walk2.COMBOS.index(('tema', 3, 300))
    assert walk.eligible[long_sma]
    assert not walk.eligible[long_tema]
    picks = walk.analyse()['picks']
    assert picks
    assert all(walk.eligible[pick['combo']] for pick in picks)


def test_the_two_kinds_of_flat_days_are_counted_separately():
    # a stock listed in 2014: the window starts at its first session, before the long averages exist
    rng = np.random.default_rng(5)
    values = 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 400)))
    calendar, day = [], date(2014, 1, 2)
    while len(calendar) < 400:
        if day.weekday() < 5:
            calendar.append(day.isoformat())
        day += timedelta(days=1)
    walk = ma_walk2.Walk2(calendar, values, calendar, 'zero', 'daily', {}, start='2014-01-02')
    long_combo = ma_walk2.COMBOS.index(('sma', 3, 300))
    counts = walk.empty_for(long_combo, 0)
    assert counts['no_signal'] == 299 and 0 < counts['flat_signal'] < 101
    short_combo = ma_walk2.COMBOS.index(('sma', 3, 20))
    counts = walk.empty_for(short_combo, 0)
    assert counts['no_signal'] == 19 and counts['flat_signal'] > 0


# -- 2. common parameters -------------------------------------------------------------------------------------------

def test_common_parameters_follow_the_median_and_the_coverage_rule():
    year = 2020
    # combos 0, 1, 2; the third is only eligible on two of three stocks, so the 90% rule drops it
    stocks = [
        phase1_entry('A', year, {0: 0.5, 1: 0.9, 2: 0.1}, [True, True, True]),
        phase1_entry('B', year, {0: 0.7, 1: 0.8, 2: 0.2}, [True, True, False]),
        phase1_entry('C', year, {0: 0.6, 1: 0.85, 2: 0.3}, [True, True, True]),
    ]
    common = ma_walk2.common_combos(stocks)
    assert common[year]['combo'] == 1
    assert common[year]['median'] == pytest.approx(0.85)
    assert common[year]['coverage'] == pytest.approx(1.0)

    # ten stocks, the second combo eligible on exactly nine: 0.9 passes, 8/10 does not
    ten = [phase1_entry(f'S{i}', year, {0: 0.1, 1: 0.9, 2: 0.95}, [True, i < 9, i < 8]) for i in range(10)]
    common = ma_walk2.common_combos(ten)
    assert common[year]['combo'] == 1
    assert common[year]['coverage'] == pytest.approx(0.9)


# -- 3. borrowed parameters -----------------------------------------------------------------------------------------

def test_nobody_borrows_its_own_pick_and_a_seed_repeats_exactly():
    year = 2020
    stocks = [phase1_entry(f'S{i}', year, {i: 0.5 + i / 100}, [True, True, True, True, True])
              for i in range(5)]
    first = ma_walk2.borrow_year(stocks, year, seed=3)
    second = ma_walk2.borrow_year(stocks, year, seed=3)
    assert first == second
    for stock in stocks:
        assert first[stock['ticker']] != stock['picks_by_year'][year]


def test_the_receiver_computes_the_signal_on_its_own_prices():
    calendar, values_a = synthetic(seed=3)
    _, values_b = synthetic(seed=4, drift=0.0012)
    walk_a, walk_b = walk_of(values_a, calendar), walk_of(values_b, calendar)
    combo = ma_walk2.COMBOS.index(('sma', 10, 50))
    state_a = walk_a.assignment_state({pick['year']: combo for pick in walk_a.analyse()['picks']},
                                      walk_a.analyse()['picks'][0]['signal_index'])
    state_b = walk_b.assignment_state({pick['year']: combo for pick in walk_b.analyse()['picks']},
                                      walk_b.analyse()['picks'][0]['signal_index'])
    first_day = int(walk_a.positions[walk_a.analyse()['picks'][0]['signal_index']])
    growth = np.where(state_a[:-1], 1.0 + walk_a.returns[first_day:], 1.0)
    expected = np.concatenate([[1.0], np.cumprod(growth)])
    row, curve, _ = walk_a.line_metrics(state_a, walk_a.analyse()['picks'][0]['signal_index'])
    assert np.allclose(curve, expected, rtol=0, atol=1e-12)
    # the same combination on different prices is a different line
    row_b, curve_b, _ = walk_b.line_metrics(state_b, walk_b.analyse()['picks'][0]['signal_index'])
    assert not np.allclose(curve[:200], curve_b[:200])


# -- 4. the constant-fraction mix -----------------------------------------------------------------------------------

def test_full_exposure_equals_holding_and_zero_exposure_is_flat():
    calendar, values = synthetic()
    walk = walk_of(values, calendar)
    first_sig = walk.analyse()['picks'][0]['signal_index']
    hold, _, _ = walk.line_metrics(np.ones(len(walk.signal_days) - first_sig, dtype=bool), first_sig)
    full, _ = walk.mix_metrics(1.0, first_sig)
    assert full['cagr'] == pytest.approx(hold['cagr'], abs=1e-12)
    assert full['max_drawdown'] == pytest.approx(hold['max_drawdown'], abs=1e-12)
    flat, curve = walk.mix_metrics(0.0, first_sig)
    assert flat['total_return'] == pytest.approx(0.0, abs=1e-15)
    assert np.allclose(curve, 1.0)


# -- 5. the frozen anchor -------------------------------------------------------------------------------------------

def test_prices_after_the_anchor_do_not_change_the_anchor_pick():
    calendar, values = synthetic()
    walk = walk_of(values, calendar)
    before = walk.ratios_at(anchor=ma_walk2.HOLDOUT_ANCHOR)
    assert before is not None
    changed = values.copy()
    cut = next(i for i, day in enumerate(calendar) if day > ma_walk2.HOLDOUT_ANCHOR)
    changed[cut:] = changed[cut - 1] * np.exp(np.cumsum(np.full(len(values) - cut, 0.01)))
    other = walk_of(changed, calendar)
    after = other.ratios_at(anchor=ma_walk2.HOLDOUT_ANCHOR)
    assert int(np.nanargmax(before['ratio'])) == int(np.nanargmax(after['ratio']))
    assert before['ratio'][int(np.nanargmax(before['ratio']))] == pytest.approx(
        after['ratio'][int(np.nanargmax(after['ratio']))], abs=1e-12)


# -- 6. the holdout switch ------------------------------------------------------------------------------------------

def _write_auth(directory: Path, scope='wf_v2', window=('2024-07-01', '2026-06-30')) -> Path:
    path = directory / 'authorization.json'
    path.write_text(json.dumps(dict(scope=scope, window=list(window))), encoding='utf8', newline='\n')
    return path


def test_the_holdout_switch_rejects_every_missing_condition(tmp_path):
    if not FULL_DB.exists():
        pytest.skip('full-period database is not present')
    with pytest.raises(ValueError):
        ma_lab.check_holdout('2024-06-28', str(FULL_DB), tmp_path / 'missing.json')
    with pytest.raises(ValueError):
        ma_lab.check_holdout('2024-06-28', str(FULL_DB), _write_auth(tmp_path, scope='other'))
    with pytest.raises(ValueError):
        ma_lab.check_holdout('2024-06-28', str(FULL_DB), _write_auth(tmp_path, window=('2024-07-01', '2025-12-31')))
    with pytest.raises(ValueError):
        ma_lab.check_holdout('2026-07-01', str(FULL_DB), _write_auth(tmp_path))
    with pytest.raises(ValueError):
        ma_lab.check_holdout('2024-06-28', str(DEV_DB), _write_auth(tmp_path))
    # the switch being off keeps the old cut-off
    with pytest.raises(ValueError):
        ma_lab.run_lab('MSFT', start='2014-02-28', end='2024-07-01', types=['sma'], fasts=[50], slows=[200],
                       db_path=str(DEV_DB), frequency='daily')
    # and the real authorization passes
    assert AUTH.exists()
    ma_lab.check_holdout('2024-06-28', str(FULL_DB), AUTH)


# -- 7. the data release --------------------------------------------------------------------------------------------

def test_the_full_database_keeps_the_development_segment_and_fixes_the_seam():
    if not FULL_DB.exists():
        pytest.skip('full-period database is not present')
    config = json.loads((ROOT / 'config' / 'tri_seam_v1.json').read_text(encoding='utf8'))
    formal = sqlite3.connect(f'file:{DEV_DB.as_posix()}?mode=ro', uri=True)
    full = sqlite3.connect(f'file:{FULL_DB.as_posix()}?mode=ro', uri=True)

    def digest(conn):
        h = hashlib.sha256()
        for row in conn.execute("select security_id, session_date, tri, daily_return from tri_daily "
                                "where session_date <= '2024-06-28' order by security_id, session_date"):
            h.update(json.dumps(row, separators=(',', ':')).encode('utf8'))
        return h.hexdigest()

    assert digest(formal) == digest(full)
    assert full.execute("select value from meta where key='tri_seam_release'").fetchone()[0] == 'tri_seam_v1'
    for item in config['securities']:
        sid = item['security_id']
        row = full.execute("select tri, daily_return from tri_daily where security_id=? and session_date='2024-07-01'",
                           (sid,)).fetchone()
        previous = full.execute("select tri from tri_daily where security_id=? and session_date='2024-06-28'",
                                (sid,)).fetchone()[0]
        assert row[0] == pytest.approx(previous * (1 + row[1]), rel=1e-12)
    broken = full.execute("""select count(*) from (
        select tri, lag(tri) over (partition by security_id order by session_date) as previous, daily_return
        from tri_daily where session_date between '2012-12-03' and '2026-06-30')
        where daily_return is not null and previous is not null
          and abs(tri - previous * (1 + daily_return)) > 1e-9 * previous""").fetchone()[0]
    assert broken == 0
    formal.close()
    full.close()


# -- 8. wf_v1 stays reproducible ------------------------------------------------------------------------------------

def test_wf_v1_msft_is_unchanged():
    if not DEV_DB.exists():
        pytest.skip('released database is not present')
    archived = json.loads((ROOT / 'research' / 'wf_v1' / 'wf_daily_zero.json').read_text(encoding='utf8'))
    stock = next(item for item in archived['stocks'] if item['ticker'] == 'MSFT')
    out = ma_walk.walk('MSFT', db_path=str(DEV_DB))
    for key in ('wf', 'hold', 'fixed', 'ex_post'):
        for field in ('cagr', 'total_return', 'volatility', 'max_drawdown', 'cagr_over_dd', 'sharpe'):
            assert out['lines'][key][field] == pytest.approx(stock['lines'][key][field], abs=1e-9)
    assert [row['date'] for row in out['selections']] == [row['date'] for row in stock['selections']]
