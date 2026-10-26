"""Tests of the round-2 behaviour (rule_version top200_v1): parser amounts, dated identity, types, the membership bound,
ties, the strategy filter and the time query. Each test names the failure it guards against."""
from __future__ import annotations

import sqlite3
from datetime import date

from leadtrend_db import rules as R
from leadtrend_db.build import Build, NAME_LAG_DAYS
from leadtrend_db.identity import (name_key, name_signature, security_type, v05_evidence_fields)
from leadtrend_db.parse_ncsr import parse_500_schedule
from leadtrend_db.query import members_as_of, version_as_of
from leadtrend_db.sources import RawStore
from leadtrend_db.top200 import bound, rank_snapshot


def _page(rows: str, total: int) -> bytes:
    return (f'<html><body><p>Vanguard 500 Index Fund</p><p>Schedule of Investments</p><table>'
            f'<tr><td>Common Stocks (99.6%)</td><td>1</td></tr>{rows}'
            f'<tr><td>Total Common Stocks (Cost $1)</td><td>{total}</td></tr>'
            f'<tr><td>Total Investments (100.1%)</td><td>{total + 1}</td></tr></table></body></html>').encode()


def _row(name: str, shares: str, value: str) -> str:
    return f'<tr><td>{name}</td><td>{shares}</td><td>{value}</td></tr>'


# -- parser: a holding is a holding whatever its size ----------------------------------------------------------------

def test_small_dollar_values_are_amounts_not_footnotes():
    """Failure: 6, 12 and 72 (thousand dollars) were read as footnote marks, so Halyard, News Corp and Wyndham were
    dropped and the schedule still looked close to its total."""
    rows = _row('Halyard Health Inc.', '132', '6') + _row('News Corp. Class B', '6,219', '72') + \
        _row('Wyndham Hotels & Resorts Inc.', '216', '10')
    res = parse_500_schedule(_page(rows, 88))
    assert res['reconciled'] is True
    assert sorted(r['value_usd'] for r in res['stock_rows']) == [6000, 10000, 72000]
    assert {r['name'] for r in res['stock_rows']} == {'Halyard Health Inc.', 'News Corp. Class B',
                                                     'Wyndham Hotels & Resorts Inc.'}


def test_dash_value_is_a_reported_nil_holding_not_a_lost_row():
    """Failure: a holding with a dash in the value column (Conduent, Quality Care) was an unparsed row."""
    rows = _row('Conduent Inc.', '4', '&#8212;') + _row('Quality Care Properties Inc.', '2', '&#8212;') + \
        _row('Alpha Corp.', '100', '5')
    res = parse_500_schedule(_page(rows, 5))
    assert res['unparsed'] == [] and res['reconciled'] is True
    assert {r['name']: r['value_usd'] for r in res['stock_rows']}['Conduent Inc.'] == 0


def test_name_split_over_three_rows_is_joined_in_full():
    """Failure: 'Fidelity National / Information Services / Inc.' kept only its last part and was mapped to another
    company (Information Services Group)."""
    rows = ('<tr><td></td><td>Fidelity National</td><td></td><td></td></tr>'
            '<tr><td></td><td>Information Services</td><td></td><td></td></tr>'
            '<tr><td></td><td>Inc.</td><td>2,808,529</td><td>150,762</td></tr>')
    res = parse_500_schedule(_page(rows, 150762))
    assert [r['name'] for r in res['stock_rows']] == ['Fidelity National Information Services Inc.']


def test_negative_row_is_kept_apart_from_holdings():
    rows = _row('Holding Corp.', '1,000', '5') + '<tr><td>CDK Global Inc.</td><td>(1,009,196)</td><td>(30,871)</td></tr>'
    res = parse_500_schedule(_page(rows, 5 * 1000 - 30871))
    assert [r['name'] for r in res['negative_rows']] == ['CDK Global Inc.']
    assert all(r['value_usd'] > 0 for r in res['stock_rows'])


# -- identity -------------------------------------------------------------------------------------------------------

def test_v05_evidence_cik_is_read_from_its_own_key():
    """Failure: the v05 CUSIP evidence path was dead because the key was 'SEC ticker corroboration CIK'."""
    ev = v05_evidence_fields('cusip=03743Q108;type=Common Stock;SEC ticker corroboration CIK=1841666;ind=x')
    assert ev['evidence_cik'] == '0001841666' and ev['openfigi_type'] == 'Common Stock'


