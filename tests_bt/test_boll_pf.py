"""The first portfolio: the high-beta half, TILT_B's concentrated state, and weights that add up to one."""
from __future__ import annotations

import numpy as np
import pytest

from leadtrend_bt import boll_pf


def test_high_beta_half_takes_only_betas_strictly_above_the_median():
    # 1, 2, 2, 3 -> median 2, so the two names at 2.0 are not in the half
    betas = {0: 1.0, 1: 2.0, 2: 3.0, 3: 2.0, 4: float('nan'), 5: None}
    assert boll_pf.high_beta_half(betas) == [2]
    # a missing beta never joins the half, and one known beta is not enough to split
    assert boll_pf.high_beta_half({0: 1.0, 1: float('nan'), 2: None}) == []
    assert boll_pf.high_beta_half({0: float('nan')}) == []
    # ten betas with an even count: the median falls between the fifth and sixth
    ten = {k: float(v) for k, v in enumerate([0.8, 0.85, 1.12, 1.15, 1.30, 1.35, 1.47, 1.54, 0.95, 0.84])}
    assert boll_pf.high_beta_half(ten) == [3, 4, 5, 6, 7]


def test_concentrated_state_starts_with_the_episode_and_runs_the_hold_weeks():
    # the state survives a short gap, ends once the hold window has passed outside a zone
    assert boll_pf.concentrated([0, 2, 2, 0, 0, 0, 0, 2, 0], window=3) == \
        [False, True, True, True, False, False, False, True, True]
    # a new episode inside the tail restarts the clock
    assert boll_pf.concentrated([0, 2, 0, 0, 2, 0, 0, 0], window=3) == \
        [False, True, True, True, True, True, True, False]
    # a zone that never ends keeps the state on
    assert boll_pf.concentrated([2, 2, 2, 2, 2], window=1) == [True] * 5


def _fake_book():
    days = ['2020-01-03', '2020-01-10', '2020-01-17']

    class Market:
        pass

    market = Market()
    market.sids = [f'S{i}' for i in range(6)]
    market.index = {day: i for i, day in enumerate(days)}
    market.pools = {days[0]: np.array([0, 1, 2, 3]), days[1]: np.array([1, 2, 3, 4]),
                    days[2]: np.array([0, 2, 4, 5])}
    top = {days[0]: [(0, 100.0, 1), (1, 50.0, 2), (2, 50.0, 3)],
           days[1]: [(1, 30.0, 1), (2, 30.0, 2), (3, 40.0, 3)],
           days[2]: [(0, 10.0, 1), (2, 10.0, 2), (4, 10.0, 3)]}
    betas = {days[0]: {0: 1.0, 1: 2.0, 2: 3.0},
             days[1]: {1: 1.0, 2: float('nan'), 3: 2.0},
             days[2]: {0: float('nan'), 2: float('nan'), 4: 1.0}}
    return dict(market=market, weeks=days, top=top, betas=betas, levels=[2, 0, 2],
                concentrated=[True, False, True])


def test_every_line_targets_add_up_to_one():
    book = _fake_book()
    plans, applied = boll_pf.targets(book)
    assert set(plans) == {'EW50', 'EW10', 'CW10', 'TILT_A', 'TILT_B'}
    for name, weeks in plans.items():
        for i, weights in weeks.items():
            assert weights.shape == (6,)
            assert weights.sum() == pytest.approx(1.0, abs=1e-12), f'{name} on session {i}'
            assert np.all(weights >= 0)
    # the tilt is the single highest-beta name on the first week, and the same ten otherwise
    first, second, third = (book['market'].index[day] for day in book['weeks'])
    assert np.flatnonzero(plans['TILT_A'][first]).tolist() == [2]
    assert np.flatnonzero(plans['TILT_A'][second]).tolist() == [1, 2, 3]
    assert np.flatnonzero(plans['TILT_A'][third]).tolist() == [0, 2, 4]   # only one beta: back to the ten
    assert applied['TILT_A'][first] and not applied['TILT_A'][second] and not applied['TILT_A'][third]
    # CW10 follows the company amounts of the ten
    assert plans['CW10'][first].tolist() == pytest.approx([0.5, 0.25, 0.25, 0, 0, 0])
