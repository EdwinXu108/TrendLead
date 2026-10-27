"""The 2026-10-27 investor exclusion is in force in the released database.

Prices and coverage stay in place; only the strategy list drops the two securities.
"""
import sqlite3

import pytest

from leadtrend_db import api

ACE = 'CUSIP:H1467J104'
CMCSK = 'CIKCLS:0001166691:CL_A_SPECIAL'
CHUBB = 'CIK:0000896159'
COMCAST = 'CIK:0001166691'


@pytest.fixture(scope='module')
def conn():
    if not api.DB_PATH.exists():
        pytest.skip('released database is not present')
    connection = sqlite3.connect(f'file:{api.DB_PATH.as_posix()}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def test_release_marker(conn):
    meta = dict(conn.execute('select key, value from meta'))
    assert meta['pool_exclusion_release'] == 'pool_exclusions_v2'
    assert meta['economic_data_version'] == 'development_release_v1'


def test_both_securities_are_out_of_every_pool(conn):
    for sid in (ACE, CMCSK):
        rows = conn.execute('select strategy_eligible, reason, count(*) n from strategy_securities '
                            'where security_id=? group by 1, 2', (sid,)).fetchall()
        assert rows, f'{sid} must keep its historical rows'
        assert all(r['strategy_eligible'] == 0 for r in rows)
        assert any('investor_exclusion' in r['reason'] for r in rows)


def test_chubb_leaves_the_pool_and_comcast_stays(conn):
    chubb = conn.execute('select strategy_eligible, excluded_reason from strategy_companies '
                         'where issuer_id=?', (CHUBB,)).fetchall()
    assert chubb and all(r['strategy_eligible'] == 0 for r in chubb)
    assert any('investor_exclusion' in r['excluded_reason'] for r in chubb)
    comcast = conn.execute('select distinct strategy_eligible from strategy_companies where issuer_id=?',
                           (COMCAST,)).fetchall()
    assert [r[0] for r in comcast] == [1]


def test_price_history_is_kept(conn):
    for sid in (ACE, CMCSK):
        n = conn.execute('select count(*) from prices_daily where security_id=?', (sid,)).fetchone()[0]
        assert n > 100, f'{sid}: price history must not be deleted'


def test_pool_at_no_longer_lists_them(conn):
    pool = api.pool_at('2016-05-02')
    ids = {s['security_id'] for s in pool['securities']}
    assert ACE not in ids and CMCSK not in ids
    assert 'CUSIP:20030N101' in ids, 'Comcast Class A must stay in the pool'
