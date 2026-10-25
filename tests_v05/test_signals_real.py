"""Signal code on the real development data: lagged momentum cells and rank-mean composites, checked independently.

Verification only: no backtest is run and no return is read.
"""
import random
from pathlib import Path

import pytest

from leadtrend_v05.perf_cache import build_context
from leadtrend_v05.signals import composite_cells, momentum_cell

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module')
def real():
    if not (ROOT / 'research/v05/registry/stage4_registry.json').exists():
        pytest.skip('real development data not present')
    ctx = build_context(ROOT, 'uniform')
    return ctx.prepared[-1], ctx.frames


def test_lagged_momentum_cells_equal_the_engine_at_the_lagged_date(real):
    engine, frames = real
    checked = 0
    for d in sorted(frames):
        for sid in sorted(frames[d]['chosen']):
            for L in (60, 90):
                for skip in (5, 21):
                    cell = momentum_cell(engine, sid, d, L, skip)
                    lagged = engine.pos[d] - skip
                    if lagged < 0:  # the lagged date does not exist: nothing can be scored
                        assert cell[0] is None
                        continue
                    end = engine.sessions[lagged]
                    if cell[0] is not None:
                        assert cell[0] == engine.score(sid, end, L)[0], (sid, d, L, skip)
                    else:
                        assert engine.availability(sid, d, L + skip) or engine.score(sid, end, L)[0] is None, (sid, d, L, skip)
                    checked += 1
    assert checked > 0


def pandas_percentiles(pd, scores):
    """Percentile of each security among those scored: best 1.0, worst 0.0, equal scores ranked by security id."""
    s = pd.Series(scores, dtype='float64').sort_index()
    n = len(s)
    rank = s.rank(method='first', ascending=False)
    return {k: ((n - rank[k]) / (n - 1) if n > 1 else 1.0) for k in s.index}


def test_composite_percentiles_and_scores_equal_an_independent_pandas_ranking(real):
    pd = pytest.importorskip('pandas')
    engine, frames = real
    lengths = (20, 60, 126)
    for d in random.Random(0).sample(sorted(frames), 5):
        sids = sorted(frames[d]['chosen'])
        cells = [{s: momentum_cell(engine, s, d, L, 0) for s in sids} for L in lengths]
        scorable = [s for s in sids if all(cell[s][0] is not None for cell in cells)]
        expected = {s: [] for s in scorable}
        for L, cell in zip(lengths, cells):
            # One component: each security's percentile over its own scorable set.
            own = {s: cell[s][0] for s in sids if cell[s][0] is not None}
            single = composite_cells(sids, [cell], [1.0])
            for s, pct in pandas_percentiles(pd, own).items():
                assert single[s][0] == pct, (d, L, s)
            # Inside the composite: percentiles over the securities scorable on every component.
            pct = pandas_percentiles(pd, {s: cell[s][0] for s in scorable})
            for s in scorable:
                expected[s].append(pct[s])
        combined = composite_cells(sids, cells, [1.0, 1.0, 1.0])
        for s in sids:
            if s in expected:
                total = 0.0
                for p in expected[s]:
                    total += (1.0 / 3.0) * p
                assert combined[s][0] == total, (d, s)
            else:
                assert combined[s][0] is None, (d, s)
