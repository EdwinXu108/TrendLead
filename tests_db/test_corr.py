"""Unit tests of the descriptive correlation grouping (synthetic data only)."""
from __future__ import annotations

import numpy as np
import pytest

from leadtrend_db import corr


def session_dates(n, start='2019-01-01'):
    from datetime import date, timedelta
    day = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def synthetic(seed=0, groups=3, size=4, days=300):
    """Three blocks of co-moving securities plus a common market factor."""
    rng = np.random.default_rng(seed)
    market = rng.normal(0, 0.01, days)
    ids, data = [], {}
    for g in range(groups):
        shock = rng.normal(0, 0.01, days)
        for i in range(size):
            sid = f'G{g}S{i}'
            ids.append(sid)
            data[sid] = 0.8 * market + 0.6 * shock + rng.normal(0, 0.002, days)
    return ids, data, market


def test_average_linkage_recovers_known_blocks():
    ids, data, _ = synthetic(groups=3, size=8)
    sessions = session_dates(len(data[ids[0]]))
    returns_by_day = {day: {sid: float(data[sid][i]) for sid in ids} for i, day in enumerate(sessions)}
    params = dict(corr.PARAMS, groups=3)
    groups, info = corr.group_matrix(returns_by_day, sessions, ids, params)
    assert info['kept'] == len(ids)
    blocks = {}
    for sid, group in groups.items():
        blocks.setdefault(group, set()).add(sid[:2])
    assert len(blocks) == 3
    assert all(len(v) == 1 for v in blocks.values()), 'each group must contain exactly one synthetic block'


def test_short_history_is_dropped_not_grouped():
    ids, data, _ = synthetic()
    sessions = session_dates(20)
    returns_by_day = {day: {sid: float(data[sid][i]) for sid in ids} for i, day in enumerate(sessions)}
    groups, info = corr.group_matrix(returns_by_day, sessions, ids)
    assert groups == {}
    assert info['kept'] == 0


def test_group_labels_are_ordered_by_size_then_id():
    clusters = [[5, 6], [0], [1, 2, 3]]
    ids = [f'S{i}' for i in range(7)]
    labels = corr.label_groups(clusters, ids)
    assert labels == {'S1': 1, 'S2': 1, 'S3': 1, 'S5': 2, 'S6': 2, 'S0': 3}


def test_adjusted_rand_index_bounds_and_invariance():
    a = [0, 0, 1, 1, 2, 2]
    assert corr.adjusted_rand_index(a, a) == pytest.approx(1.0)
    assert corr.adjusted_rand_index(a, [7, 7, 9, 9, 4, 4]) == pytest.approx(1.0)
    assert corr.adjusted_rand_index(a, [0, 1, 0, 1, 2, 2]) < 0.7


def test_clustering_uses_only_the_sessions_it_is_given():
    """Feeding the same sessions twice must give the same answer; later sessions cannot enter the matrix."""
    ids, data, _ = synthetic(seed=3, groups=3, size=8)
    sessions = session_dates(len(data[ids[0]]), '2020-06-01')
    base = {day: {sid: float(data[sid][i]) for sid in ids} for i, day in enumerate(sessions)}
    params = dict(corr.PARAMS, groups=3)
    first, _ = corr.group_matrix(base, sessions, ids, params)
    extra = dict(base)
    extra['2030-01-01'] = {sid: 1.0 for sid in ids}
    second, _ = corr.group_matrix(extra, sessions, ids, params)
    assert first == second


def test_correlation_matrix_handles_zero_variance_columns():
    x = np.array([[1.0, 5.0], [2.0, 5.0], [3.0, 5.0]])
    m = corr.correlation_matrix(x)
    assert m[0, 0] == pytest.approx(1.0)
    assert m[0, 1] == pytest.approx(0.0)
