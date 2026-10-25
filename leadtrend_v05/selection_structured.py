"""Structured selection rules for the round-1 strategy experiments (ENT_POS, ENT_NEG, SEC3/SEC2, CAP2).

Each rule keeps select_portfolio's buffer, retention and industry/total rules, and changes one thing:
- ent_pos / ent_neg: how new buys are ordered (entry scores and the two-pass fill);
- sector_cap: a second cap, per large sector, applied in the feasible order, in retention and in the fill;
- exit_cap: at most max_rank_exits buffer-line exits per week; the rest are kept one more week.
Neutral parameters reproduce select_portfolio exactly; the stage 4 neutral check verifies that on real data.
The caller passes aux = {'sector': {security: sector key}, 'short': {security: percentile at the short window}}.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from .selection import Candidate, Decision, SelectionResult


def percentile_by_rank(ranked):
    """P = (n - i) / (n - 1) over a list already sorted best first; a lone name gets 1."""
    n = len(ranked)
    return {c.security_id: ((n - i) / (n - 1) if n > 1 else 1.0) for i, c in enumerate(ranked, 1)}


def _feasible(ranked, k, ind_caps, sector_k, sec_limit, sector):
    """Feasible order F under both layers: industry k (less frozen) and, when set, the large-sector cap."""
    used_ind, used_sec, out = {}, {}, []
    for c in ranked:
        s = sector[c.security_id]
        if used_ind.get(c.industry, 0) >= ind_caps.get(c.industry, k):
            continue
        if sector_k is not None and used_sec.get(s, 0) >= sec_limit(s):
            continue
        out.append(c)
        used_ind[c.industry] = used_ind.get(c.industry, 0) + 1
        used_sec[s] = used_sec.get(s, 0) + 1
    return out


def select_structured(candidates, held_ids, N, k, b, frozen=None, *, aux, fill='default', short_weight=0.0,
                      max_short_pct=None, sector_k=None, max_rank_exits=None):
    notes = []
    counts = {}
    frozen = frozen or []
    frozen_ids = {c.security_id for c in frozen}
    frozen_issuers = {c.issuer_id for c in frozen}
    sector = aux['sector']
    short = aux.get('short', {})
    frozen_ind = Counter(c.industry for c in frozen)
    frozen_sec = Counter(sector[c.security_id] for c in frozen)
    total_capacity = max(0, N - len(frozen_ids))
    ind_caps = {ind: max(0, k - cnt) for ind, cnt in frozen_ind.items()}

    def sec_limit(s):
        return max(0, sector_k - frozen_sec.get(s, 0)) if sector_k is not None else None

    if len(frozen_ids) > N or any(x > k for x in frozen_ind.values()) or (
            sector_k is not None and any(x > sector_k for x in frozen_sec.values())):
        notes.append('passive_frozen_limit_exceeded')
    candidates = [c for c in candidates if c.security_id not in frozen_ids and c.issuer_id not in frozen_issuers]
    ranked = sorted(candidates, key=lambda c: c.sort_key)
    rank_of = {c.security_id: i + 1 for i, c in enumerate(ranked)}
    by_id = {c.security_id: c for c in ranked}
    p90 = percentile_by_rank(ranked)
    held_set = set(held_ids) - frozen_ids
    F = _feasible(ranked, k, ind_caps, sector_k, sec_limit, sector)
    f_index = {c.security_id: i + 1 for i, c in enumerate(F)}

    pos = min(math.ceil(b * N), len(F))
    buffer_key = None
    buffer_score = buffer_id = None
    if pos <= 0:
        notes.append('feasible_order_empty_no_retention')
    else:
        buf = F[pos - 1]
        buffer_key, buffer_score, buffer_id = buf.sort_key, buf.score, buf.security_id
        notes.append(f'buffer_from_F[{pos}]:{buffer_id}')

    retain, exits = [], []
    for hid in sorted(held_set):
        c = by_id.get(hid)
        if c is None or buffer_key is None:
            continue
        if c.sort_key <= buffer_key:
            retain.append(c)
        else:
            exits.append(('below_buffer_line', c))

    if max_rank_exits is not None:
        # Worst names leave first; the rest are kept and occupy their slot until next week's check.
        below = sorted((c for r, c in exits if r == 'below_buffer_line'), key=lambda c: c.sort_key, reverse=True)
        leaving = {c.security_id for c in below[:max_rank_exits]}
        deferred = below[max_rank_exits:]
        if deferred:
            counts['cap_trigger_weeks'] = 1
            counts['cap_deferred'] = len(deferred)
            retain.extend(deferred)
        exits = [(r, c) for r, c in exits if r != 'below_buffer_line' or c.security_id in leaving]
    retain.sort(key=lambda c: c.sort_key)

    removed = set()

    def drop(c, reason):
        removed.add(c.security_id)
        exits.append((reason, c))

    ind_count = Counter(c.industry for c in retain)
    for ind in sorted(ind_count):
        cap = ind_caps.get(ind, k)
        if ind_count[ind] > cap:
            for c in [c for c in retain if c.industry == ind][cap:]:
                drop(c, 'industry_cap_overflow')
    retain = [c for c in retain if c.security_id not in removed]
    if sector_k is not None:
        sec_count = Counter(sector[c.security_id] for c in retain)
        for s in sorted(sec_count):
            limit = sec_limit(s)
            if sec_count[s] > limit:
                for c in [c for c in retain if sector[c.security_id] == s][limit:]:
                    drop(c, 'sector_cap_overflow')
        retain = [c for c in retain if c.security_id not in removed]
    if len(retain) > total_capacity:
        for c in retain[total_capacity:]:
            drop(c, 'total_cap_overflow')
        retain = retain[:total_capacity]

    keep_ids = {c.security_id for c in retain}
    fill_ind = Counter(c.industry for c in retain)
    fill_sec = Counter(sector[c.security_id] for c in retain)
    used_issuers = {c.issuer_id for c in retain}
    fills, fill_reason = [], {}

    def can_take(c):
        s = sector[c.security_id]
        if c.issuer_id in used_issuers or fill_ind.get(c.industry, 0) >= ind_caps.get(c.industry, k):
            return False
        return sector_k is None or fill_sec.get(s, 0) < sec_limit(s)

    def take(c, reason):
        fills.append(c)
        fill_reason[c.security_id] = reason
        used_issuers.add(c.issuer_id)
        fill_ind[c.industry] += 1
        fill_sec[sector[c.security_id]] += 1

    def room():
        return len(retain) + len(fills) < total_capacity

    def eligible(c):
        return c.security_id not in keep_ids and c.security_id not in held_set and c.security_id not in fill_reason

    inside = (lambda c: buffer_key is not None and c.sort_key <= buffer_key)
    if fill == 'default':
        for c in ranked:
            if not room():
                break
            if eligible(c) and can_take(c):
                take(c, 'fill_unheld_candidate')
    elif fill == 'pos':
        def entry(c):
            s = short.get(c.security_id)
            h = p90[c.security_id]
            return h if s is None else (1 - short_weight) * h + short_weight * s
        line = sorted((c for c in ranked if eligible(c) and inside(c)), key=lambda c: (-entry(c), c.sort_key))
        for c in line:
            if room() and can_take(c):
                take(c, 'entry_line_fill')
        counts['pass1_buys'] = len(fills)
        for c in F:
            if room() and eligible(c) and can_take(c):
                take(c, 'feasible_order_fill')
        counts['pass2_buys'] = len(fills) - counts['pass1_buys']
    elif fill == 'neg':
        for c in F:
            s = short.get(c.security_id)
            if room() and eligible(c) and inside(c) and can_take(c) and not (
                    max_short_pct is not None and s is not None and s > max_short_pct):
                take(c, 'line_pass_fill')
        counts['pass1_buys'] = len(fills)
        for c in F:
            if room() and eligible(c) and can_take(c):
                take(c, 'unrestricted_pass_fill')
        counts['pass2_buys'] = len(fills) - counts['pass1_buys']
    else:
        raise ValueError('unknown fill rule ' + fill)

    target = retain + fills
    target_ids = [c.security_id for c in target]
    filled_all = len(target_ids) == total_capacity
    if not filled_all:
        notes.append(f'underfilled_target={len(target_ids)}/{N}')
    final_ind = Counter(c.industry for c in target)
    retained = {c.security_id for c in retain}
    decisions = []
    for c in ranked:
        in_f = c.security_id in f_index
        if c.security_id in retained:
            action, reason = 'retain', 'retained_above_buffer'
        elif c.security_id in fill_reason:
            action, reason = 'fill', fill_reason[c.security_id]
        elif c.security_id in held_set:
            continue
        else:
            action, reason = 'skip', 'not_selected'
        decisions.append(Decision(action=action, security_id=c.security_id, issuer_id=c.issuer_id, industry=c.industry,
                                  score=c.score, reason=reason, rank=rank_of.get(c.security_id),
                                  buffer_key_score=buffer_score, buffer_key_id=buffer_id,
                                  industry_counts_after=dict(final_ind), in_feasible_order=in_f,
                                  feasible_index=f_index.get(c.security_id)))
    for reason, c in exits:
        decisions.append(Decision(action='exit', security_id=c.security_id, issuer_id=c.issuer_id, industry=c.industry,
                                  score=c.score, reason=reason, rank=rank_of.get(c.security_id),
                                  buffer_key_score=buffer_score, buffer_key_id=buffer_id,
                                  in_feasible_order=c.security_id in f_index, feasible_index=f_index.get(c.security_id)))
    decisions.sort(key=lambda d: (d.security_id, d.action))
    return SelectionResult(target_ids=target_ids, decisions=decisions, buffer_score=buffer_score,
                           buffer_security_id=buffer_id, feasible_order=[c.security_id for c in F],
                           filled_all=filled_all, target_count=len(target_ids), notes=notes, counts=counts)


def _rule(**preset):
    def run(candidates, held_ids, N, k, b, frozen=None, *, aux, **params):
        for name in ('short_window', 'signal_date', 'held_meta'):
            params.pop(name, None)  # short_window is consumed by the caller, which builds aux['short']
        return select_structured(candidates, held_ids, N, k, b, frozen, aux=aux, **dict(preset, **params))
    return run


STRUCTURED_RULES = {
    'ent_pos': _rule(fill='pos', short_weight=0.25),
    'ent_neg': _rule(fill='neg', max_short_pct=0.80),
    'sector_cap': _rule(sector_k=3),
    'exit_cap': _rule(max_rank_exits=2),
}

# Parameters each rule accepts beyond the common arguments; stage3 uses these to decide what aux it must build.
RULE_PARAMS = {
    'ent_pos': {'short_window': 20, 'short_weight': 0.25},
    'ent_neg': {'short_window': 20, 'max_short_pct': 0.80},
    'sector_cap': {'sector_k': 3},
    'exit_cap': {'max_rank_exits': 2},
}
