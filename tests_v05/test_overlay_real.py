"""Round-1 overlay signals on the real development data, recomputed with pandas for five signal weeks.

Verification only: the scores are compared with the cached score tables; no backtest is run and no return after the
development end is read.
"""
import random
from pathlib import Path

import pytest

from leadtrend_v05.perf_cache import build_context, ensure_signal_tables, identity, load_cache
from leadtrend_v05.perf_cache import _table_by_week
from leadtrend_v05.sectors import load_sector_map, sector_key

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.slow

SIGNALS = [{'kind': 'peer', 'L': 90, 'L2': 60}, {'kind': 'sector_demeaned', 'L': 90}, {'kind': 'vol_adjusted', 'L': 90, 'vol_L': 60},
           {'kind': 'size_tilt', 'L': 90}, {'kind': 'voo_weight', 'L': 90}]


def _pct(pd, scores):
    s = pd.Series(scores, dtype='float64').sort_index()
    n = len(s)
    rank = s.rank(method='first', ascending=False)
    return {k: ((n - rank[k]) / (n - 1) if n > 1 else 1.0) for k in s.index}


def test_overlay_scores_equal_an_independent_pandas_recomputation():
    pd = pytest.importorskip('pandas')
    np = pytest.importorskip('numpy')
    if not (ROOT / 'research/v05/registry/stage4_registry.json').exists():
        pytest.skip('real development data not present')
    doc = identity(ROOT, 'uniform')
    load_cache(ROOT, doc)
    paths_out = ensure_signal_tables(ROOT, doc, SIGNALS)
    tables = [_table_by_week(p) for p in paths_out]
    ctx = build_context(ROOT, 'uniform')
    engine, frames, full = ctx.prepared[-1], ctx.frames, ctx.full
    from leadtrend_v05.eligibility import eligibility_index
    elig = eligibility_index(full)
    sector_map = load_sector_map(ROOT)
    member_weight = {(m.snapshot_id, m.security_id): (m.weight or 0.0) for m in full.members}
    weeks = sorted(frames)
    for d in random.Random(0).sample(weeks, 5):
        iso = d.isoformat()
        win90 = frames[d]['windows'][90]['candidates']
        win60 = frames[d]['windows'][60]['candidates']
        m90 = {c.security_id: c.score for c in win90}
        m60 = {c.security_id: c.score for c in win60}
        sector = {s: sector_key(elig[s].industry, sector_map) for s in set(m90) | set(m60)}
        p90 = _pct(pd, m90)
        p60 = _pct(pd, m60)
        both = [s for s in p90 if s in p60]

        expected = {}
        for s in p90:
            peers = [t for t in both if t != s and sector[t][1] and sector[t][0] == sector[s][0]]
            if sector[s][1] and len(peers) >= 3:
                expected.setdefault('peer', {})[s] = 0.75 * p90[s] + 0.25 * float(np.median([p60[t] for t in peers]))
            else:
                expected.setdefault('peer', {})[s] = p90[s]

        overall = float(np.median(list(m90.values())))
        for s in m90:
            group = [t for t in m90 if sector[t][1] and sector[t][0] == sector[s][0]]
            if sector[s][1] and len(group) >= 3:
                expected.setdefault('sector_demeaned', {})[s] = m90[s] - float(np.median([m90[t] for t in group]))
            else:
                expected.setdefault('sector_demeaned', {})[s] = m90[s] - overall

        issuer_weight = {}
        issuer_of = {}
        for row in frames[d]['universe']:
            issuer_of[row['security_id']] = row['issuer_id']
            issuer_weight[row['issuer_id']] = issuer_weight.get(row['issuer_id'], 0.0) + member_weight.get(
                (row['snapshot_id'], row['security_id']), 0.0)
        weight_of = {s: issuer_weight.get(issuer_of.get(s), 0.0) for s in m90}
        ordered = sorted(p90, key=lambda s: (-weight_of[s], s))
        n = len(ordered)
        qmap = {s: ((n - i) / (n - 1) if n > 1 else 1.0) for i, s in enumerate(ordered, 1)}
        for s in m90:
            expected.setdefault('voo_weight', {})[s] = weight_of[s]
            if s in p90:
                expected.setdefault('size_tilt', {})[s] = 0.9 * p90[s] + 0.1 * qmap[s]

        sessions = engine.sessions
        index = engine.pos[d]
        for s in m90:
            tri = engine.tri[s].tri
            values = [tri.get(sessions[j]) for j in range(index - 60, index + 1)]
            if any(v is None or v <= 0 for v in values) or s not in p90:
                continue
            logs = pd.Series(np.log(values)).diff().dropna()
            sigma = float(logs.std(ddof=1))
            if sigma > 1e-12:
                expected.setdefault("vol_adjusted", {})[s] = m90[s] / sigma

        for name, table, kind in zip(['peer', 'sector_demeaned', 'vol_adjusted', 'size_tilt', 'voo_weight'], tables, SIGNALS):
            rows = table.get(iso, {})
            for s, value in expected.get(name, {}).items():
                got = float(rows[s]['score'])
                if name == 'vol_adjusted':  # log(ratio) against log-difference: equal to float rounding only
                    assert got == pytest.approx(value, rel=1e-12), (name, iso, s)
                else:
                    assert got == value, (name, iso, s)
            assert {s for s, r in rows.items() if r['score'] not in ('', None)} >= set(expected.get(name, {})), (name, iso)
