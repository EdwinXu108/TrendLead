"""The weekly Bollinger mid line: weights, the prediction condition, and no look-ahead.

Also the monthly and quarterly zone lines: the width-0 boundary and the same no-look-ahead property.
"""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import numpy as np
import pytest

from leadtrend_bt import boll_weekly

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('boll_dashboard', ROOT / 'research' / 'boll_v1' / 'dashboard.py')
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


def test_weights_are_symmetric_and_sum_to_one():
    w = boll_weekly.weights(20)
    raw = np.sin(np.pi * np.arange(1, 21) / 21) ** 2
    assert w.size == 20
    assert w.sum() == pytest.approx(1.0, abs=1e-12)
    assert np.allclose(w, raw / raw.sum(), atol=1e-15)
    assert np.allclose(w, w[::-1], atol=1e-15)
    assert np.all(w > 0) and np.argmax(w) == 9


def test_prediction_condition_on_a_hand_computed_series():
    # a four-week window, so every mid value is a short explicit sum of the four weights
    w = [0.1381966, 0.3618034, 0.3618034, 0.1381966]
    closes = [10, 9, 8, 7, 6, 5, 5, 5, 5, 5]
    expected = [10 * w[0] + 9 * w[1] + 8 * w[2] + 7 * w[3],
                9 * w[0] + 8 * w[1] + 7 * w[2] + 6 * w[3],
                8 * w[0] + 7 * w[1] + 6 * w[2] + 5 * w[3],
                7 * w[0] + 6 * w[1] + 5 * w[2] + 5 * w[3],
                6 * w[0] + 5 * w[1] + 5 * w[2] + 5 * w[3],
                5.0, 5.0]
    mid = boll_weekly.hann_mid(closes, n=4)
    assert np.isnan(mid[:3]).all()
    assert mid[3:].tolist() == pytest.approx(expected, abs=1e-6)
    s, a = boll_weekly.derivatives(mid)
    assert s[4:].tolist() == pytest.approx([-1.0, -1.0] + [expected[i] - expected[i - 1] for i in range(3, 7)],
                                           abs=1e-6)
    # the last falling week is already decelerating enough to flatten one week later
    assert s[7] < 0 and s[7] + a[7] < 0
    # the only week still falling but flattening within one more week
    assert np.flatnonzero(boll_weekly.is_prediction(s, a)).tolist() == [8]
    weeks = [f'2020-01-{d:02d}' for d in range(1, 11)]
    rows = boll_weekly.details(weeks, closes, s, a, '2019-01-01', '2029-12-31')
    assert rows == [dict(date='2020-01-09', close=5.0, recover_date='2020-01-10', gap_weeks=1,
                         max_drop=0.0, recover_gain=0.0)]


def test_appending_weeks_does_not_change_earlier_values():
    rng = np.random.default_rng(7)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, 60)))
    head = closes[:40]
    mid_head = boll_weekly.hann_mid(head)
    mid_full = boll_weekly.hann_mid(closes)[:40]
    assert np.allclose(mid_head[19:], mid_full[19:], atol=1e-12)
    s_head, a_head = boll_weekly.derivatives(mid_head)
    s_full, a_full = boll_weekly.derivatives(mid_full)
    assert boll_weekly.is_prediction(s_head, a_head).tolist() == \
        boll_weekly.is_prediction(s_full, a_full).tolist()


