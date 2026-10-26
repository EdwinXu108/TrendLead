"""Permanent exclusion must not change historical ranks or transfer predecessor quarters."""
from types import SimpleNamespace
from leadtrend_db import rules as R, top200


def _run(monkeypatch):
    excluded='0000000001'; target='0000000202'
    monkeypatch.setattr(R,'permanent_exclusions',lambda: {'CIK:'+excluded:{}})
    facts={f'S:{i}':dict(issuer=f'{i:010d}',cls='COM',last='2020-12-31') for i in range(1,203)}
    b=SimpleNamespace(sec_facts=facts,sec_type={k:('common','') for k in facts},end_over={},overrides=[],review=[],
                      security_tickers={k:'T'+k[2:] for k in facts})
    monkeypatch.setattr(top200,'_tickers_now',lambda b,c:'T'+str(int(c)))
    monkeypatch.setattr(top200,'current_security',lambda b,s,c:(s,b.security_tickers[s]))
    def snap(key,period,ids):
        return dict(snapshot_id=key,report_date=period,acceptance=period+'T12:00:00Z',accession=key,
            filing_date=period,form='NPORT-P',completeness='full',reconciled=True,total=1000000,raw=b'x',
            lines=[dict(line_kind='stock',issuer=f'{i:010d}',value_usd=1000-i,type='common',cands=set(),
                        raw_name=str(i),line_no=i,sec_key=f'S:{i}') for i in ids])
    snaps=[snap('OLD','2019-12-31',range(1,202)),snap('NEW','2020-12-31',[*range(2,201),202])]
    final={f'{i:010d}':dict(status='in_index',needs_review=0) for i in range(1,203)}
    return top200.compute(b,snaps,snaps,{},final,{},R.STATUS_CUTOFF)


def test_permanent_exclusion_applies_even_if_status_is_in_index(monkeypatch):
    t=_run(monkeypatch)
    row=next(r for r in t['strategy_companies'] if r['issuer_id']=='CIK:0000000001')
    assert row['strategy_eligible']==0 and row['excluded_reason']=='investor_permanent_exclusion'
    assert not any(r['strategy_eligible'] for r in t['strategy_securities'] if r['issuer_id']==row['issuer_id'])


def test_exclusion_does_not_refill_or_change_historical_ranking(monkeypatch):
    t=_run(monkeypatch)
    historical=[r for r in t['top200_companies'] if r['version_id']=='OLD']
    strategy=[r for r in t['strategy_companies'] if r['version_id']=='OLD']
    assert len(historical)==200 and historical[0]['issuer_id']=='CIK:0000000001'
    assert sum(r['strategy_eligible'] for r in strategy)==199
    assert 'CIK:0000000201' not in {r['issuer_id'] for r in strategy}


def test_new_company_keeps_own_start_and_survivor_remains(monkeypatch):
    t=_run(monkeypatch)
    selected={(r['version_id'],r['issuer_id']) for r in t['strategy_companies'] if r['strategy_eligible']}
    assert ('OLD','CIK:0000000002') in selected and ('NEW','CIK:0000000002') in selected
    assert ('NEW','CIK:0000000202') in selected and ('OLD','CIK:0000000202') not in selected


def test_query_default_companies_cannot_leak_excluded_audit_rows(tmp_path):
    from tests_db.test_top200_units import _version_db
    from leadtrend_db.query import members_as_of
    c=_version_db(tmp_path)
    c.execute("update strategy_companies set strategy_eligible=0,excluded_reason='investor_permanent_exclusion'")
    c.execute('update strategy_securities set strategy_eligible=0')
    result=members_as_of(c,'2020-02-20T10:00:00Z')
    assert result['companies']==[] and result['eligible_securities']==[]
    assert len(result['historical_companies'])==1
