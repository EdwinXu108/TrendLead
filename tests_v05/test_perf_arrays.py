"""Array-backed cache views: membership, quotes and marks behave as the former dicts did; fast scores equal ScoreEngine."""
from collections import defaultdict
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from leadtrend_v05.perf_artifacts import write_gzip_csv
from leadtrend_v05.perf_cache import FastScores, MarkView, QuoteView, SharedFrames
from leadtrend_v05.scoring import TRIResult
from leadtrend_v05.stage3_scoring import ScoreEngine


def tiny_quotes():
    axes = dict(sids=['A', 'B'], days=[date(2020, 1, 2), date(2020, 1, 3)])
    opn = np.array([[1.0, 2.0], [0.0, 0.0]])
    cls = np.array([[1.5, 2.5], [0.0, 0.0]])
    pre = np.array([[True, True], [False, False]])
    return QuoteView(axes, opn, cls, pre)


def test_quote_view_membership_and_keyerror():
    q = tiny_quotes()
    assert ('A', date(2020, 1, 2)) in q and ('A', date(2020, 1, 3)) in q
    assert ('B', date(2020, 1, 2)) not in q and ('C', date(2020, 1, 2)) not in q and ('A', date(2020, 1, 4)) not in q
    got = q['A', date(2020, 1, 3)]
    assert type(got.open) is float and type(got.close) is float
    assert got.open == 2.0 and got.close == 2.5
    with pytest.raises(KeyError):
        q['B', date(2020, 1, 2)]


def test_marks_view_is_empty_for_unknown_session_and_absent_security():
    m = MarkView(tiny_quotes())
    day = m[date(2020, 1, 3)]
    assert 'A' in day and 'B' not in day
    assert type(day['A']) is float and day['A'] == 2.5
    with pytest.raises(KeyError, match="'B'"):
        day['B']
    empty = m[date(2030, 1, 1)]  # the former defaultdict returned an empty mapping here
    assert 'A' not in empty
    with pytest.raises(KeyError, match="'A'"):
        empty['A']


def synthetic_engine():
    """A ScoreEngine built by hand (no inputs) so that the real availability and momentum code runs on edge cases."""
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(60)]
    eng = ScoreEngine.__new__(ScoreEngine)
    eng.sessions = days
    eng.pos = {d: i for i, d in enumerate(days)}
    eng.return_end = days[-1]
    eng.prices = defaultdict(dict)
    eng.events = defaultdict(list)
    eng.blocks = defaultdict(list)
    eng.tri = {}
    eng.first_price_date = {}

    def add(sid, priced, tri, invalid_from=None):
        eng.prices[sid] = {d: True for d in priced}
        eng.first_price_date[sid] = min(priced)
        eng.tri[sid] = TRIResult(tri=tri, valid_dates=sorted(tri), invalid_from=invalid_from)

    def ev(sid, kind, d, **kw):
        return SimpleNamespace(event_id=f'{sid}|{kind}|{d}', security_id=sid, event_type=kind, effective_session=d,
                               spinoff_valuation=kw.get('valuation'), spinoff_value_per_old_share=kw.get('value'))

    def series(rng_days, start=100.0, step=1.01):
        return {d: start * step ** i for i, d in enumerate(rng_days)}

    add('GOOD', days, {d: v for d, v in series(days).items()})
    add('DIV', days, {d: v for d, v in series(days, 50.0, 1.005).items()})
    eng.events['DIV'].append(ev('DIV', 'cash_dividend', days[25]))
    gap = [d for d in days if d not in days[10:13]]
    add('GAP', gap, {d: v for d, v in series(gap, 80.0, 1.002).items()})
    new = days[20:]
    add('NEW', new, {d: v for d, v in series(new, 30.0, 1.02).items()})
    zero = days
    z = {d: (0.0 if i < 5 else 10.0 * 1.01 ** i) for i, d in enumerate(zero)}
    add('ZERO', zero, z)
    add('BLK', days, {d: v for d, v in series(days).items()})
    eng.blocks['BLK'].append((days[30], 'price_gap_in_window'))
    add('SPLIT', days, {d: v for d, v in series(days, 40.0, 1.003).items()})
    eng.events['SPLIT'].append(ev('SPLIT', 'split', days[15]))
    add('SPIN', days, {d: v for d, v in series(days, 60.0, 1.004).items()})
    eng.events['SPIN'].append(ev('SPIN', 'spinoff', days[18], valuation='unit', value=1.0))
    add('INV', days[:40], {d: v for d, v in series(days[:40]).items()}, invalid_from=days[40])
    unpriced = [d for d in days if d != days[30]]
    add('UNPRICED', unpriced, {d: v for d, v in series(unpriced, 20.0, 1.001).items()})
    eng.events['UNPRICED'].append(ev('UNPRICED', 'cash_dividend', days[30]))  # effective day has no price
    return eng


