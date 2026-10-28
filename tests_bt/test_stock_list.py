"""The stock list: a name enters only once its pool version is effective, and the old union stays reproducible."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from leadtrend_bt import ma_walk

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / 'data' / 'db' / 'leadtrend_db_v1.sqlite'
LEGACY_SHA256 = 'd62f4bc298b5b007bb96f02c5265a6f9c9848f743223240001931d41277c7bba'


def build_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table issuers(issuer_id text, name text);
        create table strategy_securities(version_id text, issuer_id text, security_id text,
                                         current_ticker text, strategy_eligible integer);
        create table pool_versions(version_id text, report_period text);
        create table price_quarter_coverage(version_id text, security_id text, active_from text);
    """)
    conn.executemany('insert into issuers values (?, ?)', [('I1', 'Alpha'), ('I2', 'Beta'),
                                                           ('I3', 'Gamma'), ('I4', 'Delta')])
    versions = [('V1', '2014-03-31', '2014-05-30'), ('V2', '2019-06-30', '2019-08-30'),
                ('V3', '2025-06-30', '2025-08-29')]
    conn.executemany('insert into pool_versions values (?, ?)', [(v, p) for v, p, _ in versions])
    rows = []
    for version, _, effective in versions:
        conn.execute('insert into price_quarter_coverage values (?, ?, ?)', (version, 'X', effective))
    membership = {'V1': ['AAA', 'BBB'], 'V2': ['AAA', 'BBB', 'CCC'], 'V3': ['AAA', 'CCC', 'DDD']}
    for version, _, _ in versions:
        for index, ticker in enumerate(membership[version], 1):
            rows.append((version, f'I{index}', f'CUSIP:{ticker}', ticker, 1))
    conn.executemany('insert into strategy_securities values (?, ?, ?, ?, ?)', rows)
    conn.commit()
    conn.close()


def test_a_security_enters_only_when_its_version_is_effective(tmp_path):
    path = tmp_path / 'pool.sqlite'
    build_db(path)
    early = {row['ticker']: row for row in ma_walk.stock_list(path, as_of='2024-06-28')}
    assert set(early) == {'AAA', 'BBB', 'CCC'}
    assert early['CCC']['first_effective'] == '2019-08-30'
    assert early['CCC']['first_report_period'] == '2019-06-30'
    assert early['AAA']['first_effective'] == '2014-05-30'

    late = {row['ticker']: row for row in ma_walk.stock_list(path, as_of='2025-12-31')}
    assert set(late) == {'AAA', 'BBB', 'CCC', 'DDD'}
    assert late['DDD']['first_effective'] == '2025-08-29'

    legacy = {row['ticker'] for row in ma_walk.stock_list(path, as_of='2024-06-28', legacy_union=True)}
    assert legacy == {'AAA', 'BBB', 'CCC', 'DDD'}


def test_the_legacy_union_still_returns_the_committed_ticker_list():
    if not DB.exists():
        pytest.skip('released database is not present')
    stocks = ma_walk.stock_list(DB, legacy_union=True)
    assert len(stocks) == 318
    digest = hashlib.sha256('\n'.join(sorted(row['ticker'] for row in stocks)).encode()).hexdigest()
    assert digest == LEGACY_SHA256
