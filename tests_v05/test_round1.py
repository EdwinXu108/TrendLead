"""Round 1 engineering: structured selection rules, sector caps in random controls, grid identity, overlay signals."""
import math
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from leadtrend_v05 import experiments
from leadtrend_v05.experiments import attempt_key, expand_grid, make_point
from leadtrend_v05.overlay_signals import daily_log_volatility, peer, sector_demeaned, size_tilt, vol_adjusted, voo_weight
from leadtrend_v05.sectors import sector_key
from leadtrend_v05.selection import Candidate, select_portfolio, select_random_matched
from leadtrend_v05.selection_structured import select_structured
from leadtrend_v05.signals import main_L, normalize, signal_key


def _universe(seed, n=30, industries=6, sectors=3):
    rng = random.Random(seed)
    cands, sector = [], {}
    for i in range(n):
        sid = f'S{i:02d}'
        ind = f'I{rng.randrange(industries)}'
        cands.append(Candidate(sid, f'ISS{i:02d}', ind, round(rng.uniform(-1, 1), 6)))
        sector[sid] = f'SEC{int(ind[1:]) % sectors}'
    return cands, sector


def _same(a, b):
    return a.target_ids == b.target_ids and [(d.security_id, d.action, d.reason) for d in a.decisions] == \
        [(d.security_id, d.action, d.reason) for d in b.decisions]


@pytest.mark.parametrize('seed', range(12))
def test_neutral_structured_rule_reproduces_select_portfolio(seed):
    cands, sector = _universe(seed)
    rng = random.Random(100 + seed)
    held = sorted(rng.sample([c.security_id for c in cands], 8))
    frozen = [c for c in cands if c.security_id in held[:1]][:1]
    held_rest = [h for h in held if h not in {f.security_id for f in frozen}]
    base = select_portfolio(cands, held_rest, 10, 3, 2, frozen=frozen)
    got = select_structured(cands, held_rest, 10, 3, 2, frozen=frozen, aux={'sector': sector})
    assert _same(base, got)


def test_sector_cap_never_exceeds_the_large_sector_limit():
    # Five sectors at two names each fill ten slots; four sectors at three each fill ten as well.
    for sectors, k_sector in ((5, 2), (4, 3)):
        cands, sector = _universe(7, n=60, industries=9, sectors=sectors)
        res = select_structured(cands, [], 10, 3, 2, aux={'sector': sector}, sector_k=k_sector)
        counts = {}
        for sid in res.target_ids:
            counts[sector[sid]] = counts.get(sector[sid], 0) + 1
        assert max(counts.values()) <= k_sector and len(res.target_ids) == 10


def test_sector_cap_counts_frozen_names_first():
    cands, sector = _universe(3, n=40, industries=8, sectors=3)
    frozen_c = [c for c in cands if sector[c.security_id] == 'SEC0'][:2]
    frozen_c = [Candidate(c.security_id, c.issuer_id + 'F', c.industry, 0.) for c in frozen_c]
    res = select_structured(cands, [], 10, 3, 2, frozen=frozen_c, aux={'sector': sector}, sector_k=3)
    sec0 = sum(1 for sid in res.target_ids if sector[sid] == 'SEC0')
    assert sec0 <= 1 and len(res.target_ids) <= 8


def test_exit_cap_lets_the_worst_names_go_and_keeps_the_rest_one_more_week():
    # N=4, b=1: the buffer line is the 4th ranked name, so the held G4..G9 are all below it.
    cands = [Candidate(f'G{i}', f'G{i}', f'I{i}', 1.0 - i * 0.01) for i in range(10)]
    held = ['G4', 'G5', 'G6', 'G7', 'G8', 'G9']
    sector = {c.security_id: 'X' for c in cands}
    capped = select_structured(cands, held, 4, 10, 1, aux={'sector': sector}, max_rank_exits=2)
    below = {d.security_id for d in capped.decisions if d.action == 'exit' and d.reason == 'below_buffer_line'}
    assert below == {'G8', 'G9'}  # the two worst names leave first
    assert capped.counts == {'cap_trigger_weeks': 1, 'cap_deferred': 4}
    assert {'G4', 'G5', 'G6', 'G7'} <= set(capped.target_ids)
    uncapped = select_structured(cands, held, 4, 10, 1, aux={'sector': sector})
    assert {d.security_id for d in uncapped.decisions if d.action == 'exit' and d.reason == 'below_buffer_line'} == set(held)


def test_entry_rule_orders_new_buys_by_blended_score():
    # N=4, b=2: every name is inside the line. E = 0.5 * P90 + 0.5 * P20 gives E0, E5, E2, E3 in that order.
    cands = [Candidate(f'E{i}', f'E{i}', f'I{i}', 1.0 - i * 0.05) for i in range(8)]
    sector = {c.security_id: 'X' for c in cands}
    short = {'E0': 0.9, 'E1': 0.1, 'E2': 0.5, 'E3': 0.5, 'E4': 0.0, 'E5': 1.0, 'E6': 0.2, 'E7': 0.3}
    res = select_structured(cands, [], 4, 4, 2, aux={'sector': sector, 'short': short}, fill='pos', short_weight=0.5)
    assert res.target_ids == ['E0', 'E5', 'E2', 'E3']
    assert res.counts == {'pass1_buys': 4, 'pass2_buys': 0}


