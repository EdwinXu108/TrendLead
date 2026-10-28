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
