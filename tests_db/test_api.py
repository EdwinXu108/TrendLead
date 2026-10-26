"""Tests of the read-only query interface against the built database (skipped when it is absent)."""
from __future__ import annotations

from datetime import date

import pytest

from leadtrend_db import api
from leadtrend_db.sources import DB_PATH

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason='database not built')


def test_trading_days_are_sessions_of_the_stored_calendar():
    days = api.trading_days('2020-03-02', '2020-03-13')
    assert days[0] == '2020-03-02' and days[-1] == '2020-03-13'
    assert '2020-03-08' not in days          # a Sunday
    assert len(days) == 10


def test_friday_signal_days_are_the_last_session_of_each_week():
    days = api.friday_signal_days('2020-03-02', '2020-03-31')
    assert days == ['2020-03-06', '2020-03-13', '2020-03-20', '2020-03-27', '2020-03-31']
    for day in days[:-1]:
        assert date.fromisoformat(day).weekday() == 4


def test_pool_at_returns_eligible_securities_of_one_version():
    pool = api.pool_at('2020-06-15')
    assert pool['version_id']
    assert pool['securities'], 'a version effective in 2020 must have eligible securities'
    assert all(s['security_id'] for s in pool['securities'])
    assert pool['effective_from'] <= '2020-06-15' <= pool['effective_to']
    assert {s['issuer_id'] for s in pool['securities']} <= {c['issuer_id'] for c in pool['companies']}


def test_pool_at_before_the_first_version_is_empty():
    pool = api.pool_at('2013-12-01')
    assert pool['securities'] == [] and pool['version_id'] == ''


def test_tri_series_is_positive_and_starts_at_one():
    pool = api.pool_at('2020-06-15')
    sid = pool['securities'][0]['security_id']
    series = api.tri_series([sid], '2019-01-01', '2024-06-28')[sid]
    assert series, 'a pool security must have a total-return series'
    assert series[0][1] == pytest.approx(1.0)
    assert all(row[1] > 0 for row in series)
    for prev, cur in zip(series, series[1:]):
        assert cur[1] == pytest.approx(prev[1] * (1 + cur[2]), rel=1e-12)


def test_benchmark_series_exists_and_covers_the_backtest_window():
    series = api.tri_series(['BENCH:VOO'], '2014-02-28', '2024-06-28')['BENCH:VOO']
    assert len(series) > 2500
    assert series[0][0] >= '2014-02-28'


def test_open_prices_match_stored_rows():
    pool = api.pool_at('2020-06-15')
    sid = pool['securities'][0]['security_id']
    prices = api.open_prices([sid], '2020-06-15')
    assert sid in prices and prices[sid] > 0


def test_rf_daily_is_a_daily_rate_of_the_bill_yield():
    rates = api.rf_daily('2020-06-01', '2020-06-30')
    assert rates
    assert all(0 < value < 0.001 for value in rates.values())
    assert abs(rates['2020-06-15'] - 0.16 / 100 / 360) < 0.0002     # June 2020 bill yield was about 0.16%