def _serve(db, paths: dict):
    """Start the dashboard once and fetch several paths; returns {name: (status, body)}."""
    dashboard.STORE = dashboard.Store(str(db))
    from http.server import HTTPServer
    server = HTTPServer(('127.0.0.1', 0), dashboard.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import json
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        out = {}
        for name, path in paths.items():
            with opener.open(f'http://127.0.0.1:{port}{path}', timeout=120) as response:
                out[name] = (response.status, json.loads(response.read().decode('utf8')))
        return out
    finally:
        server.shutdown()
        server.server_close()


def test_api_returns_meta_and_both_quarter_lengths():
    from leadtrend_db import api
    if not Path(api.DB_V2_PATH).exists():
        pytest.skip('scope database is not present')
    results = _serve(api.DB_V2_PATH, {
        'default': '/api/boll?security=META',
        'q20': '/api/boll?security=META&quarter=20&d=0.5',
        'q8': '/api/boll?security=META&quarter=8&d=0.5'})
    status, data = results['default']
    assert status == 200
    assert data['meta']['ticker'] == 'META' and data['meta']['weeks'] == len(data['weeks'])
    assert len(data['close']) == len(data['mid']) == len(data['s_pct']) == len(data['weeks'])
    assert [p['date'] for p in data['points']] == ['2014-07-03', '2017-02-10', '2018-06-08', '2019-02-15',
                                                   '2019-11-22', '2020-05-29', '2021-04-01', '2023-01-27']
    assert [m['date'] for m in data['marks']] == [p['date'] for p in data['points']]
    assert (data['params']['quarter_n'], data['params']['width']) == (20, 0.5)
    # the four zones over the 540-week development window, checked against the reference table
    for name, expected in (('q20', {'q_lower': 6, 'm_lower': 52, 'q_mid': 75, 'm_mid': 119}),
                           ('q8', {'q_lower': 39, 'm_lower': 52, 'q_mid': 112, 'm_mid': 119})):
        assert results[name][0] == 200
        zones = results[name][1]['zone_summary']
        assert {key: item['weeks'] for key, item in zones.items()} == expected
        assert results[name][1]['params']['quarter_n'] == int(name[1:])


def _synthetic_months(n_months: int = 72):
    """Month-end closes with a slow cycle plus drift, and every Friday inside the same span."""
    import calendar
    from datetime import date, timedelta
    days, values = [], []
    for k in range(n_months):
        year, month = 2014 + k // 12, k % 12 + 1
        days.append(f'{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}')
        values.append(100 + 8 * np.sin(k / 2.5) + 0.6 * k)
    start, weeks = date(2014, 1, 3), []
    while (start + timedelta(days=7 * len(weeks))).isoformat() <= days[-1]:
        weeks.append((start + timedelta(days=7 * len(weeks))).isoformat())
    return days, values, weeks


def test_width_zero_counts_only_at_or_below_the_line():
    line = np.array([10.0, 10.0, 10.0, np.nan])
    sd = np.array([1.0, 1.0, 1.0, 1.0])
    close = np.array([10.0, 10.001, 9.999, 5.0])
    assert boll_weekly.in_zone(close, line, sd, 0.0).tolist() == [True, False, True, False]
    assert boll_weekly.in_zone(close, line, sd, 0.5).tolist() == [True, True, True, False]
    assert boll_weekly.in_zone(close, line, sd, 1.0).tolist() == [True, True, True, False]
    assert boll_weekly.in_zone(close, line, sd, 0.0).tolist() == [True, False, True, False]


def test_appending_months_does_not_change_earlier_lines_or_zones():
    days, values, weeks = _synthetic_months()
    cut = next(i for i, day in enumerate(days) if day > '2018-12-31')
    head_weeks = [week for week in weeks if week <= '2018-12-31']
    month = dict(days=days, **boll_weekly.band_stats(values, 20))
    quarter = dict(zip(('days', 'values'),
                       boll_weekly.period_closes(days, values, 'quarter')))
    quarter.update(boll_weekly.band_stats(quarter.pop('values'), 8))
    head_month = dict(days=days[:cut], **boll_weekly.band_stats(values[:cut], 20))
    head_quarter = dict(zip(('days', 'values'),
                            boll_weekly.period_closes(days[:cut], values[:cut], 'quarter')))
    head_quarter.update(boll_weekly.band_stats(head_quarter.pop('values'), 8))
    close = np.asarray([values[days.index(week)] if week in days else values[
        next(i for i, day in enumerate(days) if day >= week)] for week in head_weeks])
    for key in ('mid', 'lower'):
        assert np.allclose(boll_weekly.line_for(head_weeks, head_month['days'], head_month)[key],
                           boll_weekly.line_for(head_weeks, month['days'], month)[key], atol=1e-12, equal_nan=True)
        assert np.allclose(boll_weekly.line_for(head_weeks, head_quarter['days'], head_quarter)[key],
                           boll_weekly.line_for(head_weeks, quarter['days'], quarter)[key], atol=1e-12, equal_nan=True)
    full = boll_weekly.zone_flags(head_weeks, close, month, quarter, 0.5)
    head = boll_weekly.zone_flags(head_weeks, close, head_month, head_quarter, 0.5)
    for key in full:
        assert head[key].tolist() == full[key].tolist()
