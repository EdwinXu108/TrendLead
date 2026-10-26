"""Round 2 engineering: start-offset and issuer-exclusion robustness runs, risk columns, registry anchors, L-window naming.

Nothing here reads development data except the committed registration files.
"""
from datetime import date
from pathlib import Path

import pytest

from leadtrend_v05.experiments import _anchor_point, make_point, pair_rows, registered_points, risk_from_series
from leadtrend_v05.overlay_signals import size_tilt
from leadtrend_v05.robustness import IssuerExcludedFrames, RobustnessContext
from leadtrend_v05.selection import Candidate
from leadtrend_v05.signals import main_L, normalize, signal_key, week_rows

ROOT = Path(__file__).resolve().parent.parent
WEEKS = ['2019-11-29', '2019-12-06', '2019-12-13', '2019-12-20', '2019-12-27', '2020-01-03']
SEC3_ID = 'L090_N10_b2_R2_sector_cap_dcda083d'


class _Frames:
    """Stands in for the shared frames: only the signal-week list is needed for authorisation."""

    def __init__(self, weeks):
        self._weeks = list(weeks)

    def weeks(self):
        return list(self._weeks)


def _context(weeks=WEEKS):
    ctx = RobustnessContext.__new__(RobustnessContext)
    ctx.segment = dict(start='2019-11-29', end='2024-06-28')
    ctx.start, ctx.end = date(2019, 11, 29), date(2024, 6, 28)
    ctx.frames = _Frames(weeks)
    p = make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2')
    ctx.configure(points=[p], costs=[5, 10], experiment={'id': 'T_ROBUST'})
    return ctx, p


def _raw(p, **changes):
    raw = dict(configuration_id=p['id'], L=p['L'], N=p['N'], k=p['k'], b=p['b'], rebalance=p['rebalance'],
               selection='default', slippage_bps=10, initial_capital=100000, fee_per_trade=1,
               start_date='2019-11-29', end_date='2024-06-28')
    raw.update(changes)
    return raw


def test_offset_starts_admit_the_development_start_and_the_first_four_signal_weeks():
    ctx, p = _context()
    for k in range(5):
        assert ctx.authorize(_raw(p, start_date=WEEKS[k]), None) == (date.fromisoformat(WEEKS[k]), ctx.end)


def test_a_start_beyond_the_first_four_offsets_or_off_the_calendar_is_refused():
    ctx, p = _context()
    with pytest.raises(ValueError, match='start must be'):
        ctx.authorize(_raw(p, start_date=WEEKS[5]), None)
    with pytest.raises(ValueError, match='start must be'):
        ctx.authorize(_raw(p, start_date='2019-12-02'), None)


def test_an_offset_run_cannot_move_the_end_or_use_an_end_override():
    ctx, p = _context()
    with pytest.raises(ValueError, match='segment'):
        ctx.authorize(_raw(p, start_date=WEEKS[1], end_date='2024-06-21'), None)
    with pytest.raises(ValueError, match='segment'):
        ctx.authorize(_raw(p, start_date=WEEKS[1]), '2024-06-21')


def test_offset_runs_refuse_unregistered_configurations_and_changed_parameters():
    ctx, p = _context()
    with pytest.raises(ValueError, match='not in the registered'):
        ctx.authorize(_raw(p, configuration_id='L010_N08_b2_R2', start_date=WEEKS[1]), None)
    with pytest.raises(ValueError, match='does not match'):
        ctx.authorize(_raw(p, start_date=WEEKS[1], k=7), None)


def test_the_first_signal_week_must_be_the_development_start():
    ctx, p = _context(weeks=WEEKS[1:])
    with pytest.raises(ValueError, match='first signal week'):
        ctx.authorize(_raw(p, start_date=WEEKS[1]), None)


class _Week(dict):
    pass


def test_issuer_exclusion_removes_every_class_of_the_issuer_from_chosen_candidates_and_universe():
    d = date(2020, 1, 3)
    cands = [Candidate('A1', 'ISS_A', 'x', 0.3), Candidate('A2', 'ISS_A', 'x', 0.2), Candidate('B1', 'ISS_B', 'y', 0.1)]
    week = _Week(chosen=frozenset({'A1', 'A2', 'B1'}),
                 universe=[dict(security_id='A1', issuer_id='ISS_A'), dict(security_id='A2', issuer_id='ISS_A'),
                           dict(security_id='B1', issuer_id='ISS_B')],
                 windows={90: dict(scores=[], ranked={}, candidates=cands, unscored={'A2': 'price_gap_in_window'})})

    class _Frames:
        def weeks(self):
            return [d.isoformat()]

        def __getitem__(self, key):
            return week

    view = IssuerExcludedFrames(_Frames(), {'A1', 'A2'})[d]
    assert view['chosen'] == frozenset({'B1'})
    assert [r['security_id'] for r in view['universe']] == ['B1']
    window = view['windows'][90]
    assert [c.security_id for c in window['candidates']] == ['B1'] and window['unscored'] == {}
    assert IssuerExcludedFrames(_Frames(), set()).weeks() == [d.isoformat()]