def test_legal_form_and_state_suffixes_do_not_block_a_match():
    """Failure: 'US BANCORP \\DE\\', 'VERTEX ... / MA', 'O REILLY' and 'N.V.' were not matched to their holdings."""
    assert name_signature('US BANCORP \\DE\\') == name_signature('US Bancorp')
    assert name_signature('VERTEX PHARMACEUTICALS INC / MA') == name_signature('Vertex Pharmaceuticals Inc.')
    assert name_signature("O REILLY AUTOMOTIVE INC") == name_signature("O'Reilly Automotive Inc.")
    assert name_signature('NXP Semiconductors N.V.') == name_signature('NXP Semiconductors NV')
    assert name_signature('CRH PUBLIC LTD CO') == name_signature('CRH PLC')


def test_holding_and_group_words_are_kept():
    """Failure: 'Alphabet Holding Company' was matched to Alphabet Inc because HOLDING and COMPANY were dropped."""
    assert name_signature('Alphabet Holding Company, Inc.') != name_signature('Alphabet Inc.')


def test_reit_is_identified_by_explicit_evidence_not_by_ec():
    """Failure: INVH and MRP were common stock because the EC asset category was used before the REIT evidence."""
    assert security_type('Invitation Homes Inc.', 'Invitation Homes Inc.', '', 'REIT', 'EC')[0] == 'reit'
    assert security_type('Invitation Homes Inc.', 'Invitation Homes Inc.', '', '', 'EC', 'reit')[0] == 'reit'
    assert security_type('Millrose Properties, Inc.', 'Millrose Properties, Inc.', '', '', 'EC')[0] == 'common'
    assert security_type('Anything', '', '6770', '', 'EC')[0] == 'spac'


def test_former_name_is_valid_for_a_short_lag_after_its_end_date():
    """A holding report can still carry the former name a few days after the SEC change (priceline.com, United)."""
    fb = _fake({'0000100517': dict(name='United Airlines Holdings, Inc.',
                                   former=[dict(name='United Continental Holdings, Inc.', valid_from='2010-07-22',
                                                valid_to='2019-06-27')],
                                   filings=[('10-Q', '2019-08-01', '2019-06-30')])})
    assert fb.name_valid_at('0000100517', 'United Continental Holdings Inc', '2019-06-30') is True
    too_late = date.fromisoformat('2019-06-27').toordinal() + NAME_LAG_DAYS + 1
    assert fb.name_valid_at('0000100517', 'United Continental Holdings Inc',
                            date.fromordinal(too_late).isoformat()) is False


def test_successor_date_needs_a_registrant_that_had_not_been_reporting():
    """Failure: Cisco's own 8-K12B of 2021 made it a 'successor' from 2021, so its 2016 holdings were unplaced. A
    successor's 8-K12B counts only when the CIK had no periodic report in the year before it."""
    fb = _fake({'0000858877': dict(name='CISCO SYSTEMS, INC.', former=[], filings=[
                    ('10-Q', '2020-02-01', '2019-12-31'), ('8-K12B', '2021-01-25', '2021-01-25')]),
                '0001841666': dict(name='APA Corp', former=[], filings=[('8-K12B', '2021-03-01', '2021-03-01')])})
    assert fb.succession_date('0000858877') is None
    assert fb.succession_date('0001841666') == '2021-03-01'
    fb._subs['0000858877']['filings'].append(('10-Q', '2016-08-05', '2016-06-30'))
    fb.sub_cache.clear()
    assert fb.exists_at('0000858877', '2016-06-30') is True          # still the issuer in 2016


# -- membership bound and ranking ------------------------------------------------------------------------------------

def _snap(lines: list[dict]) -> dict:
    return dict(lines=lines, form='NPORT-P', snapshot_id='S')


def _line(issuer, usd, typ='common', cands=(), n=1):
    return dict(line_kind='stock', issuer=issuer, value_usd=usd, type=typ, cands=set(cands), raw_name=issuer or 'x',
                line_no=n, sec_key=f'CUSIP:{n:09d}')


def test_company_with_several_classes_takes_one_slot_and_ties_are_by_issuer_id():
    snap = _snap([_line('0000000001', 100.0), _line('0000000001', 50.0, n=2), _line('0000000002', 200.0, n=3),
                  _line('0000000003', 150.0, n=4), _line('0000000004', 999.0, typ='reit', n=5)])
    rk = rank_snapshot(snap)
    assert rk['ranked'] == ['0000000002', '0000000001', '0000000003']   # 150 tie by id; REIT never counts
    assert rk['comp']['0000000001']['cents'] == 15000 and rk['comp']['0000000001']['eligible_lines'] == 2


def test_unresolved_line_that_can_reach_the_200th_company_blocks_the_quarter():
    """Failure: a Cisco-sized unresolved line near the boundary was ignored, so the quarter looked complete."""
    lines = [_line(f'{i:010d}', 1000.0 + i, n=i) for i in range(1, 202)]          # 201 eligible companies
    lines.append(_line(None, 2000.0, cands=['0000000001', '0000000777'], n=900))  # candidate outside the top 200
    bd = bound(rank_snapshot(_snap(lines)))
    assert [b['issuer'] for b in bd['blocking']] == ['0000000001', '0000000777']
    small = lines[:201] + [_line(None, 1.0, cands=['0000000777'], n=901)]       # one dollar cannot reach A200
    assert bound(rank_snapshot(_snap(small)))['blocking'] == []


