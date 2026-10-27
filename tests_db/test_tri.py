"""Unit tests of the total-return index (method event_v1) on hand-computed series."""
from __future__ import annotations

import pytest

from leadtrend_db import tri

CAL = ['2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05', '2024-01-08']


def bars(values):
    return {day: dict(open=value, close=value) for day, value in zip(CAL, values)}


def test_plain_series_compounds_close_returns():
    rows = tri.build_series(CAL, bars([100.0, 110.0, 99.0, 99.0, 108.9]), [], CAL, CAL[0], CAL[-1])
    assert [r['tri'] for r in rows] == pytest.approx([1.0, 1.1, 0.99, 0.99, 1.089])
    assert rows[0]['daily_return'] is None
    assert rows[1]['daily_return'] == pytest.approx(0.10)
    assert rows[2]['daily_return'] == pytest.approx(-0.10)


def test_two_for_one_split_has_zero_return():
    action = dict(security_id='S', action_date='2024-01-04', action_type='split', ratio=2.0, cash_amount=None,
                  spin_value=None, vendor_factor=None)
    rows = tri.build_series(CAL, bars([100.0, 100.0, 50.0, 50.0, 55.0]), [action], CAL, CAL[0], CAL[-1])
    assert rows[2]['daily_return'] == pytest.approx(0.0)
    assert rows[4]['tri'] == pytest.approx(1.1)


def test_cash_dividend_is_added_on_the_ex_date():
    action = dict(security_id='S', action_date='2024-01-03', action_type='cash_dividend', ratio=None,
                  cash_amount=2.0, spin_value=None, vendor_factor=None)
    rows = tri.build_series(CAL, bars([100.0, 99.0, 99.0, 99.0, 99.0]), [action], CAL, CAL[0], CAL[-1])
    assert rows[1]['daily_return'] == pytest.approx((99.0 + 2.0) / 100.0 - 1)
    assert 'cash' in rows[1]['flags']


def test_spinoff_uses_documented_value_first():
    action = dict(security_id='S', action_date='2024-01-03', action_type='spinoff', ratio=0.5, cash_amount=None,
                  spin_value=10.0, vendor_factor=0.8)
    rows = tri.build_series(CAL, bars([100.0, 92.0, 92.0, 92.0, 92.0]), [action], CAL, CAL[0], CAL[-1])
    assert rows[1]['daily_return'] == pytest.approx((92.0 + 10.0) / 100.0 - 1)
    assert rows[1]['flags'] == 'spinoff_child'


def test_spinoff_falls_back_to_vendor_factor():
    action = dict(security_id='S', action_date='2024-01-03', action_type='spinoff', ratio=None, cash_amount=None,
                  spin_value=None, vendor_factor=0.8)
    rows = tri.build_series(CAL, bars([100.0, 80.0, 80.0, 80.0, 80.0]), [action], CAL, CAL[0], CAL[-1])
    assert rows[1]['daily_return'] == pytest.approx(0.0)
    assert rows[1]['flags'] == 'vendor_factor'


def test_spinoff_falls_back_to_opening_drop():
    series = bars([100.0, 80.0, 80.0, 80.0, 80.0])
    series['2024-01-03']['open'] = 70.0
    action = dict(security_id='S', action_date='2024-01-03', action_type='spinoff', ratio=None, cash_amount=None,
                  spin_value=None, vendor_factor=None)
    rows = tri.build_series(CAL, series, [action], CAL, CAL[0], CAL[-1])
    assert rows[1]['daily_return'] == pytest.approx((80.0 + 30.0) / 100.0 - 1)
    assert rows[1]['flags'] == 'price_drop_implied'


def test_event_on_a_non_session_day_is_applied_on_the_next_session():
    action = dict(security_id='S', action_date='2024-01-06', action_type='split', ratio=2.0, cash_amount=None,
                  spin_value=None, vendor_factor=None)
    rows = tri.build_series(CAL, bars([100.0, 100.0, 100.0, 100.0, 50.0]), [action], CAL, CAL[0], CAL[-1])
    assert rows[4]['daily_return'] == pytest.approx(0.0)
    assert 'split' in rows[4]['flags']


def test_gap_flag_when_the_previous_session_is_missing():
    cal = CAL + ['2024-01-09']
    series = {d: dict(open=v, close=v) for d, v in zip(cal, [100.0, 101.0, 102.0, 103.0, 104.0, 105.0])}
    del series['2024-01-05']
    rows = tri.build_series(sorted(series), series, [], cal, cal[0], cal[-1])
    gap_row = rows[-2]                      # 2024-01-08 follows the missing 2024-01-05
    assert gap_row['session_date'] == '2024-01-08'
    assert 'gap' in gap_row['flags']
    assert gap_row['daily_return'] == pytest.approx(104.0 / 102.0 - 1)
    assert 'gap' not in rows[-1]['flags']


def test_tri_starts_at_one_on_the_first_session_in_range():
    rows = tri.build_series(CAL, bars([50.0, 51.0, 52.0, 53.0, 54.0]), [], CAL, '2024-01-04', CAL[-1])
    assert rows[0]['session_date'] == '2024-01-04'
    assert rows[0]['tri'] == 1.0
    assert rows[0]['daily_return'] is None
    assert rows[-1]['tri'] == pytest.approx(54.0 / 52.0)


def test_class_distribution_uses_child_value_not_a_parent_share_split():
    action = dict(action_date=CAL[1], action_type='class_distribution', ratio=1.0, spin_value=20.0)
    result = tri.build_series(CAL, bars([100.0, 80.0, 80.0, 80.0, 80.0]), [action], CAL, CAL[0], CAL[-1])
    assert result[1]['daily_return'] == pytest.approx(0)
    action['spin_value'] = None
    with pytest.raises(ValueError, match='verified child valuation'):
        tri.build_series(CAL, bars([100.0, 80.0, 80.0, 80.0, 80.0]), [action], CAL, CAL[0], CAL[-1])
