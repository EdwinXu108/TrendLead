"""Synthetic registration/gate tests; never run an extra real parameter point."""
import json
from datetime import date
from pathlib import Path
import pytest
from leadtrend_v05.stage4_registry import points,register,verify,digest
from leadtrend_v05.stage4_summary import neighbors,evaluate_regions,validate_holdout_decision,run_holdout,interval
from leadtrend_v05.stage2_enrich import write_csv
from leadtrend_v05.execution import plan_exit_cash_trades
from leadtrend_v05.ledger import AccountState
from leadtrend_v05.stage3_ledger import DevBook
from leadtrend_v05.dataio import EventRow
from leadtrend_v05 import paths

def _registry(root):
    (root/paths.REGISTRY).mkdir(parents=True)
    old={'data_version':{},'development':{'start':'2019-11-29','end':'2024-06-28','n_weeks':240},
         'holdout':{'start':'2024-07-05','end':'2026-10-23','n_weeks':121}}
    (root/paths.STAGE2_REGISTRY).write_text(json.dumps(old))
    for f in [root/paths.STAGE3_RESULTS,root/paths.STAGE3_RESULTS/'stress_10bps']:
        f.mkdir(parents=True,exist_ok=True)
        for name in ['trades.csv','daily_equity.csv','benchmark.csv','summary.json']:(f/name).write_text('synthetic test fixture')
    return register(root)

def _rows(reg):
    return [dict(configuration_id=p['id'],cost_bps=cost,status='completed',acceptance_status='PASS',annualized_excess=.1,
                 **{f'excess_{y}':.01 for y in range(2020,2024)}) for p in reg['configurations'] for cost in [5,10]]

def _decision(root,reg):
    rs=_rows(reg);criteria,selected=evaluate_regions(reg,rs);out=root/paths.STAGE4_RESULTS/'summary';out.mkdir(parents=True)
    write_csv(out/'all_results.csv',rs,list(rs[0]));write_csv(out/'region_criteria.csv',criteria,list(criteria[0]))
    d=dict(registry_sha256=digest(root/paths.STAGE4_REGISTRY),selected=selected,holdout_permitted=True,
           results_csv_sha256=digest(out/'all_results.csv'),criteria_csv_sha256=digest(out/'region_criteria.csv'),
           selected_evidence=[r for r in criteria if r['configuration_id'] in selected])
    (root/paths.HOLDOUT_DECISION).write_text(json.dumps(d))
    return d

def test_grid_is_exactly_72_points_and_k_uses_half_up():
    ps=points();assert len(ps)==72 and len({p['id'] for p in ps})==72
    assert {p['N']:p['k'] for p in ps}=={8:2,10:3,12:4}
    assert sum(p['rebalance']=='R1' for p in ps)==36

def test_neighbors_are_cardinal_same_b_and_rebalance():
    reg={'configurations':points()};p=next(p for p in points() if p['L']==40 and p['N']==10 and p['b']==2 and p['rebalance']=='R1')
    ids=neighbors(p,reg);assert len(ids)==4
    assert all(next(x for x in points() if x['id']==n)['b']==2 and n.endswith('R1') for n in ids)
    corner=next(p for p in points() if p['L']==10 and p['N']==8 and p['b']==2 and p['rebalance']=='R1')
    assert len(neighbors(corner,reg))==2

def test_strict_majority_not_half_and_stress_and_calendar_conditions():
    reg={'configurations':points()};rs=_rows(reg);p=reg['configurations'][0];ns=neighbors(p,reg)
    next(r for r in rs if r['configuration_id']==ns[0] and r['cost_bps']==5)['annualized_excess']=0
    c,_=evaluate_regions(reg,rs);assert not c[0]['b_neighbor_majority']
    r=next(r for r in rs if r['configuration_id']==p['id'] and r['cost_bps']==10)
    r['annualized_excess']=0;r['excess_2020']=0;r['excess_2021']=-.1
    c,_=evaluate_regions(reg,rs);assert not c[0]['a_stress_beats_voo'] and not c[0]['c_three_years']

