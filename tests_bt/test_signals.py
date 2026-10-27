"""Signal helpers and one end-to-end check on the released database."""
from __future__ import annotations

import numpy as np
import pytest

from leadtrend_bt import signals


def test_ratio_is_total_return():
    numer = np.array([[2.0, 1.5]])
    denom = np.array([[1.0, 1.0]])
    out = signals._ratio(numer, denom)
    assert out[0, 0] == pytest.approx(1.0)
    assert out[0, 1] == pytest.approx(0.5)


def test_ratio_marks_missing_as_nan():
    out = signals._ratio(np.array([[np.nan]]), np.array([[1.0]]))
    assert np.isnan(out[0, 0])


def test_rolling_std_of_a_constant_series_is_zero():
    rets = np.zeros((30, 2))
    ci = np.array([20])
    out = signals._rolling_std(rets, ci, 10)
    assert out[0, 0] == pytest.approx(0.0, abs=1e-12)


def test_rolling_mean_ignores_missing():
    matrix = np.array([[1.0, np.nan], [2.0, 4.0], [3.0, 6.0]])
    out = signals._rolling_mean(matrix, np.array([2]), 2)
    assert out[0, 0] == pytest.approx(2.5)
    assert out[0, 1] == pytest.approx(5.0)


def test_momentum_signal_on_the_released_database():
    from leadtrend_db import api
    if not api.DB_PATH.exists():
        pytest.skip('released database is not present')
    scores = signals.scores('MOM_126', api.DB_PATH)
    assert len(scores) == 540
    day = '2016-06-03'
    assert scores[day]
    values = list(scores[day].values())
    assert all(-1.0 < v < 20.0 for v in values)
