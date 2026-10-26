"""Unit tests for leadtrend_db v1 on synthetic inputs (no network, no real data).

Each test names the failure it guards against (repair round, brief section 3). The end-to-end properties on the real
archive (completeness, weights, v05 agreement, status, foreign keys, offline rebuild) are verified by checks.py and
recorded in data/db/exports/checks.json; they are not repeated here.
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from leadtrend_db import sources as S
from leadtrend_db.build import Build, content_hash, quarter_ends, source_id, write_db
from leadtrend_db.identity import (ELIGIBLE_TYPES, EXCLUDED_TYPES, NON_STOCK_TYPES, cusip_check_ok, isin_check_ok,
                                   load_overrides, name_key, name_signature, parse_cik_lookup, parse_company_tickers,
                                   security_type, us_isin_cusip, valid_cusip, valid_isin, valid_ticker,
                                   class_code_from_text)
from leadtrend_db.parse_ncsr import parse_500_schedule
from leadtrend_db.parse_nport import holdings_lines
from leadtrend_db.sources import RawMissing, RawStore, sha256_bytes
from leadtrend_db.status import classify, detect_successors, filing_rows

SCHEDULE = """
<html><body>
<p>Vanguard 500 Index Fund</p><p>Schedule of Investments</p>
<table>
<tr><td>Common Stocks (99.6%)</td><td>1</td></tr>
<tr><td>Consumer Discretionary (12.5%)</td></tr>
<tr><td>* Amazon.com Inc.</td><td>4,280,247</td><td>1,592,680</td></tr>
<tr><td>Comcast Corp. Class A</td><td>27,223,688</td><td>1,537,322</td></tr>
<tr><td>Total System Services Inc.</td><td>1,851,193</td><td>70,623</td></tr>
<tr><td>CDK Global Inc.</td><td>(1,009,196)</td><td>(30,871)</td></tr>
<tr><td>Total Common Stocks (Cost $117,602,783)</td><td>&nbsp;</td><td>3,100,000</td></tr>
<tr><td>Temporary Cash Investments (0.5%)</td><td>1</td></tr>
<tr><td>2,3 Vanguard Market Liquidity Fund</td><td>0.128%</td><td>962,239,969</td><td>962,240</td></tr>
<tr><td>Total Investments (100.1%) (Cost $118,615,010)</td><td>4,112,227</td></tr>
</table></body></html>
"""
# the stock total the file reports is 3,100,000 (thousands); stock lines 4,280,247/1,592,680 and 27,223,688/1,537,322
# are not both in the total, so the reconciliation test below uses a self-consistent fixture (see SELF_CONSISTENT)


def _schedule(stock_values: list[int], negative: int = 0, total_common: int | None = None) -> bytes:
    rows = ''.join(f'<tr><td>Holding {i} Corp.</td><td>{1000 + i}</td><td>{v}</td></tr>'
                   for i, v in enumerate(stock_values))
    if negative:
        rows += f'<tr><td>Negative Co.</td><td>(5,000)</td><td>({negative})</td></tr>'
    total = total_common if total_common is not None else sum(stock_values) - negative
    return (f'<html><body><p>Vanguard 500 Index Fund</p><p>Schedule of Investments</p><table>'
            f'<tr><td>Common Stocks (99.6%)</td><td>1</td></tr>{rows}'
            f'<tr><td>Total Common Stocks (Cost $1)</td><td>{total}</td></tr>'
            f'<tr><td>Total Investments (100.1%)</td><td>{total + 1}</td></tr>'
            f'</table></body></html>').encode('utf-8')


# -- parser -------------------------------------------------------------------------------------------------------

def test_negative_row_is_kept_apart_and_reconciles():
    """Failure: CDK Global's parenthesised negative row was read as a positive holding (2014-09-30)."""
    values = [100, 200, 300]
    res = parse_500_schedule(_schedule(values, negative=500))
    assert [r['value_usd'] for r in res['stock_rows']] == [v * 1000 for v in values]
    assert [r['value_usd'] for r in res['negative_rows']] == [-500 * 1000]
    assert res['reconciled'] is True and res['total_common_stocks_usd'] == 100 * 1000


def test_reconciliation_fails_when_a_row_is_lost():
    """Failure mode: the old parser silently dropped rows; a total that does not reconcile must say so."""
    res = parse_500_schedule(_schedule([100, 200], total_common=999))
    assert res['reconciled'] is False


