"""Unit tests for leadtrend_db v1 on synthetic inputs (no network, no real data)."""
from __future__ import annotations

import sqlite3

import pytest

from leadtrend_db import sources as S
from leadtrend_db.build import Build, content_hash, quarter_ends, source_id, write_db
from leadtrend_db.identity import (class_code_from_text, cik10, core_name, load_overrides, norm_core,
                                   parse_cik_lookup, parse_company_tickers, security_type, ticker_key)
from leadtrend_db.parse_ncsr import parse_500_schedule
from leadtrend_db.sources import RawMissing, RawStore, sha256_bytes
from leadtrend_db.status import classify, detect_successors, filing_rows

SCHEDULE = """
<html><body>
<p>Vanguard 500 Index Fund</p><p>Schedule of Investments</p>
<table>
<tr><td>Market</td><td>Value</td></tr>
<tr><td>Common Stocks (99.6%)</td><td>1</td></tr>
<tr><td>Consumer Discretionary (12.5%)</td></tr>
<tr><td>* Amazon.com Inc.</td><td>4,280,247</td><td>1,592,680</td></tr>
<tr><td>Comcast Corp. Class A</td><td>27,223,688</td><td>1,537,322</td></tr>
<tr><td>Financials (16.1%)</td></tr>
<tr><td>Total System Services Inc.</td><td>1,851,193</td><td>70,623</td></tr>
<tr><td>2,3</td><td>Teradata Corp.</td><td>1,631,459</td><td>72,013</td></tr>
<tr><td>Total Common Stocks (Cost $117,602,783)</td><td>&nbsp;</td><td>3,100,000</td></tr>
<tr><td>Temporary Cash Investments (0.5%)</td><td>1</td></tr>
<tr><td>Money Market Fund (0.5%)</td></tr>
<tr><td>2,3 Vanguard Market Liquidity Fund</td><td>0.128%</td><td>962,239,969</td><td>962,240</td></tr>
<tr><td>Total Temporary Cash Investments (Cost $1,012,227)</td><td>1,012,227</td></tr>
<tr><td>Total Investments (100.1%) (Cost $118,615,010)</td><td>4,112,227</td></tr>
</table></body></html>
"""


def test_schedule_keeps_stock_rows_and_excludes_cash():
    res = parse_500_schedule(SCHEDULE.encode('utf-8'))
    names = [r['name'] for r in res['stock_rows']]
    assert names == ['Amazon.com Inc.', 'Comcast Corp. Class A', 'Total System Services Inc.', 'Teradata Corp.']
    comcast = res['stock_rows'][1]
    assert comcast['raw_class_text'] == 'Class A'
    assert comcast['value_usd'] == 1537322 * 1000
    assert res['total_common_stocks_usd'] == 3100000 * 1000
    assert res['total_investments_usd'] == 4112227 * 1000
    assert [e['category'] for e in res['excluded']] == ['money market fund']
    assert res['excluded'][0]['value_usd'] == 962240 * 1000


def test_schedule_absent_is_reported_not_guessed():
    res = parse_500_schedule(b'<html>no schedule here</html>')
    assert res['found'] is False and res['stock_rows'] == []


def test_norm_core_drops_class_text():
    assert norm_core('Alphabet Inc. Class A') == norm_core('Alphabet Inc. Class C') == norm_core('Alphabet Inc.')
    assert core_name('NIKE Inc. Class B') .strip() == 'NIKE Inc.'
    assert class_code_from_text('Class B') == 'CL_B'
    assert class_code_from_text('Common Stock') == 'COM'


def test_security_type_rules():
    assert security_type('Simon Property Group Inc.', '', '6798') == 'reit'
    assert security_type('Enterprise Products Partners L.P.', 'Common Units') == 'partnership'
    assert security_type('Partners Group Holding', 'Common Stock') == 'common'   # a Partners name is not a unit
    assert security_type('Sanofi ADR', '') == 'adr'
    assert security_type('Apple Inc.', 'Common Stock') == 'common'


def test_cik_lookup_and_tickers():
    lookup = parse_cik_lookup('apple inc.:0000320193:\nAPPLE INC:320193:\nkraft heinz co:0001637459:\n')
    assert lookup[norm_core('Apple Inc.')] == {'0000320193'}
    tickers = parse_company_tickers({'0': {'cik_str': 1652044, 'ticker': 'GOOGL', 'title': 'Alphabet Inc.'}})
    assert tickers['GOOGL'] == {'cik': '0001652044', 'title': 'Alphabet Inc.'}
    assert ticker_key('BRK.B') == 'BRK-B' and cik10(36405) == '0000036405'


