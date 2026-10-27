"""A scoring window may miss one session: the score uses the two endpoints and is flagged."""
from __future__ import annotations

import pytest

from leadtrend_bt.engine import Backtest


def dates(n=20):
    from datetime import date, timedelta
    day = date(2024, 1, 1)
    return [(day + timedelta(days=i)).isoformat() for i in range(n)]


def market(series, signal_idx=(4, 9, 14, 19)):
    cal = dates(len(next(iter(series.values()))))
    tri = {sid: {d: v for d, v in zip(cal, values)} for sid, values in series.items()}
    pools = {d: dict(version_id='v', securities=[dict(security_id=s, issuer_id='I' + s) for s in series
                                                 if not s.startswith('BENCH')]) for d in [cal[i] for i in signal_idx]}
    return dict(calendar=cal, signal_days=[cal[i] for i in signal_idx], pools=pools, tri=tri,
                opens={s: dict(tri[s]) for s in series}, closes={s: dict(tri[s]) for s in series},
                issuer={s: 'I' + s for s in series if not s.startswith('BENCH')})


def base_cfg(**kw):
    cfg = dict(protocol='legacy_v1', run_id='unit', start=dates()[0], end=dates()[-1], mode='momentum', L=2, N=2, b=1,
               K=1, cost_bps=0, fee_per_order=1.0, capital=1000.0)
    cfg.update(kw)
    return cfg


def two_series():
    return {'A': [1 + 0.01 * i for i in range(20)],
            'B': [1 + 0.02 * i for i in range(20)],
            'BENCH:VOO': [1 + 0.002 * i for i in range(20)]}


def scored(cfg, mk, day):
    bt = Backtest(cfg, market=mk)
    bt.load()
    pool = mk['pools'][day]
    return bt, [sid for sid, _ in bt._scored(day, pool)]


def test_one_missing_session_is_scored_with_the_endpoints():
    mk = market(two_series())
    cal = dates()
    mk['tri']['B'].pop(cal[7])
    bt, ids = scored(base_cfg(protocol='research_v1', L=3, N=2, b=1, K=1), mk, cal[9])
    assert 'B' in ids
    assert bt.window_missing['B'] == 1
    # the score is tri_t / tri_{t-L} - 1 even though one session in between is absent
    assert dict(bt._scored(cal[9], mk['pools'][cal[9]]))['B'] == pytest.approx(
        mk['tri']['B'][cal[9]] / mk['tri']['B'][cal[6]] - 1)


def test_two_missing_sessions_are_refused():
    mk = market(two_series())
    cal = dates()
    mk['tri']['B'].pop(cal[7])
    mk['tri']['B'].pop(cal[8])
    bt, ids = scored(base_cfg(protocol='research_v1', L=3, N=2, b=1, K=1), mk, cal[9])
    assert 'B' not in ids
    assert 'A' in ids


def test_the_tolerance_can_be_switched_off():
    mk = market(two_series())
    cal = dates()
    mk['tri']['B'].pop(cal[7])
    _, ids = scored(base_cfg(protocol='research_v1', L=3, N=2, b=1, K=1, max_missing_days=0), mk, cal[9])
    assert 'B' not in ids


def test_legacy_protocol_has_no_window_check():
    """legacy_v1 predates the completeness rule: it scores the endpoints even with two holes."""
    mk = market(two_series())
    cal = dates()
    mk['tri']['B'].pop(cal[7])
    mk['tri']['B'].pop(cal[8])
    _, ids = scored(base_cfg(protocol='legacy_v1', L=3, N=2, b=1, K=1), mk, cal[9])
    assert 'B' in ids


def test_decisions_carry_the_flag():
    mk = market(two_series(), signal_idx=(4,))
    cal = dates()
    mk['tri']['B'].pop(cal[3])
    out = Backtest(base_cfg(protocol='research_v1', L=2, N=2, b=1, K=1), market=mk).run()
    buys = [d for d in out['decisions'] if d['action'] == 'buy']
    assert any(d['security_id'] == 'B' and d['window_missing'] == 1 for d in buys)
    assert all(d['window_missing'] == 0 for d in buys if d['security_id'] == 'A')


def test_the_endpoint_itself_cannot_be_missing():
    mk = market(two_series())
    cal = dates()
    mk['tri']['B'].pop(cal[9])                      # the signal day itself
    bt, ids = scored(base_cfg(protocol='research_v1', L=3, N=2, b=1, K=1), mk, cal[9])
    assert 'B' not in ids
