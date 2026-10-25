"""Boundaries, identity continuity, frozen capacity and event ownership."""
import json
from datetime import date,timedelta
from dataclasses import replace
from pathlib import Path
import pytest
from leadtrend_v05.stage3 import guard_development,run_dev
from leadtrend_v05.stage3_scoring import ScoreEngine
from leadtrend_v05.stage3_ledger import DevBook,SecurityReceivable
from leadtrend_v05.stage2_repair import canonical_aliases,coalesce
from leadtrend_v05.dataio import EventRow,Inputs
from leadtrend_v05.scoring import TRIResult
from leadtrend_v05.selection import Candidate,select_portfolio
from leadtrend_v05.ledger import AccountState
from leadtrend_v05.execution import plan_equal_weight_trades
from leadtrend_v05.stage3_metrics import longest_drawdown
from conftest import make_config,price
from leadtrend_v05 import paths

CONFIG={'start_date':'2019-11-29','end_date':'2024-06-28','L':20,'N':10,'k':3,'b':2,
        'initial_capital':100000,'fee_per_trade':1,'slippage_bps':5}
REGISTRY={'development':{'start':'2019-11-29','end':'2024-06-28'},'holdout':{'start':'2024-07-05'}}

@pytest.mark.parametrize('end',['2024-07-01','2024-07-05','2026-10-23'])
def test_holdout_config_and_cli_end_rejected_before_load_or_output(tmp_path,end):
    (tmp_path/paths.REGISTRY).mkdir(parents=True)
    (tmp_path/paths.STAGE2_REGISTRY).write_text(json.dumps(REGISTRY))
    cfg=tmp_path/'cfg.json';cfg.write_text(json.dumps(CONFIG))
    out=tmp_path/'forbidden'
    with pytest.raises(ValueError,match='holdout protection'):run_dev(tmp_path,cfg,out,end_override=end)
    assert not out.exists()
    with pytest.raises(ValueError,match='holdout protection'):guard_development(dict(CONFIG,end_date=end),REGISTRY)

def test_score_engine_never_constructs_holdout_tri_and_refuses_signal(tmp_path,monkeypatch):
    cut=date(2024,6,28);future=date(2024,7,5);calls=[]
    def guarded(rows,events,dates):
        calls.append((rows,dates));assert all(r.session_date<=cut for r in rows);assert all(d<=cut for d in dates)
        return TRIResult()
    monkeypatch.setattr('leadtrend_v05.stage3_scoring.build_tri',guarded)
    inputs=Inputs(tmp_path,make_config(),[],[],[],[],[price('S',cut,10),price('S',future,999)],[])
    engine=ScoreEngine(inputs,[cut,future],tmp_path,cut)
    assert calls
    with pytest.raises(ValueError,match='holdout'):engine.score('S',future,1)
    # Availability can inspect a missing future point without calculating return.
    assert engine.availability('S',future,1)==''

def test_vendor_spinoff_only_blocks_intersecting_windows(tmp_path):
    dates=[date(2024,1,2)+timedelta(days=i) for i in range(8)]
    e=EventRow('SPIN','S','spinoff',dates[2],None,None,'',None,'vendor',10.,'vendor_factor')
    inputs=Inputs(tmp_path,make_config(),[],[],[],[],[price('S',d,100) for d in dates],[e])
    engine=ScoreEngine(inputs,dates,tmp_path,dates[-1])
    assert engine.score('S',dates[3],2)[4]=='spinoff_vendor_valuation_in_window'
    assert engine.score('S',dates[5],2)[0] is not None

def test_alias_merge_keeps_member_continuity_and_prices_exact():
    sec=[{'security_id':'ISIN:US1234567890','ticker':'S','issuer_id':'CUSIP:N/A','security_type':'common_stock'},
         {'security_id':'CUSIP:123456789','ticker':'S','issuer_id':'LEI:A','security_type':'common_stock'}]
    manifest=[dict(security_id=s['security_id'],snapshot_id=str(i),holdings_as_of='2020-01-01',isin='US1234567890') for i,s in enumerate(sec)]
    aliases,audit=canonical_aliases(sec,manifest)
    assert aliases[sec[0]['security_id']]==aliases[sec[1]['security_id']]
    ps=[dict(security_id=s['security_id'],session_date='2020-01-01',open='10',close='11',volume='1',turnover='10') for s in sec]
    assert len(coalesce(ps,['security_id','session_date'],aliases))==1
    ps[-1]['close']='12'
    with pytest.raises(ValueError,match='conflicting alias price'):coalesce(ps,['security_id','session_date'],aliases)

