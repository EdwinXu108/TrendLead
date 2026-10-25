"""Turnover-matched random selection on synthetic weeks: exits follow the reference table; caps and issuers hold."""
from collections import Counter

from leadtrend_v05.selection import Candidate, SELECTION_RULES, draw_order, select_random_matched

N, K = 6, 2
WEEKS = ['2020-01-03', '2020-01-10', '2020-01-17', '2020-01-24', '2020-01-31']
UNIVERSE = [f'S{i:02d}' for i in range(24)]


def issuer(sid):
    return f'ISS{int(sid[1:]) // 2}'  # two securities per issuer, in different industries


def industry(sid):
    return f'IND{int(sid[1:]) % 4}'


def candidates(week_index, missing=()):
    out = []
    for i, sid in enumerate(UNIVERSE):
        if sid in missing:
            continue
        out.append(Candidate(sid, issuer(sid), industry(sid), ((i * 7 + week_index * 13) % 24) / 24))
    return out


def run_weeks(exit_counts, seed=1, missing=None):
    held, history = [], []
    for w, iso in enumerate(WEEKS):
        held_meta = {sid: (issuer(sid), industry(sid)) for sid in held}
        res = select_random_matched(candidates(w, (missing or {}).get(iso, ())), held, N, K, 0.0, signal_date=iso,
                                    seed=seed, exit_counts=exit_counts, held_meta=held_meta)
        exits = {d.security_id: d.reason for d in res.decisions if d.action == 'exit'}
        history.append((iso, exits, res))
        held = res.target_ids
    return history


def test_weekly_exits_equal_the_reference_table_for_every_week():
    reference = {WEEKS[0]: 0, WEEKS[1]: 2, WEEKS[2]: 3, WEEKS[3]: 1, WEEKS[4]: 0}
    history = run_weeks(reference)
    for iso, exits, res in history:
        assert len(exits) == reference[iso], iso
        assert set(exits.values()) <= {'random_exit'}, iso
        assert res.target_count == N and res.filled_all, iso


def test_selection_is_deterministic_and_depends_on_the_seed():
    reference = {w: 2 for w in WEEKS[1:]}
    first = [res.target_ids for _, _, res in run_weeks(reference, seed=3)]
    again = [res.target_ids for _, _, res in run_weeks(reference, seed=3)]
    other = [res.target_ids for _, _, res in run_weeks(reference, seed=4)]
    assert first == again
    assert first != other
    assert draw_order(3, WEEKS[1], 'S01') == draw_order(3, WEEKS[1], 'S01') != draw_order(4, WEEKS[1], 'S01')


def test_industry_cap_and_issuer_uniqueness_hold_every_week():
    history = run_weeks({w: 3 for w in WEEKS})
    for iso, _, res in history:
        counts = Counter(industry(sid) for sid in res.target_ids)
        issuers = [issuer(sid) for sid in res.target_ids]
        assert max(counts.values()) <= K, iso
        assert len(set(issuers)) == len(issuers), iso


def test_unscorable_held_name_leaves_as_forced_exit_and_counts_against_the_reference():
    # S05 is held from week 0; in week 2 it has no score, so its exit is forced and one random exit remains.
    reference = {WEEKS[0]: 0, WEEKS[1]: 0, WEEKS[2]: 2, WEEKS[3]: 0, WEEKS[4]: 0}
    history = run_weeks(reference, missing={WEEKS[2]: ('S05',)})
    iso, exits, res = history[2]
    assert exits.get('S05') == 'random_forced_exit'
    assert sum(1 for r in exits.values() if r == 'random_exit') == 1
    assert len(exits) == 2 and 'S05' not in res.target_ids


def test_frozen_names_hold_slots_and_their_issuers_are_not_refilled():
    frozen = [Candidate('F01', 'ISSF', 'IND1', 0.0)]  # restricted holding in industry 1
    cands = candidates(0) + [Candidate('X01', 'ISS0', 'IND2', 0.9)]  # X01 shares ISS0 with S00 and S01
    res = select_random_matched(cands, [], N, K, 0.0, frozen=frozen, signal_date=WEEKS[0], seed=1,
                                exit_counts={}, held_meta={})
    assert 'F01' not in res.target_ids
    assert len(res.target_ids) == N - 1 and res.filled_all  # the frozen name holds one slot
    assert len({'S00', 'S01', 'X01'} & set(res.target_ids)) <= 1  # one issuer, one holding
    assert sum(1 for sid in res.target_ids if industry(sid) == 'IND1') <= K - 1  # the frozen name counts toward the cap


def test_registered_rule_name_and_decision_reasons():
    assert 'random_matched' in SELECTION_RULES
    history = run_weeks({w: 2 for w in WEEKS})
    reasons = {d.reason for _, _, res in history for d in res.decisions}
    assert {'random_retained', 'random_fill', 'random_exit', 'not_selected'} <= reasons
