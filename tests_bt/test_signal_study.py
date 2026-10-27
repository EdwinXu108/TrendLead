"""Signal-study tests: rank IC, point-in-time inputs and the reserved-segment guard on future windows."""
from __future__ import annotations

import pytest

from leadtrend_bt import signal_study as ss


def test_rank_ic_is_one_when_signal_and_future_return_are_identical():
    values = [-0.05, -0.01, 0.0, 0.02, 0.07, 0.11]
    assert ss.spearman(values, values) == pytest.approx(1.0)
    assert ss.spearman(values, [v * 3 + 1 for v in values]) == pytest.approx(1.0)
    assert ss.spearman(values, [-v for v in values]) == pytest.approx(-1.0)


def test_momentum_uses_only_the_signal_day_and_earlier():
    tri = {'2024-01-02': 10.0, '2024-01-03': 11.0, '2024-01-04': 12.0}
    before = ss.momentum(tri, '2024-01-03', '2024-01-02')
    tri['2024-01-04'] = 999.0                      # a later price must not change the signal of 01-03
    assert ss.momentum(tri, '2024-01-03', '2024-01-02') == before


def test_future_window_cannot_cross_the_reserved_segment():
    tri = {'2024-06-21': 100.0, '2024-06-28': 101.0, '2024-07-05': 102.0}
    assert ss.future_return(tri, '2024-06-21', '2024-06-28') == pytest.approx(0.01)
    with pytest.raises(ValueError):
        ss.future_return(tri, '2024-06-21', '2024-07-05')
    with pytest.raises(ValueError):
        ss.assert_future_end('2024-07-05')


def synthetic_panel(n=30):
    """Two signal days; MOM_5 is a monotone transform of the next-week return, so the IC must be 1."""
    calendar = [f'2024-01-{d:02d}' for d in range(1, 9)]
    days = [calendar[6], calendar[7]]
    rates = [-0.05 + 0.1 * i / (n - 1) for i in range(n)]
    universe = {d: {f'S{i:02d}': 1000 - i for i in range(n)} for d in days}
    tri = {}
    for i, r in enumerate(rates):
        sid = f'S{i:02d}'
        tri[sid] = {calendar[1]: 100.0 / (1 + r) ** 5,
                    calendar[2]: 100.0 / (1 + r) ** 4,
                    calendar[6]: 100.0 * (1 + r),
                    calendar[7]: 100.0 * (1 + r) ** 2}
    return dict(signal_days=days, calendar=calendar, universe=universe, tri=tri, lookback=calendar[1])


def test_synthetic_panel_has_ic_one_for_the_perfect_signal():
    out = ss.compute(synthetic_panel())
    assert out['ic']['MOM_5'][1]['mean'] == pytest.approx(1.0, abs=1e-12)
    assert out['quintile']['MOM_5'][1]['groups'][-1] > out['quintile']['MOM_5'][1]['groups'][0]
    assert out['stability']['MOM_5']['mean'] is not None


def test_a_future_window_beyond_the_last_signal_day_is_skipped():
    panel = synthetic_panel()
    out = ss.compute(panel)
    assert 4 not in out['ic']['MOM_5'] and 13 not in out['ic']['MOM_5']