def test_alias_does_not_merge_simultaneous_different_share_classes():
    sec=[dict(security_id=x,ticker='S',issuer_id='LEI:A',security_type='common_stock') for x in ['A','B']]
    manifest=[dict(security_id=x,snapshot_id='same',holdings_as_of='2020-01-01',isin=x) for x in ['A','B']]
    with pytest.raises(ValueError,match='simultaneous'):canonical_aliases(sec,manifest)

def test_frozen_capacity_industry_and_issuer_are_counted_before_fill():
    frozen=[Candidate('F','ISS_F','TECH',0),Candidate('G','ISS_G','OTHER',0)]
    candidates=[Candidate('S'+str(i),'ISS'+str(i),'TECH' if i<5 else 'I'+str(i),100-i) for i in range(20)]
    candidates.append(Candidate('F2','ISS_F','DIFFERENT',999))
    result=select_portfolio(candidates,[],10,3,2,frozen=frozen)
    by={c.security_id:c for c in candidates}
    assert len(result.target_ids)==8
    assert sum(by[s].industry=='TECH' for s in result.target_ids)<=2
    assert 'F2' not in result.target_ids

@pytest.mark.parametrize('Z',[10,11])
def test_frozen_at_or_above_N_no_new_positions_and_no_asset_deletion(Z):
    frozen=[Candidate('F'+str(i),'I'+str(i),'TECH',0) for i in range(Z)]
    a=AccountState(100,{'F'+str(i):1 for i in range(Z)})
    result=select_portfolio([Candidate('A','IA','X',1)],[],10,3,2,frozen=frozen)
    assert result.target_ids==[] and len(a.positions)==Z
    assert ('passive_frozen_limit_exceeded' in result.notes)

def test_frozen_and_receivable_values_do_not_enter_available_cash():
    book=DevBook(AccountState(50,{'F':5}));book.frozen['F']='verified halt'
    book.account.receivable_cash=25
    book.security_receivables.append(SecurityReceivable('E','C',2,date(2024,1,2),'delivery_pending','official'))
    parts=book.components({'F':100,'C':10})
    assert parts['total_equity']==595 and parts['cash']==50
    assert parts['frozen_stock_value']==500 and book.frozen_ids()=={'F','C'}

def test_spinoff_is_visible_parent_unchanged_and_replay_idempotent():
    book=DevBook(AccountState(0,{'P':5}));d=date(2024,1,2)
    e=EventRow('E','P','spinoff',d,None,None,'',None,'official',20,'child_raw_close')
    children=[dict(security_id='C',shares_per_old=.2,source='official')]
    book.spinoff(e,children,{}, {'P':80,'C':100});book.spinoff(e,children,{}, {'P':80,'C':100})
    assert book.account.positions=={'P':5,'C':1}
    assert book.components({'P':80,'C':100})['total_equity']==500
    assert book.account.cash==0 and len(book.event_rows)==1

def test_fractional_spinoff_is_restricted_not_invented_cash():
    book=DevBook(AccountState(0,{'P':3}));d=date(2024,1,2)
    e=EventRow('E','P','spinoff',d,None,None,'',None,'official',20,'child_raw_close')
    book.spinoff(e,[dict(security_id='C',shares_per_old=.2,source='official')],{}, {'P':80,'C':100})
    assert book.account.cash==0 and book.account.shares_of('C')==0
    assert book.security_receivables[0].shares==pytest.approx(.6)
    assert book.components({'P':80,'C':100})['total_equity']==pytest.approx(300)
    assert book.frozen_ids()=={'C'}

def test_simultaneous_reverse_split_and_spinoff_use_previous_close_entitlement():
    book=DevBook(AccountState(0,{'P':6}));d=date(2024,1,2)
    split=EventRow('S','P','reverse_split',d,1/3,None,'',None,'official')
    spin=EventRow('E','P','spinoff',d,None,None,'',None,'official',80,'child_raw_close')
    book.split(split,60)
    book.spinoff(spin,[dict(security_id='C',shares_per_old=1,source='official')],{}, {'P':60,'C':80},entitled_shares=6)
    assert book.account.positions=={'P':2,'C':6}
    assert book.components({'P':60,'C':80})['total_equity']==600

