"""The weekly Bollinger mid line: weights, the prediction condition, and no look-ahead."""
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


def _serve(db):
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
        with opener.open(f'http://127.0.0.1:{port}/api/boll?security=META', timeout=120) as response:
            return response.status, json.loads(response.read().decode('utf8'))
    finally:
        server.shutdown()
        server.server_close()


def test_api_returns_meta():
    from leadtrend_db import api
    if not Path(api.DB_V2_PATH).exists():
        pytest.skip('scope database is not present')
    status, data = _serve(api.DB_V2_PATH)
    assert status == 200
    assert data['meta']['ticker'] == 'META' and data['meta']['weeks'] == len(data['weeks'])
    assert len(data['close']) == len(data['mid']) == len(data['s_pct']) == len(data['weeks'])
    assert [p['date'] for p in data['points']] == ['2014-07-03', '2017-02-10', '2018-06-08', '2019-02-15',
                                                   '2019-11-22', '2020-05-29', '2021-04-01', '2023-01-27']
    assert [m['date'] for m in data['marks']] == [p['date'] for p in data['points']]