def test_overrides_need_reason_and_evidence(tmp_path):
    p = tmp_path / 'o.csv'
    p.write_text('override_kind,key,value,field,reason,evidence\ncusip_cik,123456789,1652044,,r,e\n', encoding='utf-8')
    assert load_overrides(p)[0]['key'] == '123456789'
    p.write_text('override_kind,key,value,field,reason,evidence\nname_cik,x,1,,,\n', encoding='utf-8')
    with pytest.raises(ValueError):
        load_overrides(p)


def fil(form, date, rep=''):
    return dict(form=form, filing_date=date, report_date=rep, accession='0000000000-00-000000', document='d',
                acceptance='')


def test_status_rules():
    base = dict(name='X', exchanges=[], filings=[], source_sha='')
    assert classify(dict(base, exchanges=['NYSE']), False, '2026-10-26')['status'] == 'listed_out_of_index'
    assert classify(dict(base, exchanges=['OTC']), True, '2026-10-26')['status'] == 'in_index'
    d = classify(dict(base, filings=[fil('25', '2017-05-01'), fil('15-12B', '2017-05-12')]), False, '2026-10-26')
    assert d['status'] == 'delisted' and d['delisting_date'] == '2017-05-11'
    assert d['method'] == 'form25_filing_date_plus_10d'
    d15 = classify(dict(base, filings=[fil('15-12G', '2018-01-02')]), False, '2026-10-26')
    assert d15['status'] == 'delisted' and d15['delisting_date'] == '2018-01-02'
    assert classify(dict(base), False, '2026-10-26')['status'] == 'unknown'
    recent = classify(dict(base, filings=[fil('10-Q', '2026-08-01')]), False, '2026-10-26')
    assert recent['status'] == 'unknown' and recent['needs_review'] == 1


def test_merger_reason_is_a_hint_only():
    base = dict(name='X', exchanges=[], source_sha='')
    d = classify(dict(base, filings=[fil('DEFM14A', '2017-03-01'), fil('25', '2017-05-01')]), False, '2026-10-26')
    assert d['reason'] == 'acquired_or_merged'
    d = classify(dict(base, filings=[fil('25', '2017-05-01')]), False, '2026-10-26')
    assert d['reason'] == 'unknown'


def test_successor_single_candidate_and_review():
    from datetime import date
    info = {
        'CIK0000001': dict(name='Walgreen Co', former_names=[], filings=[fil('15-12B', '2014-12-31')]),
        'CIK0000002': dict(name='Walgreens Boots Alliance Inc', former_names=[], filings=[fil('8-K12B', '2014-12-20')]),
        'CIK0000003': dict(name='Medtronic Inc', former_names=[], filings=[fil('15-12B', '2015-01-05')]),
        'CIK0000004': dict(name='Medtronic plc', former_names=[], filings=[fil('8-K12B', '2015-01-02')]),
        'CIK0000006': dict(name='Unrelated Corp', former_names=[], filings=[]),
    }
    delisted = {'CIK0000001': date(2014, 12, 31), 'CIK0000003': date(2015, 1, 5)}
    links, reviews = detect_successors(info, delisted)
    # Medtronic: one candidate with the same name -> high-confidence link
    assert [(l['predecessor'], l['successor'], l['confidence']) for l in links] == [('CIK0000003', 'CIK0000004', 'high')]
    # Walgreen: an 8-K12B in the window but the name is below the threshold -> review, no link
    assert [r['subject_id'] for r in reviews] == ['CIK0000001']
    assert reviews[0]['review_type'] == 'successor_candidate'