def test_actual_and_pending_same_security_use_one_slot_and_unverified_partial_delivery_stops():
    book=DevBook(AccountState(0,{'C':1}))
    book.security_receivables.append(SecurityReceivable('E','C',.2,date(2024,1,2),'cash_in_lieu_unverified','official'))
    assert book.frozen_ids()=={'C'}
    with pytest.raises(ValueError,match='partially delivered'):book.require_resolved_partial_delivery()
    assert book.account.shares_of('C')==1 and book.security_receivables[0].shares==.2

def test_dividend_ex_date_receivable_only_then_verified_payment_once():
    book=DevBook(AccountState(0,{'P':10}));d=date(2024,1,2)
    e=EventRow('D','P','cash_dividend',d,None,1,'pre_event_share',d+timedelta(days=2),'official')
    book.dividend(e,10);book.dividend(e,10)
    assert book.account.cash==0 and book.account.receivable_cash==10
    book.pay(d+timedelta(days=1));assert book.account.cash==0
    book.pay(d+timedelta(days=2));book.pay(d+timedelta(days=2))
    assert book.account.cash==10 and book.account.receivable_cash==0

def test_no_swap_has_no_trades_despite_drift_or_available_dividend():
    a=AccountState(1000,{'S':2})
    assert plan_equal_weight_trades(a,['S'],{'S':500},1,.0005,date(2024,1,2),False)==[]

def test_allocation_uses_N_minus_frozen_slots_even_if_underfilled():
    a=AccountState(8000)
    trades=plan_equal_weight_trades(a,['S'],{'S':100},1,0,date(2024,1,2),True,allocation_slots=8)
    assert trades[0].shares==9 and a.cash==7099

def test_drawdown_duration_starts_at_peak_and_reports_trough_recovery():
    ds=[date(2024,1,1)+timedelta(days=i) for i in range(5)]
    result=longest_drawdown([100,90,80,95,100],ds)
    assert result['longest_underwater_calendar_days']==4
    assert result['maximum_drawdown_recovery_from_trough_days']==2

