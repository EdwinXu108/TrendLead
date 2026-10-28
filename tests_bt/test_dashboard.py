"""The dashboard's own helpers: year splitting, JSON cleaning, and the reserved-segment guard."""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('ma_dashboard', ROOT / 'research' / 'ma_lab_v1' / 'dashboard.py')
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


def test_annual_returns_split_by_calendar_year():
    days = ['2020-01-02', '2020-06-30', '2021-01-04', '2021-12-31']
    equity = [1.0, 1.1, 1.2, 1.32]
    out = dashboard.annual_returns(days, equity)
    assert [row['year'] for row in out] == ['2020', '2021']
    assert out[0]['value'] == pytest.approx(0.1)
    assert out[1]['value'] == pytest.approx(0.2)
    assert not out[0]['partial'] and not out[1]['partial']


def test_a_year_that_starts_late_or_ends_early_is_marked_partial():
    days = ['2020-03-02', '2020-12-31', '2021-11-30']
    out = dashboard.annual_returns(days, [1.0, 1.1, 1.2])
    assert out[0]['partial'] is True          # starts in March
    assert out[1]['partial'] is True          # ends in November


def test_clean_replaces_non_finite_numbers_with_null():
    assert dashboard.clean({'a': float('nan'), 'b': [1.5, float('inf')], 'c': True, 'd': 'x'}) == {
        'a': None, 'b': [1.5, None], 'c': True, 'd': 'x'}


def test_lab_refuses_the_reserved_segment():
    from leadtrend_db import api
    if not api.DB_PATH.exists():
        pytest.skip('released database is not present')
    with pytest.raises(ValueError):
        dashboard.Lab('MSFT', '2014-02-28', '2024-07-05', 'zero', str(api.DB_PATH))


def test_win_rate_counts_each_long_run_as_one_trade():
    import numpy as np
    days = ['d1', 'd2', 'd3', 'd4', 'd5']
    values = np.array([1.0, 1.1, 1.21, 1.15, 1.2])
    state = [True, False, True, False]
    segments = dashboard.winrate.segment_returns(days, values, days[:4], state)
    assert len(segments) == 2
    assert segments[0]['ret'] == pytest.approx(0.1)
    assert segments[1]['ret'] == pytest.approx(1.15 / 1.21 - 1)
    stats = dashboard.winrate.stats(segments)
    assert (stats['segments'], stats['wins']) == (2, 1)
    assert stats['win_rate'] == pytest.approx(0.5)
    assert stats['profit_factor'] == pytest.approx(0.1 / abs(1.15 / 1.21 - 1))


def test_an_open_run_is_marked_to_the_last_session():
    import numpy as np
    days = ['d1', 'd2', 'd3', 'd4']
    values = np.array([1.0, 1.05, 1.1, 1.2])
    segments = dashboard.winrate.segment_returns(days, values, days[:2], [True, True])
    assert len(segments) == 1
    assert segments[0]['open_end'] is True
    assert segments[0]['ret'] == pytest.approx(0.2)
    assert segments[0]['exit'] == 'd4'


def test_win_rate_is_none_without_trades():
    stats = dashboard.winrate.stats([])
    assert stats['segments'] == 0 and stats['win_rate'] is None and stats['profit_factor'] is None


def test_fast_rows_match_run_lab_for_every_combination():
    """The dashboard's fast path must reproduce ma_lab.run_lab field by field."""
    import math
    from leadtrend_bt import ma_lab
    from leadtrend_db import api
    if not api.DB_PATH.exists():
        pytest.skip('released database is not present')
    lab = dashboard.Lab('MSFT', '2014-02-28', '2024-06-28', 'zero', str(api.DB_PATH), 'daily')
    fast = lab.rows_with_winrate()
    slow = ma_lab.run_lab('MSFT', start='2014-02-28', end='2024-06-28', types=dashboard.TYPES,
                          fasts=dashboard.FASTS, slows=dashboard.SLOWS, cash_rate='zero',
                          db_path=api.DB_PATH, frequency='daily')['rows']
    assert len(fast) == len(slow) == 462
    numeric = ['cagr', 'total_return', 'volatility', 'max_drawdown', 'cagr_over_dd', 'sharpe',
               'invested_share', 'buys', 'average_holding_weeks']
    for a, b in zip(slow, fast):
        for key in numeric:
            left, right = a[key], b[key]
            if isinstance(left, float) and math.isnan(left):
                assert isinstance(right, float) and math.isnan(right), (a['type'], key)
            else:
                assert abs(left - right) <= 1e-9, (a['type'], a['fast'], a['slow'], key, left, right)
        for key in ('peak_date', 'trough_date', 'recovery_date', 'first_valid_signal_day', 'warmup_short'):
            assert a[key] == b[key], (a['type'], a['fast'], a['slow'], key, a[key], b[key])


def _serve(db):
    import threading
    from http.server import HTTPServer
    dashboard.STORE = dashboard.Store(str(db))
    server = HTTPServer(('127.0.0.1', 0), dashboard.Handler)
    port = server.server_address[1]
    results = {}

    def client():
        import json
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def get(path):
            try:
                with opener.open(f'http://127.0.0.1:{port}{path}', timeout=180) as response:
                    return response.status, json.loads(response.read().decode('utf8'))
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read().decode('utf8'))

        try:
            results['pool'] = get('/api/pool_ma')
            results['wf'] = get('/api/wf?security=MSFT&start=2014-02-28&end=2024-06-28&cash=zero&freq=daily')
            results['wfboard'] = get('/api/wfboard?freq=daily&cash=zero')
            results['wf2board'] = get('/api/wf2board?segment=dev')
        finally:
            server.shutdown()

    thread = threading.Thread(target=client, daemon=True)
    thread.start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        thread.join(timeout=10)
    return results


def test_the_pool_endpoint_and_the_removed_wf_v1_endpoints():
    from leadtrend_db import api
    if not api.DB_PATH.exists():
        pytest.skip('released database is not present')
    cache = ROOT / 'work' / 'pool_ma_v1' / 'curves.json'
    if not cache.exists():
        pytest.skip('the pool experiment has not been run')
    results = _serve(api.DB_PATH)
    status, pool = results['pool']
    assert status == 200 and {'summary', 'curves'} <= set(pool)
    assert pool['curves']['dates'] and pool['curves']['weekly']
    assert pool['summary']['random']['beats']['cagr'] <= pool['summary']['random']['seeds']
    assert results['wf'][0] == 404 and results['wfboard'][0] == 404
    status, board = results['wf2board']
    assert status == 200 and board['stocks'] and len(board['late_entrants']) == 39
    first = board['stocks'][0]
    assert {'ticker', 'lines', 'diffs'} <= set(first)