def test_incomplete_neighbors_not_wins_and_extended_mean_is_not_fake_return():
    reg={'configurations':points()};rs=_rows(reg);p=reg['configurations'][0];neighbor=neighbors(p,reg)[0]
    for r in rs:
        if r['configuration_id']==neighbor:r['status']='incomplete';r['annualized_excess']=None
    c,_=evaluate_regions(reg,rs)
    assert c[0]['neighborhood_mean_10bps'] is None and c[0]['neighborhood_mean_selection_value']=='-infinity'
    assert not c[0]['b_neighbor_majority']

def test_selection_is_neighborhood_mean_then_fixed_identifier():
    reg={'configurations':points()};rs=_rows(reg);cs,selected=evaluate_regions(reg,rs)
    assert selected==[min(p['id'] for p in points())]
    for r in rs:
        if r['configuration_id'].endswith('R2'):r['annualized_excess']=.2
    cs,selected=evaluate_regions(reg,rs);assert selected[0].endswith('R2')

def test_registry_is_create_once_and_hash_changes_rejected(tmp_path):
    reg=_registry(tmp_path);p=tmp_path/paths.STAGE4_REGISTRY;before=p.read_bytes()
    register(tmp_path);assert p.read_bytes()==before
    p.write_bytes(before+b' ')
    with pytest.raises(ValueError,match='hash mismatch'):verify(tmp_path)

def _authorize(root):
    (root/paths.HOLDOUT).mkdir(parents=True,exist_ok=True);(root/paths.HOLDOUT/'authorization.json').write_text('{}')

def test_holdout_refused_without_investor_authorization(tmp_path,monkeypatch):
    reg=_registry(tmp_path);_decision(tmp_path,reg)
    monkeypatch.setattr('leadtrend_v05.stage4_runner.load_inputs',lambda *a,**k:pytest.fail('future data loaded'))
    with pytest.raises(ValueError,match='investor authorization'):run_holdout(tmp_path)
    assert not (tmp_path/paths.HOLDOUT_LOCK).exists()

def test_no_decision_refuses_holdout_before_any_data_load(tmp_path,monkeypatch):
    _registry(tmp_path);_authorize(tmp_path)
    monkeypatch.setattr('leadtrend_v05.stage4_runner.load_inputs',lambda *a,**k:pytest.fail('future data loaded'))
    with pytest.raises(ValueError,match='missing decision'):run_holdout(tmp_path)
    assert not (tmp_path/paths.HOLDOUT_LOCK).exists()

def test_multiple_selected_points_refused(tmp_path):
    reg=_registry(tmp_path);_authorize(tmp_path);d=_decision(tmp_path,reg);d['selected']=[p['id'] for p in reg['configurations'][:2]]
    (tmp_path/paths.HOLDOUT_DECISION).write_text(json.dumps(d))
    with pytest.raises(ValueError,match='exactly one'):run_holdout(tmp_path)

def test_changed_registry_refuses_holdout(tmp_path):
    reg=_registry(tmp_path);_authorize(tmp_path);_decision(tmp_path,reg)
    p=tmp_path/paths.STAGE4_REGISTRY;p.write_bytes(p.read_bytes()+b' ')
    with pytest.raises(ValueError,match='hash mismatch'):run_holdout(tmp_path)

def test_false_evidence_does_not_authorize_holdout(tmp_path):
    reg=_registry(tmp_path);_authorize(tmp_path);d=_decision(tmp_path,reg);d['selected_evidence'][0]['a_stress_beats_voo']=False
    (tmp_path/paths.HOLDOUT_DECISION).write_text(json.dumps(d))
    with pytest.raises(ValueError,match='evidence does not reproduce'):run_holdout(tmp_path)

def test_positive_holdout_is_one_invocation_with_two_costs(tmp_path,monkeypatch):
    reg=_registry(tmp_path);_authorize(tmp_path);_decision(tmp_path,reg);calls=[]
    import leadtrend_v05.stage4_runner as runner
    class FakeContext:
        def __init__(self,*a,**k):pass
    def fake(ctx,point,cost,folder):
        folder.mkdir(parents=True);(folder/'summary.json').write_text('{"status":"completed"}');calls.append(cost);return 0
    monkeypatch.setattr(runner,'PreparedGrid',FakeContext);monkeypatch.setattr(runner,'run_point',fake)
    monkeypatch.setattr('leadtrend_v05.stage4_acceptance.audit',lambda *a,**k:{'status':'PASS'})
    assert run_holdout(tmp_path)==0 and calls==[5,10]
    with pytest.raises(ValueError,match='already claimed'):run_holdout(tmp_path)
    assert calls==[5,10]