def test_split_name_and_share_in_name_cell_are_joined():
    """Failure: 'Twenty-First Century' and 'Fox Inc. Class A' sit in separate rows; shares printed in the name cell."""
    html = ('<html><body><p>Vanguard 500 Index Fund</p><p>Schedule of Investments</p><table>'
            '<tr><td>Common Stocks (99.6%)</td></tr>'
            '<tr><td>Twenty-First Century</td></tr>'
            '<tr><td>Fox Inc. Class A</td><td>20,114,141</td><td>772,484</td></tr>'
            '<tr><td>Bank of America Corp. 114,076,519</td><td>2,040,829</td></tr>'
            '<tr><td>Total Common Stocks</td></tr><tr><td>(Cost $1)</td><td>775,313</td></tr>'
            '<tr><td>Total Investments (100.0%)</td></tr><tr><td>(Cost $2)</td><td>775,314</td></tr>'
            '</table></body></html>').encode('utf-8')
    res = parse_500_schedule(html)
    names = {r['name']: (r['shares'], r['value_usd']) for r in res['stock_rows']}
    assert names['Twenty-First Century Fox Inc. Class A'] == (20114141, 772484000)
    assert names['Bank of America Corp.'] == (114076519, 2040829000)
    assert res['total_common_stocks_usd'] == 775313000 and res['total_investments_usd'] == 775314000


def test_company_name_beginning_with_total_is_joined_across_rows():
    """Failure: 'Total System' + 'Services Inc.' (split over two rows) lost its first half because names starting with
    'total' were never joined; 'Services Inc.' then stood as a holding of its own."""
    html = ('<html><body><p>Vanguard 500 Index Fund</p><p>Schedule of Investments</p><table>'
            '<tr><td>Common Stocks (99.6%)</td></tr>'
            '<tr><td>Total System</td></tr><tr><td>Services Inc.</td><td>1,851,193</td><td>70,623</td></tr>'
            '<tr><td>Total Common Stocks (Cost $1)</td><td>70,623</td></tr>'
            '<tr><td>Total Investments (100.0%)</td><td>70,624</td></tr></table></body></html>').encode('utf-8')
    res = parse_500_schedule(html)
    assert [r['name'] for r in res['stock_rows']] == ['Total System Services Inc.']


def test_running_head_fragment_is_not_a_company_name():
    """Failure: a page-head 'Shares' cell was joined onto the next company name ('Shares Amphenol Corp.')."""
    html = ('<html><body><p>Vanguard 500 Index Fund</p><p>Schedule of Investments</p><table>'
            '<tr><td>Common Stocks (99.6%)</td></tr><tr><td>Shares</td></tr>'
            '<tr><td>Amphenol Corp. Class A</td><td>5,923,638</td><td>641,115</td></tr>'
            '<tr><td>Total Common Stocks (Cost $1)</td><td>641,115</td></tr>'
            '<tr><td>Total Investments (100.0%)</td><td>641,116</td></tr></table></body></html>').encode('utf-8')
    res = parse_500_schedule(html)
    assert [r['name'] for r in res['stock_rows']] == ['Amphenol Corp. Class A']


def test_section_starts_at_statement_of_net_assets_not_a_later_page():
    """Failure: the holdings began under 'Financial Statements / Statement of Net Assets' and the parser started at a
    later running head, losing about 60 rows. A performance page mentioning the fund must not be the start."""
    html = ('<html><body><p>Vanguard 500 Index Fund</p><p>Sector Diversification</p><p>Perf text</p>'
            '<p>500 Index Fund Financial Statements</p><p>Statement of Net Assets</p><table>'
            '<tr><td>Alpha Inc.</td><td>1,000,000</td><td>10,000</td></tr>'
            '<tr><td>Total Common Stocks (Cost $1)</td><td>10,000</td></tr>'
            '<tr><td>Total Investments (100.0%)</td><td>10,001</td></tr></table></body></html>').encode('utf-8')
    res = parse_500_schedule(html)
    assert res['found'] and [r['name'] for r in res['stock_rows']] == ['Alpha Inc.']
    assert res['reconciled'] is True


def test_missing_schedule_is_reported_not_guessed():
    res = parse_500_schedule(b'<html>no schedule here</html>')
    assert res['found'] is False and res['stock_rows'] == []


# -- identifiers --------------------------------------------------------------------------------------------------

def test_placeholders_are_never_identifiers():
    """Failure: 'N/A' was used as a CUSIP and merged 87 holdings of unrelated companies."""
    assert valid_cusip('N/A') == '' and valid_cusip('') == '' and valid_cusip('0') == ''
    assert valid_isin('N/A') == ''


