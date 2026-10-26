"""Regressions found in the read-only review of the promoted Top 200 build."""
import sqlite3
from types import SimpleNamespace
from leadtrend_db.top200 import rank_snapshot, bound, current_security, cents
from leadtrend_db.query import members_as_of, version_as_of, main_query
from leadtrend_db.checks import check_readiness
from leadtrend_db.identity import class_code_from_text, issuer_id
from tests_db.test_top200_units import _line, _snap, _version_db


def test_unresolved_candidate_and_unknown_amounts_can_jointly_change_membership():
    lines = [_line('A', 100), _line('B', 90, n=2), _line('C', 80, n=3),
             _line(None, 6, cands=['C'], n=4), _line(None, 5, n=5)]
    b = bound(rank_snapshot(_snap(lines)), top_n=2)
    assert b['unknown_blocks'] or b['blocking']  # C could actually be 91.


def test_unresolved_amount_can_change_ranks_inside_selected_companies():
    lines = [_line('A', 100), _line('B', 90, n=2), _line('C', 80, n=3),
             _line(None, 20, cands=['B'], n=4)]
    assert bound(rank_snapshot(_snap(lines)), top_n=2)['unknown_blocks']


def test_blocked_query_is_not_success_and_cli_exits_nonzero(tmp_path, capsys):
    c = _version_db(tmp_path)
    c.execute("update pool_versions set quarter_state='blocked' where version_id='A'")
    c.commit()
    assert members_as_of(c, '2020-02-21T00:00:00Z')['status'] == 'blocked'
    assert main_query(tmp_path/'v.sqlite', None, '2020-02-21T00:00:00Z') == 1


def test_late_old_period_revision_never_replaces_public_new_period(tmp_path):
    c = _version_db(tmp_path)
    c.execute("insert into pool_versions values ('D','2018-12-31','2020-06-01T00:00:00Z','N-CSR',0,'ready')")
    assert version_as_of(c, '2020-07-01T00:00:00Z')['version_id'] == 'C'
    assert version_as_of(c, '2020-05-01T11:00:00+01:00')['version_id'] == 'C'


def test_failed_quarter_check_prevents_promotion_even_if_sql_ranks_match():
    c=sqlite3.connect(':memory:')
    c.execute('create table quarter_checks(version_id text,check_name text,status text,detail text)')
    c.execute('create table pool_versions(version_id text,quarter_state text,quarter_reason text)')
    c.execute("insert into quarter_checks values ('Q','membership_bound','fail','unknown')")
    c.execute("insert into pool_versions values ('Q','blocked','unknown')")
    assert check_readiness(c,{})


def test_successor_security_keeps_historical_class_and_resolves_current_class():
    b=SimpleNamespace(overrides=[],sec_facts={
        'OLD':dict(issuer='old',cls='CL_A',last='2014-12-31'),
        'NEW_A':dict(issuer='new',cls='CL_A',last='2026-06-30'),
        'NEW_C':dict(issuer='new',cls='CL_C',last='2026-06-30')},
        sec_type={k:('common','test') for k in ['OLD','NEW_A','NEW_C']},
        security_tickers={'NEW_A':'GOOGL','NEW_C':'GOOG'},end_over={})
    assert current_security(b,'OLD','new') == ('NEW_A','GOOGL')


def test_current_ticker_uses_validated_identifier_even_if_v05_ticker_blank():
    b=SimpleNamespace(overrides=[],sec_facts={'LIN':dict(issuer='linde',cls='COM',last='2026-06-30')},
                      sec_type={'LIN':('common','test')},security_tickers={'LIN':'LIN'},end_over={})
    assert current_security(b,'LIN','linde') == ('LIN','LIN')


def test_special_class_a_is_not_ordinary_class_a():
    assert class_code_from_text('Comcast Special Class A') == 'CL_A_SPECIAL'
    assert class_code_from_text('Comcast Class A') == 'CL_A'
    assert issuer_id('LEI:VLP5AP0XK2GM6GKD1007') == 'LEI:VLP5AP0XK2GM6GKD1007'


def test_decimal_cents_and_nonpositive_rows():
    assert cents('1234567890.07') == 123456789007
    assert rank_snapshot(_snap([_line('A',-1),_line('B',0),_line('C',1)]))['ranked'] == ['C']


def test_all_nport_versions_are_retained(tmp_path):
    from leadtrend_db.build import Build
    from leadtrend_db.sources import RawStore
    b=Build(RawStore(raw_dir=tmp_path/'raw',offline=True),log=lambda _:None)
    b._nport_snapshot=lambda pick,acceptance,filed:pick['accession']
    trust=[dict(accession='a',acceptance='2020-01-01',filing_date='2020-01-01'),
           dict(accession='b',acceptance='2020-01-02',filing_date='2020-01-02')]
    screen={x:dict(accession=x,report_date='2019-12-31',decision='target') for x in ['a','b']}
    assert b.nport_snapshots(trust,screen) == ['a','b']


def test_dated_type_override_does_not_project_reit_status_into_earlier_quarters():
    from leadtrend_db.build import Build
    b=SimpleNamespace(v05={},overrides=[dict(override_kind='security_type',key='CUSIP:CCI',value='common',
        field='2013-12-31..2013-12-31',evidence='official conversion January 1, 2014')],
        _window_ok=Build._window_ok,line_type=lambda ln,ev:('reit','current type'))
    snaps=[dict(report_date=d,lines=[dict(sec_key='CUSIP:CCI',line_kind='stock')])
           for d in ['2013-12-31','2014-03-31']]
    Build.assign_types(b,snaps)
    assert snaps[0]['lines'][0]['type']=='common'
    assert snaps[1]['lines'][0]['type']=='reit'
    assert b.sec_type['CUSIP:CCI'][0]=='reit'


def test_company_name_digits_are_not_footnotes_even_across_rows():
    from tests_db.test_top200_units import _page,_row
    from leadtrend_db.parse_ncsr import parse_500_schedule
    text=_row('Phillips 66','100','20')+'<tr><td>Level 3</td></tr>'+_row('Communications Inc.','200','30')
    text+=_row('Example Corp.<sup>1</sup>','300','40')
    parsed=parse_500_schedule(_page(text,90))
    assert [r['name'] for r in parsed['stock_rows']]==['Phillips 66','Level 3 Communications Inc.','Example Corp.']


def test_nport_quarter_date_takes_precedence_over_fiscal_year_end():
    from leadtrend_db.parse_nport import holdings_lines
    raw=b'<root><repPdEnd>2019-12-31</repPdEnd><repPdDate>2019-09-30</repPdDate></root>'
    assert holdings_lines(raw)['report_date']=='2019-09-30'
