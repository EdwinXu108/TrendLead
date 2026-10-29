"""The pick grid: the three measures against numpy, and the ordering rules on hand-made weeks."""
from __future__ import annotations

import numpy as np
import pytest

from leadtrend_bt import pick


def test_measures_match_numpy_and_go_empty_on_gaps():
    bench = np.array([0.01, -0.02, 0.03, 0.00, -0.01, 0.02])
    stock = np.array([[0.02, 0.01], [-0.01, -0.03], [0.05, 0.04], [0.01, 0.00], [-0.02, -0.02], [0.03, 0.01]])
    returns = np.vstack([np.zeros((1, 2)), stock])          # week 0 has no return
    bench_returns = np.vstack([np.zeros(1), bench.reshape(-1, 1)]).ravel()
    beta, corr, vol = pick.measures(returns, bench_returns, 6, 6)
    x, y = bench, stock[:, 0]
    assert beta[0] == pytest.approx(np.cov(y, x, ddof=0)[0, 1] / np.var(x), abs=1e-12)
    assert corr[0] == pytest.approx(np.corrcoef(y, x)[0, 1], abs=1e-12)
    assert vol[0] == pytest.approx(np.std(y, ddof=0), abs=1e-12)
    assert vol[1] == pytest.approx(np.std(stock[:, 1], ddof=0), abs=1e-12)
    # a gap inside the window empties that stock only
    returns[3, 1] = np.nan
    beta, corr, vol = pick.measures(returns, bench_returns, 6, 6)
    assert np.isfinite(beta[0]) and np.isnan(beta[1])
    assert np.isnan(corr[1]) and np.isnan(vol[1])
    # a window that reaches before the first return is empty everywhere
    beta, corr, vol = pick.measures(returns, bench_returns, 2, 6)
    assert np.all(np.isnan(beta)) and np.all(np.isnan(vol))


def _book(betas):
    size = len(betas)
    ranks = {column: column + 1 for column in range(size)}
    return dict(
        weeks=['2020-01-03'], levels=[0], cands=[list(ranks.items())],
        columns=[list(range(size))], ranks=[ranks],
        measure={w: {key: np.asarray(betas, dtype=float).reshape(1, -1) for key in ('beta', 'corr', 'vol')}
                 for w in pick.WS},
        forward={h: np.full((1, size), 0.01 * (np.arange(size) + 1)) for h in pick.HS},
        voo_forward={h: np.zeros(1) for h in pick.HS})


def test_ordering_ties_gaps_wb_and_short_weeks():
    book = _book([0.5, 2.0, 1.0, 2.0, float('nan'), 0.2])
    # highest beta first, ties broken by the smaller weight rank, the gap left out
    assert pick.order(book, 'BETA_HI', 52, 50)[0] == [1, 3, 2, 0, 5]
    assert pick.order(book, 'BETA_LO', 52, 50)[0] == [5, 0, 2, 1, 3]
    # K keeps only the first four ranks, and WGT ignores K entirely
    assert pick.order(book, 'BETA_HI', 52, 4)[0] == [1, 3, 2, 0]
    assert pick.order(book, 'WGT', None, 2)[0] == [0, 1, 2, 3, 4, 5]
    # WB: weight rank + beta rank (highest beta is 1; two names at 2.0 share rank 1), ties by weight rank
    assert pick.order(book, 'WB', 52, 50)[0] == [1, 0, 3, 2, 5]

    table = pick.baskets(book)
    assert table['BETA_HI_K50_N5_W52']['valid'][0] is np.True_
    assert not table['BETA_HI_K50_N10_W52']['valid'][0]         # only five betas known
    assert table['WGT_N5']['valid'][0]                          # weight needs no measure
    assert not table['WGT_N10']['valid'][0]                     # only six candidates
    # the basket is the equal-weighted mean of the chosen names' forward returns
    chosen = [1, 3, 2, 0, 5]
    expected = np.mean([0.01 * (column + 1) for column in chosen])
    assert table['BETA_HI_K50_N5_W52'][13][0] == pytest.approx(expected, abs=1e-12)