def test_cusip_and_isin_check_digits():
    assert cusip_check_ok('037833100') and valid_cusip('037833100') == '037833100'
    assert not cusip_check_ok('037833101')
    assert isin_check_ok('US0378331005') and not isin_check_ok('US0378331006')


def test_us_isin_gives_its_cusip_and_foreign_isin_gives_none():
    assert us_isin_cusip('US0378331005') == '037833100'
    assert us_isin_cusip('IE00BZ12WP82') == ''


def test_ticker_shape_rejects_vendor_codes():
    """Failure: Linde carried the vendor code 9995214D as its ticker."""
    assert valid_ticker('LIN') == 'LIN' and valid_ticker('9995214D') == '' and valid_ticker('BRK.B') == 'BRK.B'


def test_share_class_codes():
    assert class_code_from_text('Alphabet Inc. Class C') == 'CL_C' and class_code_from_text('') == 'COM'


# -- security type -----------------------------------------------------------------------------------------------

def test_rights_and_cvrs_are_not_common_stock():
    """Failure: T-Mobile rights (CUSIP 872590112) were eligible common stock; the Hologic CVR was in the index."""
    assert security_type('T-MOBILE US INC', 'T-Mobile US Inc', '', 'Right', 'EC')[0] == 'right'
    assert security_type('HOLOGIC INC', 'HOLOGIC INC - CVR (APRIL 2026)', '', '', 'EC')[0] == 'cvr'
    assert security_type('ABIOMED Inc', 'ABIOMED INC-CVR', '', '', 'EC')[0] == 'cvr'


def test_common_only_on_equity_evidence_and_unknown_otherwise():
    assert security_type('Walt Disney Co.', 'Walt Disney Co.', '', '', 'EC') == \
        ('common', 'SEC asset category EC (common equity), no explicit exclusion evidence')
    assert security_type('Something Plc', 'Something Plc', '', '', 'DBT')[0] == 'unknown'


def test_partnership_units_and_reit_by_sic():
    assert security_type('Enterprise Products Partners L.P.', 'Common Units', '', '', 'EC')[0] == 'partnership'
    assert security_type('Simon Property Group', 'Simon Property Group', '6798', '', 'EC')[0] == 'reit'
    assert set(NON_STOCK_TYPES) == {'right', 'warrant', 'cvr', 'etf', 'other'}
    assert {'reit', 'partnership', 'spac', 'preferred'} <= set(EXCLUDED_TYPES)
    assert ELIGIBLE_TYPES == ('common', 'adr')


def test_nport_lines_keep_blank_cusip_and_placeholder_lines():
    """Failure: the v05 parser dropped equity lines without a CUSIP; a line must be kept with its identifiers as
    reported."""
    xml = ('<?xml version="1.0"?><edgarSubmission xmlns="http://www.sec.gov/edgar/nport">'
           '<seriesId>S000002839</seriesId><repPdDate>2019-12-31</repPdDate>'
           '<classId>C000092055</classId><invstOrSec><name>Foo Ltd.</name><title>Foo Ltd.</title>'
           '<cusip>N/A</cusip><lei>N/A</lei><balance>10</balance><valUSD>100.5</valUSD><pctVal>1.5</pctVal>'
           '<assetCat>EC</assetCat></invstOrSec>'
           '<invstOrSec><name>Bar</name><title>Bar</title><cusip></cusip><balance>5</balance>'
           '<valUSD>50</valUSD><pctVal>0.5</pctVal><assetCat>EC</assetCat></invstOrSec>'
           '<invstOrSec><name>Debt</name><title>Debt</title><cusip>123456789</cusip><valUSD>7</valUSD>'
           '<assetCat>DBT</assetCat></invstOrSec></edgarSubmission>').encode('utf-8')
    info = holdings_lines(xml)
    assert [l['cusip'] for l in info['lines']] == ['N/A', '']
    assert info['non_equity_rows'] == 1 and info['non_equity_value_usd'] == 7


# -- names and SEC lookups ----------------------------------------------------------------------------------------

def test_name_keys_drop_sec_suffixes_and_order():
    assert name_key('MATTEL INC /DE/') == 'MATTEL'
    assert name_key('CHARTER COMMUNICATIONS, INC. /MO/') == 'CHARTER COMMUNICATIONS'
    assert name_key('US Bancorp') == name_key('U.S. Bancorp') == 'US BANCORP'
    assert name_key('Becton Dickinson and Co.') == name_key('BECTON DICKINSON & CO') == 'BECTON DICKINSON'
    assert name_signature('LILLY ELI & CO') == name_signature('ELI LILLY & CO')


