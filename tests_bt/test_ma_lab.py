"""ma_lab: seven averages against an independent reference, the window rules, the metrics and the real database."""
from __future__ import annotations

import math

import numpy as np
import pytest

from leadtrend_bt import ma_lab
from leadtrend_db import api

NAN = None


def series():
    """Uptrend, downtrend, flat tail, with one missing session in the downtrend."""
    values = []
    price = 100.0
    for i in range(40):                      # up
        price *= 1.01
        values.append(price)
    for i in range(40):                      # down
        price *= 0.985
        values.append(price)
    for i in range(40):                      # flat
        values.append(price)
    values[60] = NAN
    return values


def filled(values):
    out, last = [], NAN
    for value in values:
        if value is not NAN:
            last = value
        out.append(last)
    return out


# -- independent reference implementations (plain loops, no module code) -----------------------------------------

def ref_sma(values, n):
    data = filled(values)
    out = [NAN] * len(data)
    for i in range(n - 1, len(data)):
        window = data[i - n + 1:i + 1]
        if all(v is not NAN for v in window):
            out[i] = sum(window) / n
    return out


def ref_ema(values, n):
    data = filled(values)
    out = [NAN] * len(data)
    first = next((i for i, v in enumerate(data) if v is not NAN), None)
    if first is None or len(data) < first + n:
        return out
    current = sum(data[first:first + n]) / n
    out[first + n - 1] = current
    alpha = 2.0 / (n + 1.0)
    for i in range(first + n, len(data)):
        current = alpha * data[i] + (1 - alpha) * current
        out[i] = current
    return out


def ref_wma(values, n):
    data = filled(values)
    out = [NAN] * len(data)
    first = next((i for i, v in enumerate(data) if v is not NAN), None)
    if first is None:
        return out
    divisor = n * (n + 1) / 2.0
    for i in range(first + n - 1, len(data)):
        total = 0.0
        for k in range(n):
            total += (k + 1) * data[i - n + 1 + k]
        out[i] = total / divisor
    return out


def ref_dema(values, n):
    e1 = ref_ema(values, n)
    e2 = ref_ema(e1, n)
    return [NAN if a is NAN or b is NAN else 2 * a - b for a, b in zip(e1, e2)]


def ref_tema(values, n):
    e1 = ref_ema(values, n)
    e2 = ref_ema(e1, n)
    e3 = ref_ema(e2, n)
    return [NAN if NAN in (a, b, c) else 3 * a - 3 * b + c for a, b, c in zip(e1, e2, e3)]


def ref_hma(values, n):
    half, root = n // 2, int(round(math.sqrt(n)))
    a, b = ref_wma(values, half), ref_wma(values, n)
    diff = [NAN if x is NAN or y is NAN else 2 * x - y for x, y in zip(a, b)]
    return ref_wma(diff, root)


def ref_kama(values, n):
    data = filled(values)
    out = [NAN] * len(data)
    first = next((i for i, v in enumerate(data) if v is not NAN), None)
    if first is None or len(data) < first + n + 1:
        return out
    fastest, slowest = 2.0 / 3.0, 2.0 / 31.0
    current = data[first + n]
    out[first + n] = current
    for i in range(first + n + 1, len(data)):
        change = abs(data[i] - data[i - n])
        volatility = sum(abs(data[j] - data[j - 1]) for j in range(i - n + 1, i + 1))
        er = change / volatility if volatility > 0 else 0.0
        sc = (er * (fastest - slowest) + slowest) ** 2
        current = current + sc * (data[i] - current)
        out[i] = current
    return out


REFERENCE = dict(sma=ref_sma, ema=ref_ema, wma=ref_wma, dema=ref_dema, tema=ref_tema, hma=ref_hma, kama=ref_kama)


@pytest.mark.parametrize('kind', sorted(REFERENCE))
def test_average_matches_the_loop_reference(kind):
    values = series()
    for n in (3, 5, 9):
        got = ma_lab.MOVING_AVERAGES[kind](values, n)
        want = REFERENCE[kind](values, n)
        assert len(got) == len(want)
        for i, (a, b) in enumerate(zip(got, want)):
            if b is None:
                assert not np.isfinite(a), (kind, n, i)
            else:
                assert abs(a - b) < 1e-9, (kind, n, i, a, b)


def first_valid(array):
    finite = np.flatnonzero(np.isfinite(array))
    return int(finite[0]) if len(finite) else None


