"""Signal definitions: skip-0 momentum equals the existing score, skip cells and rank-mean composites."""
import pytest

from leadtrend_v05 import signals
from leadtrend_v05.signals import composite_cells, momentum_cell, normalize, ranked, signal_key
from test_perf_arrays import synthetic_engine


def test_momentum_with_zero_skip_equals_the_existing_score_cell_for_cell():
    eng = synthetic_engine()
    for sid in ['GOOD', 'DIV', 'GAP', 'NEW', 'ZERO', 'BLK', 'SPLIT', 'SPIN', 'INV', 'UNPRICED']:
        for d in eng.sessions:
            for L in (1, 3, 5, 10):
                old, new = eng.score(sid, d, L), momentum_cell(eng, sid, d, L, 0)
                assert old == new and repr(old) == repr(new), (sid, d, L)


def test_momentum_with_skip_uses_the_lagged_window_and_the_full_availability_window():
    eng = synthetic_engine()
    tri = eng.tri['GOOD'].tri
    i = 30
    d = eng.sessions[i]
    L, skip = 5, 3
    score, tend, tstart, n, reason = momentum_cell(eng, 'GOOD', d, L, skip)
    assert reason == '' and n == L + 1
    assert tend == tri[eng.sessions[i - skip]] and tstart == tri[eng.sessions[i - skip - L]]
    assert score == tend / tstart - 1.0 and type(score) is float
    # Too little history for the whole lagged window: the existing availability reason.
    assert momentum_cell(eng, 'GOOD', eng.sessions[6], L, skip)[4] == 'insufficient_history'
    # A scoring block that applies inside the lagged window is reported with its own reason.
    assert momentum_cell(eng, 'BLK', eng.sessions[33], L, skip)[4] == 'price_gap_in_window'


def test_composite_rank_mean_is_the_normalised_weighted_percentile():
    sids = ['A', 'B', 'C']
    first = {'A': (0.3, None, None, None, ''), 'B': (0.1, None, None, None, ''), 'C': (0.2, None, None, None, '')}
    second = {'A': (0.0, None, None, None, ''), 'B': (0.5, None, None, None, ''), 'C': (0.4, None, None, None, '')}
    out = composite_cells(sids, [first, second], [1.0, 3.0])
    # first: A 1.0, C 0.5, B 0.0; second: B 1.0, C 0.5, A 0.0; weights normalised to 0.25 and 0.75
    assert out == {'A': (0.25, None, None, None, ''), 'B': (0.75, None, None, None, ''),
                   'C': (0.5, None, None, None, '')}


def test_composite_single_scorable_security_gets_percentile_one_and_first_listed_reason_wins():
    sids = ['A', 'B']
    first = {'A': (0.3, None, None, None, ''), 'B': (None, None, None, None, 'insufficient_history')}
    second = {'A': (0.2, None, None, None, ''), 'B': (None, None, None, None, 'price_gap_in_window')}
    out = composite_cells(sids, [first, second], [1.0, 1.0])
    assert out['A'] == (1.0, None, None, None, '')  # n = 1 scorable: percentile 1.0 on each component
    assert out['B'] == (None, None, None, None, 'insufficient_history')


def test_rank_follows_score_then_security_id():
    cells = {'B': (0.5, None, None, None, ''), 'A': (0.5, None, None, None, ''), 'C': (None, None, None, None, 'x')}
    assert ranked(cells) == {'A': 1, 'B': 2, 'C': None}


def test_signal_key_is_canonical_and_defaults_are_filled():
    a = {'kind': 'momentum', 'L': 90, 'skip': 0}
    b = {'skip': 0, 'L': 90, 'kind': 'momentum'}
    assert signal_key(a) == signal_key(b) == signal_key({'kind': 'momentum', 'L': 90})
    assert signal_key(a) != signal_key({'kind': 'momentum', 'L': 90, 'skip': 5})
    comp = {'kind': 'composite', 'components': [a], 'combine': 'rank_mean'}
    assert normalize(comp)['weights'] == [1.0]


@pytest.mark.parametrize('bad', [
    {'kind': 'momentum', 'L': 0},
    {'kind': 'momentum', 'L': 90, 'skip': -1},
    {'kind': 'momentum', 'L': True},
    {'kind': 'composite', 'components': []},
    {'kind': 'composite', 'components': [{'kind': 'momentum', 'L': 60}], 'weights': [0.0]},
    {'kind': 'composite', 'components': [{'kind': 'momentum', 'L': 60}], 'combine': 'mean'},
    {'kind': 'random', 'seed': 1, 'base': {'kind': 'momentum', 'L': 60}},
    {'kind': 'mystery'},
])
def test_invalid_signals_are_refused(bad):
    with pytest.raises(ValueError):
        normalize(bad)


def test_main_L_is_the_first_momentum_component():
    comp = {'kind': 'composite', 'components': [{'kind': 'momentum', 'L': 60}, {'kind': 'momentum', 'L': 126}]}
    assert signals.main_L(comp) == 60
