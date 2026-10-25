"""Diagnostics D0 on synthetic inputs: statistics, reference books, forward returns, authorisation and date limits."""
import json
import math
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from leadtrend_v05 import diagnostics as D
from leadtrend_v05.signals import normalize

ROOT = Path(__file__).resolve().parent.parent
END = date(2024, 6, 28)


def week(t, pool, exec_day, scores=None, issuer=None, weight=None):
    scores = scores or {L: {sid: 0.5 for sid in pool} for L in D.WINDOWS}
    return dict(t=date.fromisoformat(t), pool=pool, exec=None if exec_day is None else date.fromisoformat(exec_day),
                issuer_weight=weight or {sid: 1.0 for sid in pool}, industry={sid: 'X' for sid in pool},
                member_weight={}, total_weight=1.0, scores=scores)


def tri_from(table):
    def tri(sid, d):
        return table.get(sid, {}).get(d)
    return tri


# ---------------------------------------------------------------- statistics

def test_rank_correlation_is_one_for_monotone_and_handles_ties():
    assert D.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert D.spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    # ties take average ranks: x ranks 1.5, 1.5, 3.5, 3.5 against 1..4
    assert D.spearman([1, 1, 2, 2], [1, 2, 3, 4]) == pytest.approx(4 / math.sqrt(20))
    assert D.spearman([1, 1, 1], [1, 2, 3]) is None  # no variation in scores


def test_newey_west_t_matches_hand_calculation_and_is_undefined_for_constant_series():
    # mean 3, deviations -2..2, gamma0 = 2, lag 0: t = 3 / sqrt(2 / 5)
    assert D.newey_west_t([1, 2, 3, 4, 5], lag=0) == pytest.approx(3 / math.sqrt(0.4))
    assert D.newey_west_t([0.1, 0.1, 0.1, 0.1], lag=1) is None


def test_annualisation_uses_days_over_365_25_like_stage4():
    assert D.annualize(0.1, 365.25) == pytest.approx(0.1)
    assert D.annualize(0.21, 2 * 365.25) == pytest.approx(0.1)


# ---------------------------------------------------------------- D1 reference books

def test_cw_is_issuer_weight_normalised_and_ew_and_top_books_are_equal_weighted():
    tri = tri_from({'A1': {date(2024, 1, 5): 100.0, date(2024, 1, 12): 110.0},
                    'B1': {date(2024, 1, 5): 100.0, date(2024, 1, 12): 90.0},
                    'C1': {date(2024, 1, 5): 100.0, date(2024, 1, 12): 100.0},
                    'VOO': {date(2024, 1, 5): 100.0, date(2024, 1, 12): 101.0}})
    rec = week('2024-01-05', ['A1', 'B1', 'C1'], '2024-01-08', weight={'A1': 0.6, 'B1': 0.4, 'C1': 0.0})
    nxt = week('2024-01-12', ['A1'], None)
    voo = {date(2024, 1, 5): 100.0, date(2024, 1, 12): 101.0}
    weekly, counts = D.d1_portfolios([rec, nxt], tri, voo)
    row = weekly[0]
    assert row['CW'] == pytest.approx(0.6 * 0.10 + 0.4 * (-0.10))  # zero-weight C1 drops out of CW
    assert row['EW'] == pytest.approx((0.10 - 0.10 + 0.0) / 3)     # EW keeps all three names
    assert row['TOPW8'] == pytest.approx((0.10 - 0.10) / 2)        # top books: only two names carry weight
    assert row['voo'] == pytest.approx(0.01)
    assert counts[0]['n_pool'] == 3 and counts[0]['no_tri_entry'] == 0 and counts[0]['no_tri_exit'] == 0


def test_securities_missing_tri_are_counted_and_excluded_from_that_week():
    tri = tri_from({'A1': {date(2024, 1, 5): 100.0, date(2024, 1, 12): 120.0},
                    'B1': {date(2024, 1, 12): 90.0}})  # B1 has no TRI at entry
    voo = {date(2024, 1, 5): 100.0, date(2024, 1, 12): 100.0}
    rec = week('2024-01-05', ['A1', 'B1'], '2024-01-08', weight={'A1': 0.5, 'B1': 0.5})
    nxt = week('2024-01-12', ['A1'], None)
    weekly, counts = D.d1_portfolios([rec, nxt], tri, voo)
    assert counts[0]['no_tri_entry'] == 1
    assert weekly[0]['CW'] == pytest.approx(0.20)  # only A1 is held