def test_lookup_and_tickers_use_the_same_keys():
    lookup = parse_cik_lookup('mattel inc /de/:0000063276:\nMATTEL INC:0001284721:\n')
    assert lookup[name_key('Mattel Inc.')] == {'0000063276', '0001284721'}
    tickers = parse_company_tickers({'0': {'cik_str': 63276, 'ticker': 'MAT', 'title': 'MATTEL INC /DE/'}})
    assert tickers['MAT'] == {'cik': '0000063276', 'title': 'MATTEL INC /DE/'}


class FakeBuild(Build):
    """A Build whose SEC submissions are given directly (no archive, no network)."""

    def __init__(self, subs: dict):
        super().__init__(RawStore(offline=True), log=lambda m: None)
        self.subs = subs

    def issuer_sub(self, cik):
        return self.subs.get(cik)


def _sub(name, former=(), filings=()):
    return dict(cik='', name=name, former=[dict(name=n, valid_from=f, valid_to=t) for n, f, t in former],
                filings=[dict(form=f, filing_date=d, report_date=r, accession='', document='', acceptance='')
                         for f, d, r in filings], exchanges=[], tickers=[], sic='', sic_description='', state='',
                source_sha='')


def test_name_is_checked_for_the_holding_date_with_former_name_dates():
    """Failure: a historical holding was mapped to whichever CIK carried the same name today. A former name only
    counts for the dates in force, and the current name counts only after the last former name ended."""
    fb = FakeBuild({'0000059478': _sub('ELI LILLY & Co', former=[('LILLY ELI & CO', '1994-10-26', '2019-10-25')],
                                       filings=[('10-Q', '2019-11-01', '2019-09-30')])})
    assert fb.name_valid_at('0000059478', 'Eli Lilly & Co.', '2019-09-30') is True    # former name in force
    assert fb.name_valid_at('0000059478', 'Eli Lilly & Co.', '2015-03-31') is True
    assert fb.name_valid_at('0000059478', 'Eli Lilly & Co.', '2019-06-30') is True
    assert fb.name_valid_at('0000059478', 'Other Corp', '2019-09-30') is False


def test_candidate_without_a_filing_near_the_date_is_rejected():
    """Failure: a stale same-name entity (no filings in 2015) was accepted as Mattel's CIK for 2015."""
    fb = FakeBuild({'0001284721': _sub('MATTEL INC', filings=[('10-K', '2009-03-01', '2008-12-31')]),
                    '0000063276': _sub('MATTEL INC /DE/', filings=[('10-Q', '2016-02-05', '2015-12-31')])})
    assert fb.periodic_near('0001284721', '2015-12-31') is False
    fb.lookup = {name_key('Mattel Inc.'): {'0001284721'}}
    res = fb.issuer_by_name('Mattel Inc.', '2015-12-31')
    assert res['cik'] is None and res['method'] == 'no_candidate_valid_at_date:1'
    assert fb.periodic_near('0000063276', '2015-12-31') is True


def test_single_candidate_is_accepted_only_when_valid_at_the_date():
    fb = FakeBuild({'0000109380': _sub('ZIONS BANCORPORATION', filings=[('10-Q', '2019-11-05', '2019-09-30')])})
    fb.lookup = {name_key('Zions Bancorp NA'): {'0000109380'}}
    assert fb.issuer_by_name('Zions Bancorp NA', '2019-09-30')['cik'] is None   # not the holding's name: not accepted
    fb.lookup = {name_key('Zions Bancorporation'): {'0000109380'}}
    fb.resolve_cache.clear()
    assert fb.issuer_by_name('Zions Bancorporation', '2019-09-30')['cik'] == '0000109380'


# -- status -------------------------------------------------------------------------------------------------------

def fil(form, date_, rep=''):
    return dict(form=form, filing_date=date_, report_date=rep, accession='0000000000-00-000000', document='d',
                acceptance='')


LATEST = '2026-06-30'


def test_listed_now_and_in_latest_snapshot_is_in_index():
    d = dict(exchanges=['NYSE'], filings=[], source_id='')
    assert classify(d, True, '2026-10-26', LATEST)['status'] == 'in_index'
    assert classify(d, False, '2026-10-26', LATEST)['status'] == 'listed_out_of_index'