def test_direct_holdout_context_without_claim_refused(tmp_path,monkeypatch):
    reg=_registry(tmp_path);_authorize(tmp_path);d=_decision(tmp_path,reg)
    from leadtrend_v05.stage4_runner import PreparedGrid
    monkeypatch.setattr('leadtrend_v05.stage4_runner.load_inputs',lambda *a,**k:pytest.fail('future data loaded'))
    with pytest.raises(ValueError,match='execution claim'):PreparedGrid(tmp_path,holdout=True,decision=d['selected'][0])

def test_R2_keeps_retained_shares_and_allocates_only_exit_cash():
    a=AccountState(100,{'KEEP':10,'EXIT':5})
    ts=plan_exit_cash_trades(a,['KEEP','NEW'],{'KEEP':100,'EXIT':100,'NEW':100},1,0,date(2024,1,2),True)
    assert a.shares_of('KEEP')==10 and a.shares_of('EXIT')==0 and a.shares_of('NEW')==5
    assert [t.security_id for t in ts]==['EXIT','NEW'] and a.cash==98

def test_R2_exit_without_addition_leaves_cash_no_rebalance():
    a=AccountState(100,{'KEEP':10,'EXIT':5})
    ts=plan_exit_cash_trades(a,['KEEP'],{'KEEP':100,'EXIT':100},1,.001,date(2024,1,2),True)
    assert len(ts)==1 and a.shares_of('KEEP')==10 and a.cash==pytest.approx(598.5)

def test_R2_no_swap_no_orders_and_initial_entry_equal():
    a=AccountState(10000)
    assert plan_exit_cash_trades(a,['A','B'],{'A':100,'B':100},1,.001,date(2024,1,2),False)==[]
    ts=plan_exit_cash_trades(a,['A','B'],{'A':100,'B':100},1,.001,date(2024,1,2),True)
    assert a.shares_of('A')==a.shares_of('B') and a.cash>=0 and len(ts)==2

def test_new_spinoff_child_cannot_be_exited_using_pre_event_signal():
    book=DevBook(AccountState(100,{'P':8}));day=date(2024,4,1)
    ev=EventRow('E','P','spinoff',day,None,None,'',None,'official',20,'child_raw_close')
    book.spinoff(ev,[dict(security_id='C',shares_per_old=.25,source='official')],{}, {'P':80,'C':80})
    assert book.children_after_signal('2024-03-28')=={'C'}
    assert book.children_after_signal('2024-04-05')==set()
    assert book.account.positions=={'P':8,'C':2}

def test_year_and_subperiod_returns_use_previous_boundary_close():
    es=[dict(session_date=d,total_equity=x) for d,x in [('2019-12-02',100),('2019-12-31',110),('2020-12-31',132)]]
    bs=[dict(session_date=d,benchmark_value=x) for d,x in [('2019-12-02',100),('2019-12-31',111),('2020-12-31',122.1)]]
    x=interval(es,bs,'2019-12-31','2020-12-31','2019-12-02',100)
    assert x['strategy_return']==pytest.approx(.2) and x['benchmark_return']==pytest.approx(.1)

def test_stopped_unpriced_spinoff_preserves_contractual_right_without_fake_value():
    from leadtrend_v05.stage4_evidence import unvalued_entitlements
    from conftest import sec
    d=date(2021,8,3);e=EventRow('E','P','spinoff',d,None,None,'',None,'official')
    rights=unvalued_entitlements(d,{'P':17},[e],{}, {'P':sec('P',ticker='BBWI')})
    assert rights[0]['contractual_child_shares']==pytest.approx(17/3)
    assert rights[0]['market_value'] is None and rights[0]['cash_in_lieu'] is None