def test_fast_path_matches_score_engine_cell_for_cell():
    eng = synthetic_engine()
    fast = FastScores(eng)
    used = fallback = 0
    for sid in ['GOOD', 'DIV', 'GAP', 'NEW', 'ZERO', 'BLK', 'SPLIT', 'SPIN', 'INV', 'UNPRICED', 'NOPRICE']:
        for d in eng.sessions:
            for L in (1, 3, 5, 10):
                slow = eng.score(sid, d, L)
                cell = fast.score(sid, d, L)
                if cell is None:
                    fallback += 1
                    continue
                used += 1
                assert cell == slow and repr(cell) == repr(slow), (sid, d, L)
                assert type(cell[0]) is float and type(cell[1]) is float and type(cell[2]) is float
                assert type(cell[3]) is int and cell[4] == ''
    assert used > 500 and fallback > 500  # both branches are exercised on these edge cases


def test_fast_path_declines_every_excluded_security():
    eng = synthetic_engine()
    fast = FastScores(eng)
    for sid in ['SPLIT', 'SPIN', 'INV', 'NOPRICE']:
        assert all(fast.score(sid, d, 5) is None for d in eng.sessions), sid
    assert all(fast.score('BLK', d, 5) is None for d in eng.sessions[30:])  # the block starts at day 30
    # After the unpriced dividend's effective day the security is no longer fast-scored; before it, it is.
    assert fast.score('UNPRICED', eng.sessions[35], 5) is None
    assert fast.score('UNPRICED', eng.sessions[20], 5) is not None


def test_shared_frames_reproduce_the_frame_rows_exactly(tmp_path):
    iso = '2020-01-03'
    universe = [
        dict(signal_session_date=iso, security_id='A', snapshot_id='S1', holdings_as_of='2019-12-31', available_at='2020-01-02',
             issuer_id='I1', eligible=1, class_selected=1, eligibility_reason=''),
        dict(signal_session_date=iso, security_id='B', snapshot_id='S1', holdings_as_of='2019-12-31', available_at='2020-01-02',
             issuer_id='I2', eligible=0, class_selected=0, eligibility_reason='not_eligible')]
    scores = [
        dict(signal_session_date=iso, security_id='A', issuer_id='I1', industry='Tech', L=10, window_sessions=11,
             tri_start=1.0, tri_end=1.0123456789012345, score=0.0123456789012345, unscorable_reason='', rank=1),
        dict(signal_session_date=iso, security_id='C', issuer_id='I3', industry='Health', L=10, window_sessions=11,
             tri_start=None, tri_end=None, score=None, unscorable_reason='insufficient_history', rank=None)]
    u = tmp_path / 'universe.csv.gz'; s = tmp_path / 'scores_L10.csv.gz'
    write_gzip_csv(u, universe, list(universe[0])); write_gzip_csv(s, scores, list(scores[0]))
    frames = SharedFrames({'universe': dict(path=u), 10: dict(path=s)})
    week = frames[date(2020, 1, 3)]
    assert week['chosen'] == frozenset({'A'})
    assert week['universe'] == universe
    window = week['windows'][10]
    assert window['scores'] == scores
    assert [c.security_id for c in window['candidates']] == ['A']
    assert window['candidates'][0].score == scores[0]['score']
    assert window['unscored'] == {'C': 'insufficient_history'} and window['ranked'] == {'A': 1}
    assert window is not None and week['windows'][10]['scores'] is not None
    assert [w.iso for w in frames.values()] == [iso]
    with pytest.raises(KeyError):
        frames[date(2020, 1, 10)]


def assert_plain(value):
    """Every value reaching an output row is a Python str, int, float, bool or None."""
    assert type(value) in (str, int, float, bool, type(None)), (type(value), value)


def test_values_leaving_the_views_are_python_types():
    eng = synthetic_engine()
    fast = FastScores(eng)
    rows = [dict(score=c[0], tri_end=c[1], tri_start=c[2], window_sessions=c[3], reason=c[4])
            for c in (fast.score('GOOD', d, 5) for d in eng.sessions) if c is not None]
    assert rows
    for row in rows:
        for value in row.values():
            assert_plain(value)
    q = tiny_quotes()
    assert_plain(q['A', date(2020, 1, 2)].open)
    assert_plain(MarkView(q)[date(2020, 1, 2)]['A'])