def test_first_valid_position_matches_the_table():
    values = list(np.linspace(1.0, 2.0, 400))
    n = 6
    assert first_valid(ma_lab.sma(values, n)) == n - 1
    assert first_valid(ma_lab.ema(values, n)) == n - 1
    assert first_valid(ma_lab.wma(values, n)) == n - 1
    assert first_valid(ma_lab.dema(values, n)) == 2 * n - 2
    assert first_valid(ma_lab.tema(values, n)) == 3 * n - 3
    assert first_valid(ma_lab.kama(values, n)) == n
    assert first_valid(ma_lab.hma(values, n)) == n - 1 + int(round(math.sqrt(n))) - 1


# -- simulation ---------------------------------------------------------------------------------------------------

def synthetic_market(n=60):
    from datetime import date, timedelta
    day = date(2020, 1, 1)
    days = [(day + timedelta(days=i)).isoformat() for i in range(n)]
    values = np.array([100.0 * (1.01 ** i) for i in range(n)])
    signal_days = [d for d in days if date.fromisoformat(d).weekday() == 4]
    return days, values, signal_days


def test_always_long_equals_buy_and_hold():
    days, values, signal_days = synthetic_market()
    rf = {}
    window, hold = ma_lab.simulate(days, values, signal_days, [True] * len(signal_days), 'zero', rf)
    _, rule = ma_lab.simulate(days, values, signal_days, [True] * len(signal_days), 'zero', rf)
    assert rule == hold
    assert rule[-1] == pytest.approx(values[-1] / values[days.index(signal_days[0])])


def test_always_flat_with_zero_cash_keeps_the_equity():
    days, values, signal_days = synthetic_market()
    _, rule = ma_lab.simulate(days, values, signal_days, [False] * len(signal_days), 'zero', {})
    assert all(value == pytest.approx(1.0) for value in rule)


def test_cash_interest_is_applied_when_flat():
    days, values, signal_days = synthetic_market(30)
    rf = {d: 0.001 for d in days}
    _, rule = ma_lab.simulate(days, values, signal_days, [False] * len(signal_days), 'dtb3', rf)
    assert rule[-1] == pytest.approx(1.001 ** (len(rule) - 1))


# -- metrics ------------------------------------------------------------------------------------------------------

def test_drawdown_dates_and_size():
    days = ['d1', 'd2', 'd3', 'd4', 'd5']
    equity = [100.0, 110.0, 99.0, 121.0, 121.0]
    out = ma_lab.drawdown(days, equity)
    assert out['max_drawdown'] == pytest.approx(-0.1)
    assert (out['peak_date'], out['trough_date'], out['recovery_date']) == ('d2', 'd3', 'd4')


def test_drawdown_without_recovery_leaves_the_date_empty():
    out = ma_lab.drawdown(['d1', 'd2', 'd3'], [100.0, 90.0, 95.0])
    assert out['max_drawdown'] == pytest.approx(-0.1)
    assert out['recovery_date'] == ''


def test_cagr_volatility_and_sharpe_match_the_hand_calculation():
    days = ['d1', 'd2', 'd3', 'd4', 'd5']
    equity = [100.0, 110.0, 99.0, 121.0, 121.0]
    out = ma_lab.statistics(days, equity, {d: 0.0 for d in days})
    rets = [110 / 100 - 1, 99 / 110 - 1, 121 / 99 - 1, 121 / 121 - 1]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    assert out['cagr'] == pytest.approx((121 / 100) ** (252 / 4) - 1)
    assert out['volatility'] == pytest.approx(math.sqrt(var) * math.sqrt(252))
    assert out['sharpe'] == pytest.approx(mean / math.sqrt(var) * math.sqrt(252))


def test_sharpe_subtracts_the_risk_free_rate():
    days = ['d1', 'd2', 'd3']
    equity = [100.0, 101.0, 102.0]
    rf = {'d2': 0.001, 'd3': 0.001}
    out = ma_lab.statistics(days, equity, rf)
    excess = [0.01 - 0.001, 102 / 101 - 1 - 0.001]
    mean = sum(excess) / 2
    var = sum((r - mean) ** 2 for r in excess) / 1
    assert out['sharpe'] == pytest.approx(mean / math.sqrt(var) * math.sqrt(252))


# -- errors -------------------------------------------------------------------------------------------------------

