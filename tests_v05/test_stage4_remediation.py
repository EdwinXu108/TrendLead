from datetime import date,timedelta
from types import SimpleNamespace
import pytest
from leadtrend_v05.ledger import AccountState
from leadtrend_v05.stage3_ledger import DevBook
from leadtrend_v05.stage4_remediation import ensure_no_holdout
from leadtrend_v05 import paths

def test_fractional_cash_is_once_receivable_until_authorized_payment():
 d=date(2024,4,2);pay=date(2024,4,16)
 b=DevBook(AccountState(100,{'parent':7}),fractional_settlements={'child|2024-04-02':dict(price=140.,pay_date=pay.isoformat(),valuation='approximation',source='investor fixture')})
 e=SimpleNamespace(event_id='spin',security_id='parent',effective_session=d)
 child=[dict(security_id='child',shares_per_old=.25,source='fixture')];marks={'parent':50.,'child':140.}
 b.spinoff(e,child,{},marks);b.spinoff(e,child,{},marks)
 assert b.account.positions=={'parent':7,'child':1}
 assert b.account.receivable_cash==105 and b.account.cash==100
 assert len(b.account.receivables)==1 and len(b.fractional_rows)==1
 assert b.fractional_rows[0]['fractional_cash_valuation']=='approximation'
 assert b.fractional_rows[0]['equity_at_confirmation']==695
 assert b.components(marks)['total_equity']==695
 assert b.frozen_ids()==set();b.require_resolved_partial_delivery()
 b.pay(pay-timedelta(days=1));assert b.account.cash==100
 b.pay(pay);assert b.account.cash==205 and b.account.receivable_cash==0
 b.pay(pay);assert b.account.cash==205 and len(b.cash_flows)==1
 assert b.cash_flows[0]['kind']=='fractional_cash_payment'

def test_no_authorization_keeps_fractional_right_and_stops():
 b=DevBook(AccountState(100,{'parent':7}));e=SimpleNamespace(event_id='spin',security_id='parent',effective_session=date(2024,4,2))
 b.spinoff(e,[dict(security_id='child',shares_per_old=.25,source='fixture')],{}, {'parent':50,'child':140})
 with pytest.raises(ValueError,match='partially delivered'):b.require_resolved_partial_delivery()
 assert b.account.receivable_cash==0 and b.account.cash==100

def test_holdout_blocked_until_investor_authorization_file_exists(tmp_path):
 with pytest.raises(ValueError,match='fresh Claude review'):ensure_no_holdout(tmp_path)
 (tmp_path/paths.HOLDOUT).mkdir(parents=True);(tmp_path/paths.HOLDOUT/'authorization.json').write_text('{}')
 ensure_no_holdout(tmp_path)

def test_auxiliary_score_event_repair_does_not_mutate_registered_input(tmp_path):
 import json
 from dataclasses import dataclass
 from leadtrend_v05.stage4_remediation import scoring_input_supplement
 @dataclass
 class Event:
  event_id:str='spin'
  spinoff_valuation:str='vendor_factor'
  spinoff_value_per_old_share:float=15.66
  source:str='original'
 @dataclass
 class Inputs:
  events:list
 p=tmp_path/paths.OVERLAYS;p.mkdir(parents=True)
 (p/'score_event_overlay.json').write_text(json.dumps(dict(files={},events=[dict(event_id='spin',value_per_old_share=58.23/3,source='auxiliary raw quote')])))
 original=Inputs([Event()]);hashes={};updated=scoring_input_supplement(tmp_path,original,hashes)
 assert original.events[0].spinoff_valuation=='vendor_factor'
 assert original.events[0].spinoff_value_per_old_share==15.66
 assert updated.events[0].spinoff_valuation=='child_raw_close'
 assert updated.events[0].spinoff_value_per_old_share==58.23/3
 assert len(hashes)==1

def test_auxiliary_price_supplement_never_enters_candidate_inputs(tmp_path):
 import json
 from leadtrend_v05.stage4_remediation import supplement
 from leadtrend_v05.stage4_registry import digest
 base=tmp_path/paths.OVERLAYS;base.mkdir(parents=True)
 q=tmp_path/'aux.csv';q.write_text('session_date,open,close,volume,turnover\n2021-08-03,48,58.23,100,5000\n2024-07-05,100,101,100,10000\n')
 (base/'data_version.json').write_text(json.dumps(dict(files={'aux.csv':digest(q)},auxiliary_prices='aux.csv',children=[dict(event_id='spin',session_date='2021-08-03')],fractional_settlements={})))
 sec={};px={};children={'spin':[]};h={};original_members={'parent'}
 supplement(tmp_path,sec,px,children,h,date(2024,6,28))
 assert set(sec)=={'CHILD:VSCO'}
 assert ('CHILD:VSCO',date(2021,8,3)) in px
 assert ('CHILD:VSCO',date(2024,7,5)) not in px
 assert original_members=={'parent'}
 assert children['spin'][0]['security_id']=='CHILD:VSCO'
 q.write_text(q.read_text()+'bad')
 with pytest.raises(ValueError,match='supplement changed'):supplement(tmp_path,{}, {}, {'spin':[]},{},date(2024,6,28))

def test_supplement_manifest_seal_rejects_policy_change(tmp_path):
 from leadtrend_v05.stage4_remediation import verify_supplement_seal
 from leadtrend_v05.stage4_registry import digest
 p=tmp_path/'policy.json';p.write_text('{"price":140}')
 p.with_suffix('.json.sha256').write_text(digest(p))
 verify_supplement_seal(p)
 p.write_text('{"price":150}')
 with pytest.raises(ValueError,match='manifest hash mismatch'):verify_supplement_seal(p)

def test_fractional_receivable_cannot_fund_orders_until_it_is_paid():
 from leadtrend_v05.execution import plan_exit_cash_trades
 d=date(2024,4,2);due=date(2024,4,16)
 book=DevBook(AccountState(100,{'parent':7}),fractional_settlements={'child|2024-04-02':dict(price=140.,pay_date=due.isoformat(),valuation='approximation',source='authorized fixture')})
 book.spinoff(SimpleNamespace(event_id='spin',security_id='parent',effective_session=d),[dict(security_id='child',shares_per_old=.25,source='fixture')],{}, {'parent':50.,'child':140.})
 target=['parent','child','new'];quotes={'new':150.}
 assert plan_exit_cash_trades(book.account,target,quotes,1.,0.,d,True)==[]
 assert book.account.receivable_cash==105 and book.account.cash==100
 book.pay(due)
 orders=plan_exit_cash_trades(book.account,target,quotes,1.,0.,due,True)
 assert len(orders)==1 and orders[0].security_id=='new' and orders[0].shares==1
 assert book.account.cash==54 and book.account.receivable_cash==0