def test_a_delisting_after_the_latest_snapshot_overrides_it():
    """Failure: EA and WBD stayed in_index because the latest snapshot held them and the old check returned early."""
    d = dict(exchanges=[], filings=[fil('25-NSE', '2026-08-04')], source_id='')
    out = classify(d, True, '2026-10-26', LATEST)
    assert out['status'] == 'delisted' and out['delisting_date'] == '2026-08-14' and out['needs_review'] == 0


def test_a_delisting_before_the_latest_holding_is_flagged_not_hidden():
    d = dict(exchanges=[], filings=[fil('25', '2021-03-04')], source_id='')
    out = classify(d, True, '2026-10-26', LATEST)
    assert out['status'] == 'delisted' and out['needs_review'] == 1


def test_last_form_25_is_the_delisting_notice():
    """Failure: an earlier 25-NSE that was unrelated set a delisting date years too early (WPX)."""
    d = dict(exchanges=[], filings=[fil('25-NSE', '2018-07-30'), fil('25-NSE', '2021-01-07')], source_id='')
    assert classify(d, False, '2026-10-26', LATEST)['delisting_date'] == '2021-01-17'


def test_no_listing_and_no_notice_is_unknown_not_delisted():
    """Failure: a stale entity with no exchange field was declared delisted on its last filing date."""
    d = dict(exchanges=[], filings=[fil('10-Q', '2004-06-30')], source_id='')
    out = classify(d, False, '2026-10-26', LATEST)
    assert out['status'] == 'unknown' and out['needs_review'] == 1


def test_successor_needs_a_clear_name_match():
    from datetime import date as d_
    info = {
        'CIK0000003': dict(name='Medtronic Inc', former_names=[], filings=[fil('15-12G', '2015-02-27')]),
        'CIK0000004': dict(name='Medtronic plc', former_names=[], filings=[fil('8-K12B', '2015-01-27')]),
        'CIK0000001': dict(name='Walgreen Co', former_names=[], filings=[fil('15-12B', '2014-12-31')]),
        'CIK0000002': dict(name='Walgreens Boots Alliance Inc', former_names=[], filings=[fil('8-K12B', '2014-12-31')]),
    }
    links, reviews = detect_successors(info, {'CIK0000003': d_(2015, 2, 27), 'CIK0000001': d_(2014, 12, 31)})
    assert [(l['predecessor'], l['successor']) for l in links] == [('CIK0000003', 'CIK0000004')]
    assert [r['subject_id'] for r in reviews] == ['CIK0000001']        # similar timing, name below the rule


# -- spans, quarters, store, hash, foreign keys -------------------------------------------------------------------

def _line(issuer, sec, typ='common', kind='stock'):
    return dict(issuer=issuer, sec=sec, type=typ, line_kind=kind)


def test_spans_split_on_absence_and_gap_and_skip_excluded_types():
    def snap(rd, lines):
        return dict(report_date=rd, acceptance=f'{rd}T00:00:00Z', lines=lines)
    primary = [snap('2019-03-31', [_line('0000000001', 'CUSIP:A')]), snap('2019-06-30', [_line('0000000001', 'CUSIP:A')]),
               snap('2019-09-30', [_line('0000000001', 'CUSIP:R', 'right')]),        # a right is not the stock
               snap('2019-12-31', [_line('0000000001', 'CUSIP:A')]), snap('2020-06-30', [_line('0000000001', 'CUSIP:A')])]
    rows = Build.spans(primary, [])
    issuer = sorted((r for r in rows if r['level'] == 'issuer'), key=lambda r: r['first_as_of'])
    assert [(r['first_as_of'], r['last_as_of'], r['next_absent_as_of'], r['quarters_present']) for r in issuer] == [
        ('2019-03-31', '2019-06-30', '2019-09-30', 2), ('2019-12-31', '2019-12-31', '', 1),
        ('2020-06-30', '2020-06-30', '', 1)]


def test_quarter_ends_are_calendar_quarter_ends():
    q = quarter_ends('2013-12-31', '2019-06-30')
    assert len(q) == 23 and q[0] == '2013-12-31' and q[-1] == '2019-06-30'
    assert all(d[5:] in ('03-31', '06-30', '09-30', '12-31') for d in q)