def test_second_pass_entry_rule_admits_names_the_first_pass_skipped():
    cands = [Candidate(f'F{i}', f'F{i}', f'I{i}', 1.0 - i * 0.05) for i in range(8)]
    sector = {c.security_id: 'X' for c in cands}
    short = {'F0': 0.95, 'F1': 0.95, 'F2': 0.95, 'F3': 0.95}
    res = select_structured(cands, [], 4, 4, 1, aux={'sector': sector, 'short': short}, fill='neg', max_short_pct=0.80)
    assert res.counts['pass1_buys'] == 0 and res.counts['pass2_buys'] == 4
    assert res.target_ids == ['F0', 'F1', 'F2', 'F3']


def test_random_control_with_sector_cap_respects_the_cap():
    cands, sector = _universe(11, n=50, industries=10, sectors=5)
    res = select_random_matched(cands, [], 10, 3, 2, signal_date='2020-01-03', seed=4, exit_counts={},
                                held_meta={}, aux={'sector': sector}, sector_k=2)
    counts = {}
    for sid in res.target_ids:
        counts[sector[sid]] = counts.get(sector[sid], 0) + 1
    assert max(counts.values()) <= 2 and len(res.target_ids) == 10


def test_selection_rule_and_signal_change_the_configuration_identity():
    base = make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2')
    entry = make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2', 'ent_pos', {'short_weight': 0.25})
    entry0 = make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2', 'ent_pos', {'short_weight': 0})
    assert base['id'] == 'L090_N10_b2_R2' and 'selection' not in base
    assert entry['id'] != entry0['id'] and entry['id'].startswith('L090_N10_b2_R2_ent_pos_')
    assert attempt_key(base) != attempt_key(entry)
    again = make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2', 'ent_pos', {'short_weight': 0.25})
    assert again['id'] == entry['id']


def test_grid_rejects_unknown_rules_and_parameters():
    with pytest.raises(ValueError):
        make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2', 'no_such_rule', {})
    with pytest.raises(ValueError):
        make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2', 'exit_cap', {'short_weight': 0.1})
    with pytest.raises(ValueError):
        make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2', 'random_matched', {'seed': 1})


def test_explicit_points_block_keeps_each_anchor():
    blocks = [{'points': [
        {'signal': {'kind': 'momentum', 'L': 90, 'skip': 0}, 'N': 10, 'b': 2, 'rebalance': 'R2',
         'selection': 'exit_cap', 'selection_params': {'max_rank_exits': 2}, 'anchor': 'L090_N10_b2_R2'},
        {'signal': {'kind': 'momentum', 'L': 60, 'skip': 0}, 'N': 10, 'b': 2, 'rebalance': 'R2', 'anchor': 'L060_N10_b2_R2'}]}]
    grid = expand_grid(blocks)
    assert [p['anchor'] for p in grid] == ['L090_N10_b2_R2', 'L060_N10_b2_R2']


def test_sector_key_keeps_unmapped_and_unknown_names_apart():
    mapping = {'US_LIST2512': '金融', 'UNKNOWN:*': 'UNKNOWN'}
    assert sector_key('US_LIST2512', mapping) == ('金融', True)
    assert sector_key('UNKNOWN:CUSIP:1', mapping) == ('UNKNOWN:CUSIP:1', False)
    assert sector_key('NEVER_SEEN', mapping) == ('NEVER_SEEN', False)


def test_peer_score_blends_only_with_three_or_more_peers():
    cells90 = {f'P{i}': (1.0 - i * 0.1, 1, 1, 90, '') for i in range(4)}
    cells60 = {f'P{i}': (0.5 - i * 0.1, 1, 1, 60, '') for i in range(4)}
    same = {s: ('SEC', True) for s in cells90}
    out = peer(cells90, cells60, same)
    # P90: 1, 2/3, 1/3, 0. P0's peers P1..P3 have P60 2/3, 1/3, 0, so their median is 1/3.
    assert out['P0'][0] == pytest.approx(0.75 * 1.0 + 0.25 * (1 / 3))
    few = peer({'A': cells90['P0'], 'B': cells90['P1']}, {'A': cells60['P0'], 'B': cells60['P1']},
               {'A': ('SEC', True), 'B': ('SEC', True)})
    assert few['A'][0] == pytest.approx(1.0) and few['B'][0] == pytest.approx(0.0)


