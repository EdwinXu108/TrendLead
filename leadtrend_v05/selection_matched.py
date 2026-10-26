"""Exposure-matched random controls (plan 9.3(1)): size-matched and sector-matched.

Both keep the turnover-matched random control's exits and retention exactly as select_random_matched does (same
reference exit counts, same draw order, same industry and sector caps, same issuer rule). They differ only in the
fills. Each name the reference configuration bought in a week is replaced by a random admissible name from the same
group: the VOO-weight quintile of the week's scored pool (size) or the same mapped sector (sector). A group with no
admissible name hands the buy to the nearest other group in its order and the count is recorded; if the holding is
still short of capacity, the rest is filled uniformly in draw order and that count is recorded too.

The group of every pool name, and which names the reference bought, come from a match table written beforehand
(one row per scored pool name per signal week: signal_session_date, security_id, group, buy). The engine loads it
into aux['match'] for each week. The default rule and select_random_matched are not changed.
"""
import csv
from collections import Counter
from functools import lru_cache

from .selection import Candidate, Decision, SelectionResult, draw_order  # noqa: F401  (Candidate kept for callers)

MATCHED_RULES = ('random_size_matched', 'random_sector_matched')


@lru_cache(maxsize=4)
def load_match_table(path):
    """{signal date: {'groups': {security: group}, 'buys': [security, ...]}} from a match file."""
    table = {}
    with open(path, encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            day = table.setdefault(r['signal_session_date'], {'groups': {}, 'buys': []})
            day['groups'][r['security_id']] = r['group']
            if r['buy'] == '1':
                day['buys'].append(r['security_id'])
    return table


def _group_order(mode, pool_groups):
    """Size groups run from the highest-weight quintile ('1') down; sector groups by pool size, then label."""
    if mode == 'size':
        return sorted(pool_groups, key=lambda g: int(g))
    return sorted(pool_groups, key=lambda g: (-len(pool_groups[g]), g))


def _neighbours(order, i, dist):
    """Groups at distance dist from position i in order, lower position first."""
    if dist == 0:
        return [order[i]]
    out = []
    if i - dist >= 0:
        out.append(order[i - dist])
    if i + dist < len(order):
        out.append(order[i + dist])
    return out


def _select(candidates, held_ids, N, k, b, frozen, *, signal_date, seed, exit_counts, held_meta, aux, sector_k, mode,
            match_file):
    if aux is None or aux.get('match') is None:
        raise ValueError('matched random rules need the week of the match table (aux["match"])')
    match = aux['match']
    notes = []
    frozen = frozen or []
    frozen_ids = {c.security_id for c in frozen}
    frozen_issuers = {c.issuer_id for c in frozen}
    frozen_counts = {}
    for c in frozen:
        frozen_counts[c.industry] = frozen_counts.get(c.industry, 0) + 1
    total_capacity = max(0, N - len(frozen_ids))
    caps = {ind: max(0, k - count) for ind, count in frozen_counts.items()}
    if len(frozen_ids) > N or any(x > k for x in frozen_counts.values()):
        notes.append('passive_frozen_limit_exceeded')
    candidates = [c for c in candidates if c.security_id not in frozen_ids and c.issuer_id not in frozen_issuers]
    by_id = {c.security_id: c for c in candidates}
    held_set = set(held_ids) - frozen_ids

    sector = aux['sector'] if sector_k is not None else {}
    sec_frozen = Counter(sector[c.security_id] for c in frozen) if sector_k is not None else Counter()

    def cap_of(industry):
        return caps.get(industry, k)

    def sec_room(sid, counts):
        return sector_k is None or counts.get(sector[sid], 0) < sector_k - sec_frozen.get(sector[sid], 0)

    def draw(sid):
        return draw_order(seed, signal_date, sid)

    ranked = sorted(candidates, key=lambda c: c.sort_key)
    rank_of = {c.security_id: i + 1 for i, c in enumerate(ranked)}
    unscorable = sorted(sid for sid in held_set if sid not in by_id)
    scorable_held = sorted((sid for sid in held_set if sid in by_id), key=draw)
    n_random = max(0, exit_counts.get(signal_date, 0) - len(unscorable))
    random_exit = scorable_held[:n_random]
    retain_ids = scorable_held[n_random:]

    overflow = []
    counts = {}
    sec_counts = {}
    kept = []
    for sid in retain_ids:
        ind = by_id[sid].industry
        if counts.get(ind, 0) < cap_of(ind) and sec_room(sid, sec_counts):
            kept.append(sid)
            counts[ind] = counts.get(ind, 0) + 1
            if sector_k is not None:
                sec_counts[sector[sid]] = sec_counts.get(sector[sid], 0) + 1
        else:
            overflow.append(sid)
    if len(kept) > total_capacity:
        overflow.extend(kept[total_capacity:])
        kept = kept[:total_capacity]
    retain = [by_id[sid] for sid in kept]

    used_issuers = {c.issuer_id for c in retain}
    fill_counts = {}
    fill_sec = {}
    for c in retain:
        fill_counts[c.industry] = fill_counts.get(c.industry, 0) + 1
        if sector_k is not None:
            fill_sec[sector[c.security_id]] = fill_sec.get(sector[c.security_id], 0) + 1

    def admissible(c):
        return (c.issuer_id not in used_issuers and fill_counts.get(c.industry, 0) < cap_of(c.industry)
                and sec_room(c.security_id, fill_sec))

    fills = []

    def take(c):
        fills.append(c)
        used_issuers.add(c.issuer_id)
        fill_counts[c.industry] = fill_counts.get(c.industry, 0) + 1
        if sector_k is not None:
            fill_sec[sector[c.security_id]] = fill_sec.get(sector[c.security_id], 0) + 1

    groups = match['groups']
    pool_groups = {}
    for c in candidates:
        if c.security_id in held_set:  # held names, including those that exited this week, are not refilled
            continue
        g = groups.get(c.security_id)
        if g is not None:
            pool_groups.setdefault(g, []).append(c)
    for g in pool_groups:
        pool_groups[g].sort(key=lambda c: draw(c.security_id))
    order = _group_order(mode, pool_groups)
    pos = {g: i for i, g in enumerate(order)}
    stats = Counter()
    for sid in match['buys']:
        if len(retain) + len(fills) >= total_capacity:
            break
        g = groups.get(sid)
        if g is None or g not in pos:
            stats['match_unknown_group'] += 1
            continue
        placed = False
        for dist in range(len(order)):
            for gg in _neighbours(order, pos[g], dist):
                chosen = next((c for c in pool_groups.get(gg, []) if admissible(c)), None)
                if chosen is not None:
                    take(chosen)
                    stats['match_hit' if dist == 0 else 'match_adjacent'] += 1
                    placed = True
                    break
            if placed:
                break
        if not placed:
            stats['match_unfilled'] += 1
    for c in sorted(candidates, key=lambda c: draw(c.security_id)):
        if len(retain) + len(fills) >= total_capacity:
            break
        if c.security_id in held_set or not admissible(c):
            continue
        take(c)
        stats['match_uniform_fill'] += 1

    target = retain + fills
    target_ids = [c.security_id for c in target]
    filled_all = len(target_ids) == total_capacity
    if not filled_all:
        notes.append(f"underfilled_target={len(target_ids)}/{N}")
    notes.append(f"random_matched_exits={len(random_exit)}")

    final_ind = {}
    for c in target:
        final_ind[c.industry] = final_ind.get(c.industry, 0) + 1
    retained = set(kept)
    fill_ids = {c.security_id for c in fills}

    def exit_decision(sid, reason):
        c = by_id.get(sid)
        issuer, industry = (c.issuer_id, c.industry) if c else held_meta[sid]
        return Decision(action='exit', security_id=sid, issuer_id=issuer, industry=industry,
                        score=c.score if c else None, reason=reason, rank=rank_of.get(sid))

    exits = {sid: 'random_exit' for sid in random_exit}
    exits.update({sid: 'random_forced_exit' for sid in overflow})
    decisions = []
    for c in ranked:
        if c.security_id in exits:
            decisions.append(exit_decision(c.security_id, exits[c.security_id]))
            continue
        if c.security_id in retained:
            action, reason = 'retain', 'random_retained'
        elif c.security_id in fill_ids:
            action, reason = 'fill', 'random_fill'
        else:
            action, reason = 'skip', 'not_selected'
        decisions.append(Decision(action=action, security_id=c.security_id, issuer_id=c.issuer_id, industry=c.industry,
                                  score=c.score, reason=reason, rank=rank_of.get(c.security_id),
                                  industry_counts_after=dict(final_ind), in_feasible_order=False))
    for sid in unscorable:
        decisions.append(exit_decision(sid, 'random_forced_exit'))
    decisions.sort(key=lambda d: (d.security_id, d.action))

    return SelectionResult(target_ids=target_ids, decisions=decisions, buffer_score=None, buffer_security_id=None,
                           feasible_order=[], filled_all=filled_all, target_count=len(target_ids), notes=notes,
                           counts=dict(stats))


def select_random_size_matched(candidates, held_ids, N, k, b, frozen=None, *, signal_date, seed, exit_counts,
                               held_meta, aux=None, sector_k=None, match_file=None):
    """Size-matched random control: each reference buy is replaced by a draw from the same VOO-weight quintile."""
    return _select(candidates, held_ids, N, k, b, frozen, signal_date=signal_date, seed=seed, exit_counts=exit_counts,
                   held_meta=held_meta, aux=aux, sector_k=sector_k, mode='size', match_file=match_file)


def select_random_sector_matched(candidates, held_ids, N, k, b, frozen=None, *, signal_date, seed, exit_counts,
                                 held_meta, aux=None, sector_k=None, match_file=None):
    """Sector-matched random control: each reference buy is replaced by a draw from the same mapped sector."""
    return _select(candidates, held_ids, N, k, b, frozen, signal_date=signal_date, seed=seed, exit_counts=exit_counts,
                   held_meta=held_meta, aux=aux, sector_k=sector_k, mode='sector', match_file=match_file)


MATCHED_RULE_FUNCTIONS = {'random_size_matched': select_random_size_matched,
                          'random_sector_matched': select_random_sector_matched}