def test_raw_store_offline_and_archive(tmp_path):
    store = RawStore(raw_dir=tmp_path / 'raw', user_agent=None, offline=True)
    with pytest.raises(RawMissing):
        store.get('https://example.test/a', 'a.txt', 'other')
    online = RawStore(raw_dir=tmp_path / 'raw', user_agent=None, offline=False)
    src = tmp_path / 'src.txt'
    src.write_bytes(b'hello')
    assert online.import_local('local:src', 'x/src.txt', 'other', src, 'test') == b'hello'
    again = RawStore(raw_dir=tmp_path / 'raw', offline=True)
    assert again.get('local:src', 'unused', 'other') == b'hello'
    assert (tmp_path / 'raw' / 'manifest.jsonl').read_text(encoding='utf-8').count('\n') == 1
    with pytest.raises(RuntimeError):
        online._store_new('local:other', 'x/src.txt', b'z', 'other', 'test', '')


def test_orphan_file_without_manifest_line_is_replaced_but_archived_file_is_not(tmp_path):
    store = RawStore(raw_dir=tmp_path / 'raw', offline=False)
    (tmp_path / 'raw' / 'o').mkdir(parents=True)
    (tmp_path / 'raw' / 'o' / 'f.txt').write_bytes(b'truncated')          # from an interrupted run
    assert store._store_new('local:x', 'o/f.txt', b'complete', 'other', 'test', '') == b'complete'
    with pytest.raises(RuntimeError):
        store._store_new('local:x2', 'o/f.txt', b'other', 'other', 'test', '')


def test_content_hash_ignores_insert_order(tmp_path):
    schema = S.ROOT / 'leadtrend_db' / 'schema.sql'
    rows = [dict(security_id=f'CUSIP:{i:09d}', issuer_id='CIK:0000000001', class_code='COM', security_type='common',
                 type_basis='t', name=f'N{i}', id_scheme='CUSIP', notes='', source_id=None) for i in range(5)]
    cols = ['security_id', 'issuer_id', 'class_code', 'security_type', 'type_basis', 'name', 'id_scheme', 'notes',
            'source_id']

    def digest(order, name):
        conn = sqlite3.connect(tmp_path / name)
        conn.execute('PRAGMA foreign_keys = OFF')
        conn.executescript(schema.read_text(encoding='utf-8'))
        conn.execute("INSERT INTO issuers (issuer_id, cik) VALUES ('CIK:0000000001', '0000000001')")
        conn.executemany(f'INSERT INTO securities ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                         [tuple(r[c] for c in cols) for r in order])
        conn.commit()
        d = content_hash(conn)
        conn.close()
        return d

    assert digest(rows, 'a.sqlite') == digest(list(reversed(rows)), 'b.sqlite')


def test_reserved_price_tables_have_the_repaired_keys():
    """Failure: prices_daily had no source in its key and price_coverage could hold one source only."""
    conn = sqlite3.connect(':memory:')
    conn.executescript(S.ROOT.joinpath('leadtrend_db', 'schema.sql').read_text(encoding='utf-8'))
    pk = {r[1]: r[5] for r in conn.execute("PRAGMA table_info(prices_daily)")}
    assert {k for k, v in pk.items() if v} == {'security_id', 'session_date', 'source'}
    pk2 = {r[1] for r in conn.execute("PRAGMA table_info(price_coverage)") if r[5]}
    assert pk2 == {'security_id', 'source'}


def test_write_db_refuses_a_foreign_key_violation(tmp_path):
    """Failure: 1,613 foreign-key violations (empty source ids) were written without any error."""
    tables = {k: [] for k in ['sources', 'issuers', 'issuer_names', 'issuer_links', 'securities',
                              'security_identifiers', 'snapshots', 'holdings', 'issuer_resolution', 'membership_spans',
                              'listing_status', 'review_items']}
    tables['issuers'] = [dict(issuer_id='CIK:1', cik='0000000001', name='', sic='', sic_description='',
                              state_of_incorporation='', lei='', source_id='SRC-missing')]
    with pytest.raises(sqlite3.IntegrityError):
        write_db(tables, tmp_path / 'x.sqlite', [], {})


def test_source_id_is_content_derived():
    assert source_id(sha256_bytes(b'abc')) == 'SRC-' + sha256_bytes(b'abc')[:16]


def test_overrides_need_reason_and_evidence(tmp_path):
    p = tmp_path / 'o.csv'
    p.write_text('override_kind,key,value,field,reason,evidence\ncusip_cik,123456789,1652044,,r,e\n', encoding='utf-8')
    assert load_overrides(p)[0]['key'] == '123456789'
    p.write_text('override_kind,key,value,field,reason,evidence\nname_cik,x,1,,,\n', encoding='utf-8')
    with pytest.raises(ValueError):
        load_overrides(p)