def test_sector_demeaned_falls_back_to_the_universe_median_for_small_sectors():
    cells = {'A': (0.3, 1, 1, 90, ''), 'B': (0.1, 1, 1, 90, ''), 'C': (-0.2, 1, 1, 90, '')}
    sectors = {'A': ('X', True), 'B': ('Y', True), 'C': ('Z', True)}
    out = sector_demeaned(cells, sectors)
    assert out['A'][0] == pytest.approx(0.3 - 0.1) and out['B'][0] == pytest.approx(0.1 - 0.1)


def test_size_and_weight_overlays_use_summed_issuer_weights():
    cells = {'A': (0.2, 1, 1, 90, ''), 'B': (0.1, 1, 1, 90, ''), 'C': (0.0, 1, 1, 90, '')}
    weights = {'A': 0.02, 'B': 0.05, 'C': 0.01}
    assert voo_weight(cells, weights)['B'][0] == pytest.approx(0.05)
    sized = size_tilt(cells, weights)
    assert sized['B'][0] == pytest.approx(0.9 * (1 / 2) + 0.1 * 1.0)


def test_vol_adjusted_divides_by_sample_volatility_and_flags_flat_series():
    cells = {'A': (0.2, 1, 1, 90, ''), 'B': (0.2, 1, 1, 90, '')}
    out = vol_adjusted(cells, {'A': 0.1, 'B': None})
    assert out['A'][0] == pytest.approx(2.0) and out['B'][0] is None and out['B'][4] == 'vol_unavailable'


def test_daily_volatility_uses_sixty_log_returns():
    sessions = [date(2020, 1, 1) + timedelta(days=i) for i in range(70)]
    constant = {d: 100.0 * (1.01 ** i) for i, d in enumerate(sessions)}
    assert daily_log_volatility(constant, sessions, 69) is None  # zero dispersion is unavailable
    zigzag = {d: 100.0 * (1.0 + (0.01 if i % 2 else -0.01)) for i, d in enumerate(sessions)}
    sigma = daily_log_volatility(zigzag, sessions, 69)
    logs = [math.log(zigzag[sessions[j]] / zigzag[sessions[j - 1]]) for j in range(10, 70)]
    mean = sum(logs) / len(logs)
    assert sigma == pytest.approx((sum((r - mean) ** 2 for r in logs) / (len(logs) - 1)) ** 0.5)
    assert daily_log_volatility(zigzag, sessions, 30) is None  # fewer than 60 returns before the date


def test_new_signal_kinds_normalize_and_keep_their_main_window():
    for sig, expect in [({'kind': 'peer', 'L': 90}, 90), ({'kind': 'sector_demeaned', 'L': 90}, 90),
                        ({'kind': 'vol_adjusted', 'L': 90}, 90), ({'kind': 'size_tilt', 'L': 90}, 90),
                        ({'kind': 'voo_weight', 'L': 90}, 90)]:
        assert main_L(sig) == expect
        assert normalize(sig)['kind'] == sig['kind']
    assert signal_key({'kind': 'peer', 'L': 90}) != signal_key({'kind': 'size_tilt', 'L': 90})


def test_overlay_weights_are_explicit_signal_parameters_with_the_reviewed_defaults():
    peer_default = normalize({'kind': 'peer', 'L': 90})
    assert peer_default['weight'] == 0.25 and peer_default['min_peers'] == 3 and peer_default['L2'] == 60
    assert normalize({'kind': 'size_tilt', 'L': 90})['weight'] == 0.1
    assert signal_key({'kind': 'size_tilt', 'L': 90}) == signal_key({'kind': 'size_tilt', 'L': 90, 'weight': 0.1})
    assert signal_key({'kind': 'size_tilt', 'L': 90}) != signal_key({'kind': 'size_tilt', 'L': 90, 'weight': 0.2})
    with pytest.raises(ValueError):
        normalize({'kind': 'size_tilt', 'L': 90, 'weight': 1.0})
    with pytest.raises(ValueError):
        normalize({'kind': 'peer', 'L': 90, 'min_peers': 0})


def test_pair_table_reports_a_minus_b_and_marks_missing_pairs():
    from leadtrend_v05.experiments import pair_rows
    points = [{'id': 'X1', 'name': 'ENT_NEG'}, {'id': 'X2', 'name': 'ENT_POS'}]
    m10 = {'X1': dict(annualized_excess=0.05, high_missing_excess=0.02, lower_missing_excess=0.01),
           'X2': dict(annualized_excess=0.03, high_missing_excess=0.01, lower_missing_excess=0.02)}
    m5 = {'X1': dict(annualized_excess=0.04), 'X2': dict(annualized_excess=0.01)}
    rows = {r['pair']: r for r in pair_rows(points, m10, m5)}
    assert abs(rows['ENT_NEG-ENT_POS']['diff_10bps'] - 0.02) < 1e-12 and abs(rows['ENT_NEG-ENT_POS']['diff_5bps'] - 0.03) < 1e-12
    assert rows['SEC2-SEC3']['diff_10bps'] == ''