def test_risk_from_series_gives_beta_tracking_error_and_the_2022_drawdown_inside_the_year():
    bench_returns = [0.01, -0.02, 0.015, 0.005, -0.01]
    bench = [100.0]
    for r in bench_returns:
        bench.append(bench[-1] * (1 + r))
    strategy = [100.0]
    for r in bench_returns:
        strategy.append(strategy[-1] * (1 + 2 * r))  # twice VOO's daily return
    days = ['2021-12-31', '2022-01-03', '2022-01-04', '2022-01-05', '2022-01-06', '2022-01-07']
    risk = risk_from_series(days, strategy, bench, annual_return=0.1, max_drawdown=-0.3)
    assert risk['beta'] == pytest.approx(2.0, rel=1e-9)
    assert risk['te'] == pytest.approx(risk['vol'] / 2, rel=1e-9)  # tracking error is VOO's own deviation
    assert risk['ret_vol'] == pytest.approx(0.1 / risk['vol'])
    assert risk['ret_dd'] == pytest.approx(0.1 / 0.3)
    # Inside 2022 the peak is the highest 2022 value; the 2021 level of 200 must not count.
    series = [200.0, 100.0, 120.0, 90.0, 110.0]
    assert risk_from_series(['2021-12-31', '2022-01-03', '2022-01-04', '2022-01-05', '2022-01-06'], series,
                            [1.0] * 5, 0.1, -0.3)['dd_2022'] == pytest.approx(90 / 120 - 1)


def test_registered_points_hold_the_round_one_anchor_and_stage_four_resolves_as_before():
    assert registered_points(ROOT)[SEC3_ID]['name'] == 'SEC3'
    assert _anchor_point(ROOT, SEC3_ID, [])['name'] == 'SEC3'
    a90 = _anchor_point(ROOT, 'L090_N10_b2_R2', [])
    assert (a90['L'], a90['N'], a90['k']) == (90, 10, 3)
    with pytest.raises(ValueError, match='anchor is not'):
        _anchor_point(ROOT, 'L999_N10_b2_R2', [])


def test_pair_rows_take_the_registration_pairs_and_the_new_aliases():
    points = [{'id': 'X1', 'name': 'C_SEC3_SIZE'}, {'id': 'X2', 'name': 'SEC3'}]
    m10 = {'X1': dict(annualized_excess=0.05, high_missing_excess=0.02, lower_missing_excess=0.01),
           'X2': dict(annualized_excess=0.03, high_missing_excess=0.01, lower_missing_excess=0.02),
           'L126_N10_b2_R2': dict(annualized_excess=0.01, high_missing_excess=0.0, lower_missing_excess=0.0)}
    m5 = {'X1': dict(annualized_excess=0.04), 'X2': dict(annualized_excess=0.01),
          'L126_N10_b2_R2': dict(annualized_excess=0.0)}
    rows = pair_rows(points + [{'id': 'L126_N10_b2_R2'}], m10, m5, [('C_SEC3_SIZE', 'SEC3'), ('C_SEC3_SIZE', 'A126')])
    assert [r['pair'] for r in rows] == ['C_SEC3_SIZE-SEC3', 'C_SEC3_SIZE-A126']
    assert abs(rows[0]['diff_10bps'] - 0.02) < 1e-12 and abs(rows[1]['diff_10bps'] - 0.04) < 1e-12
    assert rows[1]['b_id'] == 'L126_N10_b2_R2'


def test_l126_signals_keep_their_window_and_size_tilt_with_zero_weight_is_the_momentum_percentile():
    sig = normalize({'kind': 'size_tilt', 'L': 126})
    assert main_L(sig) == 126 and sig['weight'] == 0.1
    assert signal_key(sig) != signal_key(normalize({'kind': 'size_tilt', 'L': 90}))
    assert normalize({'kind': 'sector_demeaned', 'L': 126})['L'] == 126
    cells = {'A': (0.30, 1, 1, 127, ''), 'B': (0.10, 1, 1, 127, ''), 'C': (0.20, 1, 1, 127, '')}
    out = size_tilt(cells, {'A': 0.0, 'B': 0.0, 'C': 0.0}, weight=0.0)
    assert out['A'][0] == pytest.approx(1.0) and out['C'][0] == pytest.approx(0.5) and out['B'][0] == pytest.approx(0.0)
    rows = week_rows('2020-01-03', ['A', 'B', 'C'], out, {s: ('I', 'x') for s in 'ABC'}, 126, 'key')
    assert {r['L'] for r in rows} == {126}