def test_unresolved_line_with_no_candidate_is_bounded_by_the_gap():
    lines = [_line(f'{i:010d}', 1000.0 * (300 - i), n=i) for i in range(1, 260)]
    lines.append(_line(None, 1e12, cands=(), n=999))
    bd = bound(rank_snapshot(_snap(lines)))
    assert bd['unknown_blocks'] is True


# -- status cutoff, time versions -----------------------------------------------------------------------------------

def test_status_cutoff_is_fixed_and_documented():
    assert R.STATUS_CUTOFF == '2026-10-26'
    assert R.TOP_N == 200 and R.BOUNDARY_RANGE == (180, 220)
    assert 'BRK.A' in R.FORBIDDEN_CANDIDATE and R.FORBIDDEN_CUSIPS['084670108'] == 'BRK.A'


def _version_db(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / 'v.sqlite')
    c.execute('create table pool_versions (version_id text, report_period text, available_at_utc text, '
              'form text, is_canonical int, quarter_state text)')
    c.execute('create table top200_companies (version_id text, rank int, issuer_id text, issuer_name_at_period text, '
              'amount_cents int, fund_weight_raw real, tie_group int, blocked_members int)')
    c.execute('create table strategy_companies (version_id text, historical_rank int, issuer_id text, current_status '
              'text, resolved_current_issuer text, current_ticker text, strategy_eligible int, blocked_reason text, '
              'excluded_reason text)')
    c.executemany('insert into pool_versions values (?,?,?,?,?,?)', [
        ('A', '2019-12-31', '2020-02-20T10:00:00Z', 'N-CSR', 0, 'alternate_version'),
        ('B', '2019-12-31', '2020-03-15T10:00:00Z', 'NPORT-P', 1, 'ready'),
        ('C', '2020-03-31', '2020-05-01T10:00:00Z', 'NPORT-P', 1, 'ready')])
    c.executemany('insert into top200_companies values (?,?,?,?,?,?,?,?)', [
        ('A', 1, 'CIK:1', 'x', 1, 0.1, 0, 0), ('B', 1, 'CIK:2', 'y', 1, 0.1, 0, 0), ('C', 1, 'CIK:3', 'z', 1, 0.1, 0, 0)])
    c.commit()
    return c


def test_time_query_returns_nothing_before_the_first_list(tmp_path):
    c = _version_db(tmp_path)
    assert version_as_of(c, '2019-12-31T23:59:59Z') is None
    assert members_as_of(c, '2019-12-31T23:59:59Z')['status'] == 'no list public yet'


def test_late_revision_of_an_older_period_is_not_visible_before_it_was_public(tmp_path):
    c = _version_db(tmp_path)
    assert version_as_of(c, '2020-02-20T09:59:59Z') is None                     # before the first version of 2019-12-31
    assert version_as_of(c, '2020-02-20T10:00:00Z')['version_id'] == 'A'        # the first public version of 2019-12-31
    assert version_as_of(c, '2020-03-15T09:59:59Z')['version_id'] == 'A'        # not yet the later filing
    assert version_as_of(c, '2020-03-15T10:00:00Z')['version_id'] == 'B'        # the later filing, its own time
    assert version_as_of(c, '2020-04-30T23:59:59Z')['version_id'] == 'B'        # no new list: the last one stays
    assert version_as_of(c, '2020-05-01T10:00:00Z')['version_id'] == 'C'        # the newer period replaces it


def test_strategy_list_only_for_canonical_versions(tmp_path):
    c = _version_db(tmp_path)
    assert members_as_of(c, '2020-02-20T10:00:00Z')['strategy'] == []


# -- helpers --------------------------------------------------------------------------------------------------------

class _FakeSub(Build):
    def __init__(self, subs):
        super().__init__(RawStore(offline=True), log=lambda m: None)
        self._subs = subs

    def issuer_sub(self, cik):
        s = self._subs.get(cik)
        if s is None:
            return None
        return dict(cik=cik, name=s['name'], sic='', sic_description='', state='', exchanges=[], tickers=[],
                    former=[dict(name=f['name'], valid_from=f['valid_from'], valid_to=f['valid_to'])
                            for f in s['former']],
                    filings=[dict(form=f, filing_date=d, report_date=r, accession='', document='', acceptance='')
                             for f, d, r in s['filings']], source_sha='')


def _fake(subs: dict) -> _FakeSub:
    return _FakeSub(subs)