def test_spans_split_on_absence_and_gap():
    def snap(rd, acc, issuers):
        return dict(report_date=rd, acceptance=f'{rd}T00:00:00Z', lines=[
            dict(issuer=i, sec=None) for i in issuers])
    primary = [snap('2019-03-31', 'a', ['CIK:1']), snap('2019-06-30', 'b', ['CIK:1']),
               snap('2019-09-30', 'c', []), snap('2019-12-31', 'd', ['CIK:1']),
               snap('2020-06-30', 'e', ['CIK:1'])]
    primary = [dict(p, lines=[dict(issuer=l['issuer'].replace('CIK:', '') if l['issuer'] else None, sec=None)
                              for l in p['lines']]) for p in primary]
    rows = Build.spans(primary, [])
    issuer = sorted((r for r in rows if r['level'] == 'issuer'), key=lambda r: r['first_as_of'])
    # left in Q3 2019 and came back: a new span; the missing 2020-03-31 quarter starts another one
    assert [(r['first_as_of'], r['last_as_of'], r['next_absent_as_of'], r['quarters_present']) for r in issuer] == [
        ('2019-03-31', '2019-06-30', '2019-09-30', 2),
        ('2019-12-31', '2019-12-31', '', 1),
        ('2020-06-30', '2020-06-30', '', 1)]


def test_quarter_ends_are_calendar_quarter_ends():
    q = quarter_ends('2013-12-31', '2019-06-30')
    assert len(q) == 23 and q[0] == '2013-12-31' and q[-1] == '2019-06-30'
    assert '2019-03-31' in q and all(d[5:] in ('03-31', '06-30', '09-30', '12-31') for d in q)


def test_filing_rows_reads_parallel_arrays():
    page = dict(form=['N-Q'], filingDate=['2014-05-29'], reportDate=['2014-03-31'], accessionNumber=['0-1-2'],
                primaryDocument=['x.htm'], acceptanceDateTime=['2014-05-29T20:00:00.000Z'])
    row = filing_rows(page)[0]
    assert row['form'] == 'N-Q' and row['report_date'] == '2014-03-31' and row['document'] == 'x.htm'


def test_raw_store_offline_and_archive(tmp_path):
    store = RawStore(raw_dir=tmp_path / 'raw', user_agent=None, offline=True)
    with pytest.raises(RawMissing):
        store.get('https://example.test/a', 'a.txt', 'other')
    online = RawStore(raw_dir=tmp_path / 'raw', user_agent=None, offline=False)
    src = tmp_path / 'src.txt'
    src.write_bytes(b'hello')
    assert online.import_local('local:src', 'x/src.txt', 'other', src, 'test') == b'hello'
    again = RawStore(raw_dir=tmp_path / 'raw', offline=True)
    assert again.get('local:src', 'unused', 'other') == b'hello'           # served from the archive
    assert (tmp_path / 'raw' / 'manifest.jsonl').read_text(encoding='utf-8').count('\n') == 1
    with pytest.raises(RuntimeError):
        online._store_new('local:other', 'x/src.txt', b'z', 'other', 'test', '')   # never overwrites


def test_content_hash_ignores_insert_order(tmp_path):
    schema = S.ROOT / 'leadtrend_db' / 'schema.sql'
    rows = [dict(security_id=f'CUSIP:{i:09d}', issuer_id='CIK:0000000001', class_code='COM', security_type='common',
                 name=f'N{i}', id_scheme='CUSIP', notes='', source_id='SRC-x') for i in range(5)]
    cols = ['security_id', 'issuer_id', 'class_code', 'security_type', 'name', 'id_scheme', 'notes', 'source_id']

    def digest(order, name):
        conn = sqlite3.connect(tmp_path / name)
        conn.executescript(schema.read_text(encoding='utf-8'))
        conn.executemany(f'INSERT INTO securities ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                         [tuple(r[c] for c in cols) for r in order])
        conn.commit()
        d = content_hash(conn)
        conn.close()
        return d

    assert digest(rows, 'a.sqlite') == digest(list(reversed(rows)), 'b.sqlite')


def test_write_db_records_hash_and_is_replaceable(tmp_path):
    tables = {'sources': [], 'issuers': [], 'issuer_names': [], 'issuer_links': [], 'securities': [],
              'security_identifiers': [], 'snapshots': [], 'holdings': [], 'issuer_resolution': [],
              'membership_spans': [], 'listing_status': [], 'review_items': []}
    path = tmp_path / 'db.sqlite'
    h1 = write_db(tables, path, [('x', 'y')], {})
    h2 = write_db(tables, path, [('x', 'y')], {})
    assert h1 == h2 and len(h1) == 64
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT value FROM meta WHERE key='content_sha256'").fetchone()[0] == h1
    conn.close()


def test_source_id_is_content_derived():
    assert source_id(sha256_bytes(b'abc')) == 'SRC-' + sha256_bytes(b'abc')[:16]