def test_a_week_whose_next_signal_is_after_the_segment_end_is_not_built():
    tri = tri_from({'A1': {date(2024, 6, 21): 100.0, date(2024, 6, 28): 101.0, date(2024, 7, 5): 150.0}})
    voo = {date(2024, 6, 21): 100.0, date(2024, 6, 28): 100.0, date(2024, 7, 5): 100.0}
    recs = [week('2024-06-21', ['A1'], '2024-06-24'), week('2024-06-28', ['A1'], None),
            week('2024-07-05', ['A1'], None)]
    weekly, _ = D.d1_portfolios(recs, tri, voo)
    assert [w['t'] for w in weekly] == ['2024-06-21']  # the 2024-06-28 -> 2024-07-05 week is never read
    assert all(date.fromisoformat(w['t_next']) <= END for w in weekly)


# ---------------------------------------------------------------- D2 forward returns and gaps

def fake_ctx(px, events):
    return SimpleNamespace(prepared=(None, px, None, None, None, None), full=SimpleNamespace(events=events))


def test_forward_open_return_uses_execution_gap_and_is_computed_from_close_to_close_tri():
    px = {('A', date(2024, 1, 5)): SimpleNamespace(open=99.0, close=100.0),
          ('A', date(2024, 1, 8)): SimpleNamespace(open=105.0, close=106.0)}
    tri = tri_from({'A': {date(2024, 1, 5): 100.0, date(2024, 1, 12): 110.0, date(2024, 1, 19): 121.0}})
    recs = [week('2024-01-05', ['A'], '2024-01-08'), week('2024-01-12', ['A'], '2024-01-15'),
            week('2024-01-19', ['A'], None)]
    weeks, stats = D.d2_forward(recs, fake_ctx(px, []), tri)
    base = weeks[0]['base']['A']
    assert base['gap'] == pytest.approx(105.0 / 100.0 - 1)
    assert weeks[0]['fwd'][1]['A'] == pytest.approx(1.10 / 1.05 - 1)  # fwd_open = (1 + fwd_close) / (1 + gap) - 1
    assert 4 not in weeks[0]['fwd']  # four weeks ahead does not exist in these records
    assert stats == dict(excluded_corporate=0, excluded_price=1)  # week two has no execution-day price


def test_securities_with_a_corporate_action_on_the_execution_day_are_excluded_and_counted():
    px = {('A', date(2024, 1, 5)): SimpleNamespace(open=100.0, close=100.0),
          ('A', date(2024, 1, 8)): SimpleNamespace(open=100.0, close=100.0)}
    events = [SimpleNamespace(effective_session=date(2024, 1, 8), security_id='A', event_type='split')]
    tri = tri_from({'A': {date(2024, 1, 5): 100.0, date(2024, 1, 12): 100.0}})
    recs = [week('2024-01-05', ['A'], '2024-01-08'), week('2024-01-12', ['A'], None)]
    weeks, stats = D.d2_forward(recs, fake_ctx(px, events), tri)
    assert weeks[0]['base'] == {} and weeks[0]['fwd'].get(1, {}) == {}
    assert stats['excluded_corporate'] == 1


def test_forward_quantities_never_read_a_date_after_the_segment_end():
    px = {('A', date(2024, 6, 21)): SimpleNamespace(open=100.0, close=100.0),
          ('A', date(2024, 6, 24)): SimpleNamespace(open=100.0, close=100.0)}
    asked = []

    def tri(sid, d):
        asked.append(d)
        return {date(2024, 6, 21): 100.0, date(2024, 6, 28): 101.0, date(2024, 7, 5): 999.0}.get(d)
    recs = [week('2024-06-21', ['A'], '2024-06-24'), week('2024-06-28', ['A'], '2024-07-01'),
            week('2024-07-05', ['A'], None)]
    weeks, _ = D.d2_forward(recs, fake_ctx(px, []), tri)
    assert max(asked) <= END
    assert 1 in weeks[0]['fwd'] and 4 not in weeks[0]['fwd']


def test_ic_and_quintiles_on_a_perfectly_ordered_synthetic_pool():
    pool = [f'S{i:02d}' for i in range(10)]
    scores = {L: {sid: float(i) for i, sid in enumerate(pool)} for L in D.WINDOWS}
    weeks = []
    for k in range(3):
        weeks.append(dict(t=date(2024, 1, 5 + 7 * k), pool=pool, scores=scores,
                          base={sid: dict(gap=0.0) for sid in pool},
                          fwd={1: {sid: 0.01 * i for i, sid in enumerate(pool)}}))
    ic, quint, top = D.d2_tables(weeks)
    row = next(r for r in ic if r['L'] == 10 and r['h'] == 1)
    assert row['ic_mean'] == pytest.approx(1.0) and row['n_weeks'] == 3
    q = next(r for r in quint if r['L'] == 10 and r['h'] == 1)
    assert q['spread_Q5_minus_Q1'] > 0 and q['Q5_fwd_open'] > q['Q1_fwd_open']