def _bounded_run_fixture(tmp_path,monkeypatch,missing_held=False,gaps=(),events=()):
    """Synthetic 2019 segment. missing_held drops two consecutive sessions of every held name; gaps drops (sid, date) pairs."""
    import csv
    from conftest import cal_row,sec,snap,member,utc
    from leadtrend_v05.stage2_enrich import write_csv
    root=tmp_path;w=root/paths.WORK;n=root/paths.NORMALIZED;w.mkdir(parents=True);n.mkdir(parents=True);(root/paths.REGISTRY).mkdir(parents=True)
    config=dict(CONFIG,end_date='2019-12-13');cfg=root/'cfg.json';cfg.write_text(json.dumps(config))
    registry={'development':{'start':'2019-11-29','end':'2019-12-13'},'holdout':{'start':'2019-12-20'},
        'status':'registered_before_viewing_any_real_returns','data_version':{}}
    (root/paths.STAGE2_REGISTRY).write_text(json.dumps(registry))
    (root/paths.STAGE2_ACCEPTANCE).parent.mkdir(parents=True);(root/paths.STAGE2_ACCEPTANCE).write_text('{"status":"PASS"}')
    (root/paths.WEEKLY_COVERAGE).parent.mkdir(parents=True)
    write_csv(root/paths.WEEKLY_COVERAGE,[dict(signal_session_date=d,n_eligible=12,historical_no_price_ratio=0) for d in ['2019-11-29','2019-12-06','2019-12-13']],['signal_session_date','n_eligible','historical_no_price_ratio'])
    write_csv(n/'spinoff_child_prices.csv',[],['event_id','child_ticker','session_date'])
    ds=[date(2019,10,1)+timedelta(days=i) for i in range(90) if (date(2019,10,1)+timedelta(days=i)).weekday()<5]
    securities=[sec('S'+str(i),industry='I'+str(i//2)) for i in range(12)]+[sec('VOO_ETF',stype='etf')]
    dropped=set(gaps)|({(s.security_id,d) for s in securities if s.security_id!='VOO_ETF' for d in (date(2019,12,3),date(2019,12,4))} if missing_held else set())
    ps=[price(s.security_id,d,100+j*.1+i*.01) for i,s in enumerate(securities) for j,d in enumerate(ds)
        if (s.security_id,d) not in dropped]
    inputs=Inputs(n,make_config(benchmark_security_id='VOO_ETF'),[cal_row(d) for d in ds],securities,
        [snap('SNAP',date(2019,10,1),utc(2019,11,1))],[member('SNAP',s.security_id) for s in securities if s.security_id!='VOO_ETF'],ps,list(events))
    monkeypatch.setattr('leadtrend_v05.stage3.load_inputs',lambda *a,**kw:inputs)
    monkeypatch.setattr('leadtrend_v05.stage3.load_config',lambda *a:inputs.config)
    out=w/'runs/dev';code=run_dev(root,cfg,out)
    return code,out

def test_complete_run_artifacts_contain_no_holdout_economic_dates(tmp_path,monkeypatch):
    import csv
    code,out=_bounded_run_fixture(tmp_path,monkeypatch)
    assert code==0
    observed=0
    for path in out.glob('*.csv'):
        with path.open(encoding='utf-8-sig',newline='') as handle:
            for r in csv.DictReader(handle):
                for key in ('session_date','signal_session_date','execution_session'):
                    if r.get(key):assert r[key]<='2019-12-13';observed+=1
    assert observed>100
    assert json.loads((out/'summary.json').read_text(encoding='utf-8'))['comparison_end_session']=='2019-12-13'

def test_unknown_held_price_gap_stops_and_preserves_real_assets(tmp_path,monkeypatch):
    # Two consecutive sessions without a price: the first is valued at the previous close, the second stops.
    code,out=_bounded_run_fixture(tmp_path,monkeypatch,missing_held=True)
    assert code==2
    state=json.loads((out/'last_explainable_state.json').read_text(encoding='utf-8'))
    assert state['positions'] and state['last_valued_session']=='2019-12-03'
    assert state['attempted_session']=='2019-12-04'
    assert 'price interrupted' in state['message']
    summary=json.loads((out/'summary.json').read_text(encoding='utf-8'))
    assert summary['status']=='incomplete' and 'total_return' not in summary

def _rows(path):
    import csv
    with path.open(encoding='utf-8-sig',newline='') as handle:return list(csv.DictReader(handle))

def test_single_session_gap_values_held_name_at_previous_close_and_completes(tmp_path,monkeypatch):
    dropped=[(f'S{i}',date(2019,12,3)) for i in range(12)]
    code,out=_bounded_run_fixture(tmp_path,monkeypatch,gaps=dropped)
    assert code==0
    summary=json.loads((out/'summary.json').read_text(encoding='utf-8'))
    assert summary['status']=='completed' and summary['accounting_rules_version']=='v2' and summary['single_session_stale_marks']>0
    marks={(r['session_date'],r['security_id']):float(r['mark']) for r in _rows(out/'positions.csv')}
    held=[sid for (day,sid) in marks if day=='2019-12-03']
    assert held
    for sid in held:assert marks[('2019-12-03',sid)]==marks[('2019-12-02',sid)]
    manifest=json.loads((out/'run_manifest.json').read_text(encoding='utf-8'))
    assert manifest['accounting_rules_version']=='v2'

def test_signal_session_gap_retains_held_name_without_sale(tmp_path,monkeypatch):
    code,out=_bounded_run_fixture(tmp_path,monkeypatch,gaps=[('S0',date(2019,12,6))])
    assert code==0
    decisions=[r for r in _rows(out/'decisions.csv') if r['signal_session_date']=='2019-12-06' and r['security_id']=='S0']
    assert decisions and decisions[0]['action']=='retain_frozen' and decisions[0]['reason']=='price_gap_retained_held'
    trades_s0=[r for r in _rows(out/'trades.csv') if r['security_id']=='S0' and r['session_date']=='2019-12-09']
    assert trades_s0==[]
    weeks=[r for r in _rows(out/'positions.csv') if r['session_date']=='2019-12-09' and r['security_id']=='S0']
    assert weeks

def test_execution_session_without_prices_voids_those_orders_and_nothing_is_postponed(tmp_path,monkeypatch):
    dropped=[(f'S{i}',date(2019,12,2)) for i in range(6)]
    code,out=_bounded_run_fixture(tmp_path,monkeypatch,gaps=dropped)
    assert code==0
    first=[r for r in _rows(out/'trades.csv') if r['session_date']=='2019-12-02']
    assert first and not {r['security_id'] for r in first}&{f'S{i}' for i in range(6)}
    summary=json.loads((out/'summary.json').read_text(encoding='utf-8'))
    assert summary['voided_orders']>0