def test_end_after_the_cutoff_is_rejected():
    with pytest.raises(ValueError):
        ma_lab.run_lab('MSFT', start='2014-02-28', end='2024-07-05', types=['sma'], fasts=[50], slows=[200])


def test_start_before_the_first_stored_day_is_rejected():
    with pytest.raises(ValueError):
        ma_lab.run_lab('MSFT', start='2012-12-02', end='2024-06-28', types=['sma'], fasts=[50], slows=[200])


def test_ambiguous_ticker_lists_the_candidates(monkeypatch):
    import sqlite3
    conn=sqlite3.connect(':memory:'); conn.row_factory=sqlite3.Row
    conn.executescript("create table issuers(issuer_id text,name text); create table strategy_securities(security_id text,issuer_id text,current_ticker text,current_security_id text,strategy_eligible integer);")
    conn.executemany('insert into strategy_securities values (?,?,?,?,?)', [('A','I','DUP','A',1),('B','I','DUP','B',1)])
    monkeypatch.setattr(api,'connect',lambda *args:conn)
    with pytest.raises(ValueError,match='ambiguous ticker DUP'):
        ma_lab.resolve_security('DUP')
    conn.close()


def test_unknown_ticker_is_rejected():
    if not api.DB_PATH.exists():
        pytest.skip('released database is not present')
    with pytest.raises(ValueError):
        ma_lab.resolve_security('NOSUCHTICKER')


# -- real database ------------------------------------------------------------------------------------------------

def test_msft_sma_50_200_matches_the_trend_v1_per_stock_file():
    import csv
    from pathlib import Path
    if not api.DB_PATH.exists():
        pytest.skip('released database is not present')
    path = Path('research/trend_v1/per_stock.csv')
    if not path.exists():
        pytest.skip('trend_v1 per-stock file is not present')
    row = next(r for r in csv.DictReader(path.open(encoding='utf8'))
               if r['security_id'] == 'CUSIP:594918104' and r['signal'] == 'sma50_200')
    out = ma_lab.run_lab('MSFT', start='2014-02-28', end='2024-06-28', types=['sma'], fasts=[50], slows=[200],
                         cash_rate='zero', db_path=api.DB_PATH)
    rule, hold = out['rows'][0], out['buy_hold']
    assert abs(rule['cagr'] - float(row['cagr_rule'])) * 100 <= 0.01
    assert abs(rule['max_drawdown'] - float(row['dd_rule'])) * 100 <= 0.01
    assert abs(hold['cagr'] - float(row['cagr_hold'])) * 100 <= 0.01
    assert abs(hold['max_drawdown'] - float(row['dd_hold'])) * 100 <= 0.01


def test_missing_friday_is_carried_forward():
    """BIIB has no bar on Friday 2020-11-06; the lab carries the previous close instead of failing."""
    from leadtrend_bt.ma_lab import run_lab
    out = run_lab('BIIB', '2020-10-02', '2020-12-31', types=['sma'], fasts=[5], slows=[50],
                  db_path='data/db/leadtrend_db_v1.sqlite')
    assert out['rows'] and out['buy_hold']['cagr'] == out['buy_hold']['cagr']


def test_daily_frequency_checks_every_session():
    if not api.DB_PATH.exists():
        pytest.skip('released database is not present')
    out = ma_lab.run_lab('MSFT', start='2014-02-28', end='2024-06-28', types=['sma'], fasts=[20], slows=[100],
                         cash_rate='zero', db_path=api.DB_PATH, frequency='daily')
    weekly = ma_lab.run_lab('MSFT', start='2014-02-28', end='2024-06-28', types=['sma'], fasts=[20], slows=[100],
                            cash_rate='zero', db_path=api.DB_PATH)
    assert out['frequency'] == 'daily' and weekly['frequency'] == 'weekly'
    assert out['signal_days'] > 2500 and weekly['signal_days'] == 540
    assert out['rows'][0]['buys'] >= weekly['rows'][0]['buys']


def test_daily_state_turns_on_the_session_it_is_decided():
    days, values, _ = synthetic_market(4)
    signal_days = list(days[:3])
    _, curve = ma_lab.simulate(days, values, signal_days, [True, False, True], 'zero', {})
    assert curve[0] == pytest.approx(1.0)
    assert curve[1] == pytest.approx(values[1] / values[0])
    assert curve[2] == pytest.approx(curve[1])          # flat on the day the signal turned off
    assert curve[3] == pytest.approx(curve[2] * values[3] / values[2])   # long again from the next close