# ---------------------------------------------------------------- helpers

def test_issuer_weight_goes_to_the_selected_class_and_other_classes_get_nothing():
    member_weight = {'A1': 0.3, 'A2': 0.2, 'B1': 0.5}
    issuer_of = {'A1': 'I1', 'A2': 'I1', 'B1': 'I2'}
    assert D.pool_issuer_weights(member_weight, issuer_of, ['A1', 'B1']) == {'A1': 0.5, 'B1': 0.5}


def test_sector_map_covers_every_listed_industry_and_marks_unknown_and_bench():
    mapping = D.load_sector_map(ROOT / 'config/sector_map_v1.csv')
    assert mapping['US_LIST2015'] == '信息技术' and mapping['住宅施工'] == '可选消费'
    assert D.sector_of('UNKNOWN:LEI:abc', mapping) == 'UNKNOWN'
    with pytest.raises(ValueError, match='without a sector mapping'):
        D.sector_of('BENCH', mapping)  # the benchmark marker is not an industry and must never be labelled
    assert D.sector_of('', mapping) == 'UNKNOWN'
    assert set(mapping.values()) <= set(D.SECTORS)
    assert len(D.SECTORS) == 11


# ---------------------------------------------------------------- D5 authorisation

POINT = dict(id='DIAG_L090_N08_b2_R2_s000', registry_id='L090_N08_b2_R2', signal=normalize({'kind': 'momentum', 'L': 90, 'skip': 0}),
             L=90, N=8, k=2, b=2, rebalance='R2', selection='random_matched', selection_params=dict(seed=0, exit_counts={}))


class _DiagCtx(D.DiagnosticContext):
    def __init__(self, point=POINT):  # bypass the cache-only constructor; authorize needs only these attributes
        self.segment = {'start': D.DEV_START, 'end': D.DEV_END}
        self.start, self.end = None, None
        self.configure(points=[point], costs=[10], experiment=D.DIAG_EXPERIMENT)


def raw_ok(**over):
    raw = dict(configuration_id=POINT['id'], L=90, N=8, k=2, b=2, rebalance='R2',
               signal={'kind': 'momentum', 'L': 90, 'skip': 0}, selection='random_matched',
               selection_params=dict(seed=0, exit_counts={}), slippage_bps=10, initial_capital=100000, fee_per_trade=1,
               start_date=D.DEV_START, end_date=D.DEV_END)
    raw.update(over)
    return raw


def test_diagnostic_authorisation_admits_a_registered_random_matched_10bp_development_control():
    assert _DiagCtx().authorize(raw_ok(), None) == (None, None)


def test_diagnostic_authorisation_refuses_a_configuration_that_is_not_a_stage4_registration():
    bad = dict(POINT, id='DIAG_L015_s000', registry_id='L015_N08_b2_R2')
    with pytest.raises(ValueError, match='stage 4 registered'):
        _DiagCtx(bad).authorize(dict(raw_ok(), configuration_id=bad['id']), None)


def test_diagnostic_authorisation_refuses_a_non_random_selection():
    with pytest.raises(ValueError, match='random_matched'):
        _DiagCtx().authorize(raw_ok(selection='default', selection_params=None), None)


def test_diagnostic_authorisation_refuses_any_cost_other_than_10bp():
    with pytest.raises(ValueError, match='10 bp'):
        _DiagCtx().authorize(raw_ok(slippage_bps=5), None)


def test_diagnostic_authorisation_refuses_dates_outside_the_development_segment():
    with pytest.raises(ValueError, match='development segment'):
        _DiagCtx().authorize(raw_ok(end_date='2024-07-05'), None)
    with pytest.raises(ValueError, match='development segment'):
        _DiagCtx().authorize(raw_ok(), '2024-07-05')


def test_diagnostic_authorisation_refuses_a_configuration_that_does_not_match_its_registration():
    with pytest.raises(ValueError, match='does not match'):
        _DiagCtx().authorize(raw_ok(L=60), None)


# ---------------------------------------------------------------- output location and dates

def test_diagnostics_refuse_an_output_outside_work_before_any_computation():
    with pytest.raises(ValueError, match='inside work/runs'):
        D.run_diagnostics(ROOT, 'research/v05/diagnostics/not_allowed')


