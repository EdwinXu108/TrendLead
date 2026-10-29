"""The holdout switch: the authorization's batch, and the cutoff that stays in place by default."""
from __future__ import annotations

import json
import sqlite3

import pytest

from leadtrend_bt import boll_weekly, ma_lab, ma_pool


def _authorization(tmp_path, scope):
    payload = dict(authorized_by='investor', date='2026-10-30', scope=scope,
                   window=['2024-07-01', '2026-06-30'])
    path = tmp_path / 'authorization.json'
    path.write_text(json.dumps(payload), encoding='utf8', newline='\n')
    return path


def _database(tmp_path):
    """A stand-in for the released database: check_holdout only reads the release marker."""
    path = tmp_path / 'released.sqlite'
    conn = sqlite3.connect(path)
    conn.execute('create table meta (key text primary key, value text)')
    conn.execute("insert into meta values ('tri_seam_release', 'tri_seam_v1')")
    conn.commit()
    conn.close()
    return str(path)


def test_authorization_covers_the_batch(tmp_path):
    db = _database(tmp_path)
    listed = _authorization(tmp_path, ['wf_v2', 'boll_pf_v1', 'boll_pf_v2', 'boll_pf_v3', 'pick_v1'])
    payload = ma_lab.check_holdout('2026-06-30', db, listed, batch='boll_pf_v1')
    assert payload['window'] == ['2024-07-01', '2026-06-30']
    for batch in ('wf_v2', 'boll_pf_v2', 'boll_pf_v3', 'pick_v1'):
        assert ma_lab.check_holdout('2026-06-30', db, listed, batch=batch)['scope']
    with pytest.raises(ValueError, match='does not cover'):
        ma_lab.check_holdout('2026-06-30', db, listed, batch='boll_pf_v9')

    # the original single-name form keeps working, and its default batch is still wf_v2
    single = _authorization(tmp_path, 'wf_v2')
    assert ma_lab.check_holdout('2026-06-30', db, single)['scope'] == 'wf_v2'
    with pytest.raises(ValueError, match='does not cover'):
        ma_lab.check_holdout('2026-06-30', db, single, batch='pick_v1')


def test_holdout_stays_closed_by_default():
    with pytest.raises(ValueError, match='reserved segment'):
        ma_pool.load_market(end='2024-07-05')
    with pytest.raises(ValueError, match='development cutoff'):
        boll_weekly.market_zone(end='2024-07-05')
    # the switch is the only way through, and it still refuses anything past 2026-06-30
    with pytest.raises(ValueError, match='after 2026-06-30'):
        boll_weekly.market_zone(end='2026-07-01', allow_holdout=True, batch='boll_pf_v1')
