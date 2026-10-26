"""Descriptive correlation grouping of the pool (never a trading input).

At each pool version's effective date T the eligible securities are grouped from their own past daily total returns:
504 NYSE sessions ending the session before T, at least 252 observed days, each day demeaned by the equal-weight
average of that day's eligible securities, distance = 1 - correlation, average-linkage hierarchical clustering into
12 groups. Only data strictly before T is read; the test suite checks that.
"""
from __future__ import annotations
import json
import math
import warnings

import numpy as np

METHOD_ID = 'corr_v1'
PARAMS = dict(window=504, min_days=252, demean='equal_weight_of_eligible_securities', distance='1-correlation',
              linkage='average', groups=12, missing='filled_with_cross_sectional_mean')
DEFINITION = json.dumps(PARAMS, sort_keys=True)


def correlation_matrix(returns: np.ndarray) -> np.ndarray:
    """Pearson correlation of columns; a zero-variance column is uncorrelated with everything (diagonal 1)."""
    n = returns.shape[1]
    if n == 0:
        return np.zeros((0, 0))
    std = returns.std(axis=0)
    safe = np.where(std > 0, std, 1.0)
    z = (returns - returns.mean(axis=0)) / safe
    corr = (z.T @ z) / max(1, returns.shape[0])
    corr = np.clip(corr, -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)
    corr[std <= 0, :] = 0.0
    corr[:, std <= 0] = 0.0
    np.fill_diagonal(corr, 1.0)
    return corr


def average_linkage(distance: np.ndarray, k: int) -> list[int]:
    """Average-linkage agglomeration until k clusters remain. Deterministic tie-break by (distance, min, max) index."""
    n = distance.shape[0]
    if n == 0:
        return []
    d = np.array(distance, dtype=float, copy=True)
    np.fill_diagonal(d, np.inf)
    members = {i: [i] for i in range(n)}
    active = list(range(n))
    while len(active) > k:
        best = None
        for a in range(len(active)):
            for b in range(a + 1, len(active)):
                i, j = active[a], active[b]
                key = (d[i, j], min(i, j), max(i, j))
                if best is None or key < best[0]:
                    best = (key, i, j)
        _, i, j = best
        size_i, size_j = len(members[i]), len(members[j])
        for c in active:
            if c in (i, j):
                continue
            new = (d[i, c] * size_i + d[j, c] * size_j) / (size_i + size_j)
            d[i, c] = d[c, i] = new
        members[i] = members[i] + members.pop(j)
        active.remove(j)
    return [sorted(members[c]) for c in active]


def label_groups(clusters: list[list[int]], ids: list[str]) -> dict[str, int]:
    """Group ids 1..k ordered by size (largest first) then by the smallest member id, so labels are reproducible."""
    ordered = sorted(clusters, key=lambda c: (-len(c), min(ids[i] for i in c)))
    out = {}
    for group, members in enumerate(ordered, 1):
        for i in members:
            out[ids[i]] = group
    return out


def adjusted_rand_index(a: list[int], b: list[int]) -> float:
    """Adjusted Rand index of two partitions given as label lists of equal length."""
    if len(a) != len(b):
        raise ValueError('partitions must have the same length')
    n = len(a)
    if n < 2:
        return 1.0
    from collections import Counter
    cont = Counter(zip(a, b))
    rows, cols = Counter(a), Counter(b)
    comb = lambda x: x * (x - 1) / 2
    index = sum(comb(v) for v in cont.values())
    sum_rows = sum(comb(v) for v in rows.values())
    sum_cols = sum(comb(v) for v in cols.values())
    expected = sum_rows * sum_cols / comb(n)
    maximum = (sum_rows + sum_cols) / 2
    if maximum == expected:
        return 1.0
    return (index - expected) / (maximum - expected)


def group_matrix(returns_by_day: dict[str, dict[str, float]], sessions: list[str], ids: list[str],
                 params: dict = PARAMS) -> tuple[dict[str, int], dict]:
    """Cluster the securities from the given sessions. returns_by_day: date -> security -> return."""
    n = len(ids)
    matrix = np.full((len(sessions), n), np.nan)
    for r, day in enumerate(sessions):
        row = returns_by_day.get(day, {})
        for c, sid in enumerate(ids):
            if sid in row:
                matrix[r, c] = row[sid]
    observed = np.sum(~np.isnan(matrix), axis=0)
    keep = [c for c in range(n) if observed[c] >= params['min_days']]
    info = dict(securities=len(ids), kept=len(keep), dropped=len(ids) - len(keep), sessions=len(sessions))
    if len(keep) < params['groups']:
        return {}, info
    sub = matrix[:, keep]
    with np.errstate(invalid='ignore'), warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        market = np.nanmean(sub, axis=1)
    market = np.where(np.isnan(market), 0.0, market)
    filled = sub - market[:, None]
    filled = np.where(np.isnan(filled), 0.0, filled)
    corr = correlation_matrix(filled)
    distance = np.clip(1.0 - corr, 0.0, 2.0)
    clusters = average_linkage(distance, params['groups'])
    kept_ids = [ids[c] for c in keep]
    groups = label_groups(clusters, kept_ids)
    return groups, info


def build(t: dict, tri_rows: list[dict], calendar: list[str], log=print):
    """corr_methods / corr_groups rows for every pool version that becomes effective on a session."""
    import bisect
    from .price_data import active_start
    by_sid: dict[str, dict[str, float]] = {}
    for row in tri_rows:
        if row['daily_return'] is None:
            continue
        by_sid.setdefault(row['security_id'], {})[row['session_date']] = row['daily_return']
    timeline = []
    for v in sorted(t['pool_versions'], key=lambda r: (r['available_at_utc'], r['version_id'])):
        if not timeline or v['report_period'] >= timeline[-1]['report_period']:
            timeline.append(v)
    eligible: dict[str, list[str]] = {}
    for r in t['strategy_securities']:
        if r['strategy_eligible']:
            eligible.setdefault(r['version_id'], []).append(r['security_id'])
    out_groups, stats = [], dict(versions=0, grouped=0, securities=0, dropped=0)
    for v in timeline:
        first = active_start(v['available_at_utc'], calendar)
        if not first:
            continue
        i = bisect.bisect_left(calendar, first)
        sessions = calendar[max(0, i - PARAMS['window']):i]
        ids = sorted(set(eligible.get(v['version_id'], [])))
        if not sessions or len(ids) < PARAMS['groups']:
            continue
        subset = {sid: by_sid.get(sid, {}) for sid in ids}
        returns_by_day = {day: {sid: subset[sid][day] for sid in ids if day in subset[sid]} for day in sessions}
        groups, info = group_matrix(returns_by_day, sessions, ids)
        stats['versions'] += 1
        stats['securities'] += info['kept']
        stats['dropped'] += info['dropped']
        for sid, group in sorted(groups.items()):
            out_groups.append(dict(version_id=v['version_id'], method_id=METHOD_ID, security_id=sid, group_id=group))
        stats['grouped'] += 1
    log(f'corr_groups: {stats["grouped"]} versions, {len(out_groups)} rows')
    return [dict(method_id=METHOD_ID, definition=DEFINITION)], out_groups, stats