def test_every_date_in_the_published_diagnostics_is_no_later_than_the_segment_end():
    folder = ROOT / 'research/v05/diagnostics/D0'
    if not folder.exists():
        pytest.skip('diagnostics not yet published')
    pattern = re.compile(r'\b(\d{4}-\d{2}-\d{2})\b')
    seen = 0
    for path in folder.glob('*.csv'):
        for m in pattern.finditer(path.read_text(encoding='utf-8')):
            seen += 1
            assert m.group(1) <= D.DEV_END, (path.name, m.group(1))
    for m in pattern.finditer((folder / 'summary.md').read_text(encoding='utf-8')):
        seen += 1
        assert m.group(1) <= D.DEV_END, m.group(1)
    assert seen > 0


# ---------------------------------------------------------------- D3 sector attribution of CW-only names

def test_cw_only_names_take_their_sector_from_the_security_industry_and_missing_ones_are_unknown():
    mapping = D.load_sector_map(ROOT / 'config/sector_map_v1.csv')
    tri = tri_from({'A': {date(2024, 1, 5): 100.0, date(2024, 1, 12): 110.0},
                    'B': {date(2024, 1, 5): 100.0, date(2024, 1, 12): 90.0}})
    rec = week('2024-01-05', ['A', 'B'], '2024-01-08', weight={'A': 0.5, 'B': 0.5})
    nxt = week('2024-01-12', ['A', 'B'], None)
    # A is held (0.4) and in CW (0.5); B is CW-only (0.5) with no industry in either source.
    weights = {'2024-01-08': {'A': 0.4}}
    industry = {'A': 'US_LIST2015', 'B': ''}
    top, sector_rows, summary = D.active_rows('T', [rec, nxt], weights, industry, tri, mapping, {})
    assert summary['total_active_pp'] == pytest.approx(4.0)  # (0.4 - 0.5) * 0.10 + (0 - 0.5) * (-0.10)
    by = {r['sector']: r['contribution_pp'] for r in sector_rows}
    assert by['信息技术'] == pytest.approx(-1.0)
    assert by['UNKNOWN'] == pytest.approx(5.0)
    assert 'UNMAPPED' not in by
    assert summary['industry_not_found'] == 1


# ---------------------------------------------------------------- D5 stop reasons (existing runs only)

def _stop_folder(base, config, name, message, attempted, status='incomplete'):
    folder = base / 'null' / config / name / 'cost_10bps'
    folder.mkdir(parents=True)
    (folder / 'completion.json').write_text(json.dumps(dict(status=status)), encoding='utf-8')
    (folder / 'last_explainable_state.json').write_text(
        json.dumps(dict(message=message, attempted_session=attempted)), encoding='utf-8')


def test_stop_reasons_are_classified_from_the_engine_message_and_completed_runs_are_ignored(tmp_path):
    _stop_folder(tmp_path, 'L010_N08_b2_R2', 'DIAG_L010_N08_b2_R2_s001',
                 'partially delivered security requires verified fractional settlement before weekly allocation: CHILD:PHIN',
                 '2022-02-04')
    _stop_folder(tmp_path, 'L010_N08_b2_R2', 'DIAG_L010_N08_b2_R2_s002',
                 'held asset price interrupted without verified disposition: CUSIP:09062X103 2020-11-06', '2020-11-06')
    _stop_folder(tmp_path, 'L010_N08_b2_R2', 'DIAG_L010_N08_b2_R2_s003', 'unused', '2020-01-01', status='completed')
    rows = {r['configuration_id']: r for r in D.stop_rows(tmp_path)}
    assert set(rows) == {'DIAG_L010_N08_b2_R2_s001', 'DIAG_L010_N08_b2_R2_s002'}
    first = rows['DIAG_L010_N08_b2_R2_s001']
    assert first['stop_type'] == 'fractional_cash_unverified' and first['security_kind'] == 'spinoff_child'
    assert first['security_id'] == 'CHILD:PHIN' and first['date'] == '2022-02-04' and first['seed'] == 1
    second = rows['DIAG_L010_N08_b2_R2_s002']
    assert second['stop_type'] == 'held_price_interrupted' and second['security_kind'] == 'cusip'
    assert second['security_id'] == 'CUSIP:09062X103' and second['date'] == '2020-11-06'


def test_only_rejects_unknown_items_before_any_output_is_written():
    with pytest.raises(ValueError, match='unknown diagnostic item'):
        D.run_diagnostics(ROOT, 'work/runs/diagnostics/never', only=['D9'])
    assert not (ROOT / 'work/runs/diagnostics/never').exists()
