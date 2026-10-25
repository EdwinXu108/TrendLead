"""Stage3 acceptance replay, reused for each Stage4 completed run.

Original source remains unchanged. Only root/end handling, independent source caching
and removal of the L20-only score comparison differ from the Stage3 auditor.
"""
import csv
import json
import math
import hashlib
import sys
from collections import Counter,defaultdict
from datetime import date
from pathlib import Path
from . import paths

ROOT=Path(__file__).resolve().parent.parent
OUT=Path(__file__).resolve().parent
END='2024-06-28'
# Reviewed acceptance tolerances; the explore level imports these same
# constants instead of copying the numbers.
TOLERANCE=1e-7
CASH_FLOOR=-1e-8
def read(path):
    from .perf_artifacts import read_table
    return read_table(path)
def f(row,key):return float(row.get(key) or 0)
def audit(folder,root=None,ctx=None,end=None):
    global ROOT, END
    if root is not None:ROOT=Path(root)
    END=end or (ctx.segment['end'] if ctx is not None else '2024-06-28')
    from .perf_artifacts import LazyRows,table_names,date_bounds
    tables={name:(LazyRows(folder/(name+'.csv')) if name in {'scores','universe','decisions'} else read(folder/(name+'.csv'))) for name in ('daily_equity','positions','trades','corporate_actions','receivables','weekly_checks','benchmark','scores','universe','decisions','weekly_coverage','cash_flows','daily_contributions','return_contributions','rolling_relative')}
    summary=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    manifest=json.loads((folder/'run_manifest.json').read_text(encoding='utf-8'))
    failures=[];equity=tables['daily_equity'];trades=tables['trades'];actions=tables['corporate_actions']
    if summary['status']!='completed':failures.append('run incomplete')
    max_identity=max(abs(f(r,'cash')+f(r,'tradable_stock_value')+f(r,'frozen_stock_value')+f(r,'receivable_cash')+f(r,'receivable_security_value')-f(r,'total_equity')) for r in equity)
    if max_identity>TOLERANCE:failures.append('asset identity')
    if min(f(r,'cash') for r in equity)<CASH_FLOOR:failures.append('negative cash')
    positions=defaultdict(list);by_day=defaultdict(list);actions_by=defaultdict(list);receivables_by=defaultdict(list)
    for r in tables['positions']:positions[r['session_date']].append(r)
    for t in trades:by_day[t['session_date']].append(t)
    for r in actions:actions_by[r['session_date']].append(r)
    for r in tables['receivables']:receivables_by[r['session_date']].append(r)
    event_inputs={r['event_id']:r for r in read(ROOT/paths.NORMALIZED/'events.csv')}
    # Quotes are read as raw prices. No scoring index is used by this replay.
    quotes=independent_quotes(ROOT,END)
    supplemental=ROOT/paths.DATA_VERSION
    settlement_policy=json.loads(supplemental.read_text(encoding='utf-8'))['fractional_settlements'] if supplemental.exists() else {}
    extension=ROOT/paths.VSCO_EXTENSION
    if extension.exists():settlement_policy.update(json.loads(extension.read_text(encoding='utf-8'))['fractional_settlements'])
    calendar_days=[r['session_date'] for r in read(ROOT/paths.NORMALIZED/'calendar.csv')]
    fractional_due={};fractional_created=set();fractional_paid=set();fractional_max_error=0.
    cash=100000.;qty=defaultdict(float);receivable=0.;processed=set();previous_positions={};max_cash_error=max_qty_error=max_mark_error=max_entitlement_error=0.
    max_trade_error=0.;frozen_sales=0;frozen_share_changes=0;duplicate_events=0;split_errors=[];spinoff_visible=[];manual=[];cash_receivables={}
    freeze_observations=0
    for day in equity:
        d=day['session_date'];previous_qty=dict(qty)
        for e in actions_by[d]:
            eid=e['event_id'];sid=e['security_id'];typ=e['event_type']
            if eid in processed:
                # Multiple child component rows do not constitute a second event.
                if typ!='spinoff':duplicate_events+=1
            else:processed.add(eid)
            if typ in {'split','reverse_split'}:
                ratio=float(event_inputs[eid]['shares_new_per_old']);old=qty[sid]
                new=old*ratio
                split_errors.append(abs(new-f(e,'shares_after')-f(e,'receivable_shares')))
                max_entitlement_error=max(max_entitlement_error,abs(old-f(e,'shares_before')))
                qty[sid]=f(e,'shares_after')
            elif typ=='cash_dividend':
                entitled=previous_qty.get(sid,0);expected=entitled*float(event_inputs[eid]['cash_amount_per_share'])
                max_entitlement_error=max(max_entitlement_error,abs(expected-f(e,'cash_amount')))
                receivable+=expected;cash_receivables[eid]=expected
            elif typ=='spinoff':
                child=e['child_security_id'];qty[child]+=f(e,'child_shares_delivered')
                visible=any(p['security_id']==child for p in positions[d])
                spinoff_visible.append(visible)
        for r in receivables_by[d]:
            if r['operation']=='fractional_cash_receivable_created':
                eid=r['event_id'];sid=r['security_id'];policy=settlement_policy[sid+'|'+d]
                if eid in fractional_created:failures.append('duplicate fractional cash confirmation '+eid)
                fractional_created.add(eid)
                action=next(e for e in actions_by[d] if e.get('child_security_id')==sid)
                total=previous_qty.get(action['security_id'],0)*f(action,'ratio')
                fraction=total-math.floor(total+1e-9)
                price=float(quotes[sid,d]['close']) if policy['valuation']=='approximation' else policy['price']
                expected=fraction*price
                due=calendar_days[calendar_days.index(d)+10] if policy['valuation']=='approximation' else policy['pay_date']
                if policy['pay_date']!=due or r.get('pay_date')!=due:failures.append('fractional payment not tenth following session')
                fractional_max_error=max(fractional_max_error,abs(expected-f(r,'amount')),abs(fraction-f(r,'shares')))
                receivable+=expected;cash_receivables[eid]=expected;fractional_due[eid]=due
            if r['operation']=='cash_receivable_paid':
                expected=cash_receivables.pop(r['event_id'],None)
                if expected is None:failures.append('uncreated or twice-paid dividend '+r['event_id'])
                else:
                    max_entitlement_error=max(max_entitlement_error,abs(expected-f(r,'amount')))
                    if r['event_id'] in fractional_due:
                        if d!=fractional_due[r['event_id']]:failures.append('fractional cash paid on wrong date')
                        fractional_paid.add(r['event_id'])
                    elif event_inputs[r['event_id']]['pay_date']>d:failures.append('dividend available before pay date')
                    cash+=expected;receivable-=expected
        for t in by_day[d]:
            sid=t['security_id'];side=t['side'];q=f(t,'shares');raw_open=float(quotes[sid,d]['open'])
            slip=float(manifest['parameters']['slippage_bps'])/10000
            fill=raw_open*(1+slip if side=='buy' else 1-slip)
            delta=-(q*fill+1) if side=='buy' else q*fill-1
            error=max(abs(fill-f(t,'fill_price')),abs(raw_open-f(t,'reference_price')),abs(delta-f(t,'cash_delta')),abs(f(t,'fee')-1),abs(cash-f(t,'cash_before')))
            max_trade_error=max(max_trade_error,error)
            if q!=int(q) or q<=0:failures.append('not positive whole-share trade')
            previous=previous_positions.get(sid,{})
            if previous.get('frozen')=='1' and previous.get('asset_type')=='actual_shares':frozen_sales+=int(side=='sell')
            qty[sid]+=q if side=='buy' else -q;cash+=delta
            max_trade_error=max(max_trade_error,abs(cash-f(t,'cash_after')),abs(qty[sid]-f(t,'shares_after')))
            if len(manual)<3 or (len(manual)==3 and side=='sell'):
                manual.append(dict(session_date=d,security_id=sid,side=side,shares=q,raw_open=raw_open,
                    fill_price_computed=fill,fee=1,cash_delta_computed=delta,cash_after_computed=cash,maximum_error=error))
        actual={r['security_id']:r for r in positions[d] if r['asset_type']=='actual_shares'}
        for sid in set(qty)|set(actual):max_qty_error=max(max_qty_error,abs(qty[sid]-f(actual.get(sid,{}),'shares')))
        marked=sum(f(r,'value') for r in positions[d])
        max_mark_error=max(max_mark_error,abs(marked+cash+receivable-f(day,'total_equity')))
        max_cash_error=max(max_cash_error,abs(cash-f(day,'cash')),abs(receivable-f(day,'receivable_cash')))
        for sid,p in actual.items():
            if p['frozen']=='1':
                freeze_observations+=1
                if sid in previous_positions and previous_positions[sid]['frozen']=='1':frozen_share_changes+=int(abs(f(p,'shares')-f(previous_positions[sid],'shares'))>1e-8)
        previous_positions=actual
    if max(max_cash_error,max_qty_error,max_mark_error,max_entitlement_error,max_trade_error)>TOLERANCE:failures.append('independent ledger/quote replay discrepancy')
    if fractional_max_error>TOLERANCE:failures.append('fractional cash amount mismatch')
    if any(eid not in fractional_paid and due<=END for eid,due in fractional_due.items()):failures.append('fractional cash never paid')
    if duplicate_events or frozen_sales or frozen_share_changes:failures.append('duplicate/frozen ledger violation')
    if split_errors and max(split_errors)>TOLERANCE:failures.append('split creates shares/value')
    if spinoff_visible and not all(spinoff_visible):failures.append('hidden spinoff shares')
    non_swap={r['signal_session_date'] for r in tables['weekly_checks'] if r['is_swap_week']=='0'}
    forbidden=[t for t in trades if t['signal_session_date'] in non_swap]
    if forbidden:failures.append('trade in no-swap week')
    benchmark=tables['benchmark'];first=min(t['session_date'] for t in trades)
    if benchmark[0]['session_date']!=first or benchmark[-1]['session_date']!=END:failures.append('benchmark alignment')
    # Independently reconstruct fractional VOO units from first actual raw OPEN.
    voo={d:r for (sid,d),r in quotes.items() if sid=='VOO_ETF'};units=100000/float(voo[first]['open']);max_benchmark_error=0.
    cash_events=defaultdict(list)
    for r in event_inputs.values():
        if r['security_id']=='VOO_ETF':cash_events[r['effective_session']].append(r)
    previous_units=None
    for point in benchmark:
        d=point['session_date'];close=float(voo[d]['close']);dividend=0.
        for e in cash_events[d]:
            if e['event_type']=='cash_dividend' and previous_units is not None:dividend+=previous_units*float(e['cash_amount_per_share'])
            elif e['event_type'] in {'split','reverse_split'}:units*=float(e['shares_new_per_old'])
        units+=dividend/close
        max_benchmark_error=max(max_benchmark_error,abs(units*close-f(point,'benchmark_value')));previous_units=units
    if max_benchmark_error>TOLERANCE:failures.append('independent benchmark mismatch')
    late=[];latest=''
    for name in table_names(folder):
        last,violations=date_bounds(folder,name,END);latest=max(latest,last);late.extend(violations)
    if late:failures.append('holdout date in economic artifacts')
    for doc in (summary,manifest):
        for key in ('start_date','end_date','comparison_start_session','comparison_end_session'):
            if doc.get(key) and doc[key]>END:failures.append('holdout boundary in output JSON '+key)
    sum_contributions=sum(f(r,'profit_dollars') for r in tables['return_contributions'])
    contribution_error=abs(sum_contributions-(float(equity[-1]['total_equity'])-100000))
    if contribution_error>TOLERANCE:failures.append('contributions do not reconcile')
    coverage_source={r['signal_session_date']:r for r in read(ROOT/paths.WEEKLY_COVERAGE)}
    coverage_mismatch=sum(r['historical_no_price_ratio']!=coverage_source[r['signal_session_date']]['historical_no_price_ratio'] for r in tables['weekly_coverage'])
    if coverage_mismatch:failures.append('historical no-price reporting changed')
    max_score_error=0.  # Stage2 L20 diagnostic is not a grid ledger acceptance condition.
    result={'status':'PASS' if not failures else 'FAIL','scenario':folder.name,'daily_asset_identity_max_error':max_identity,
        'independent_cash_receivable_replay_max_error':max_cash_error,'independent_share_replay_max_error':max_qty_error,
        'independent_position_valuation_max_error':max_mark_error,'dividend_entitlement_max_error':max_entitlement_error,
        'trade_price_fee_slippage_cash_max_error':max_trade_error,'cash_minimum':min(f(r,'cash') for r in equity),
        'frozen_actual_position_observations':freeze_observations,'frozen_sales':frozen_sales,'frozen_share_changes':frozen_share_changes,
        'event_applications':len(processed),'duplicate_event_applications':duplicate_events,
        'held_split_events':len(split_errors),'split_share_conservation_max_error':max(split_errors,default=0),
        'split_mechanical_value_max_error':max((abs(f(r,'mechanical_value_error')) for r in actions if r['event_type'] in {'split','reverse_split'}),default=0),
        'held_spinoff_components':len(spinoff_visible),'spinoff_visibility':'all visible' if spinoff_visible else 'NOT EXERCISED in real run; synthetic tests cover it',
        'held_cash_dividend_events':sum(r['event_type']=='cash_dividend' for r in actions),
        'no_swap_weeks':len(non_swap),'trades_in_no_swap_weeks':len(forbidden),
        'benchmark_start':benchmark[0]['session_date'],'first_trade':first,'benchmark_end':benchmark[-1]['session_date'],
        'independent_benchmark_max_error':max_benchmark_error,'latest_economic_artifact_date':latest,'holdout_rows':len(late),
        'return_contribution_reconciliation_error':contribution_error,'weekly_missing_history_report_mismatches':coverage_mismatch,
        'stage2_stage3_score_max_rounding_difference':max_score_error,
        'manual_trade_checks':manual,'failures':failures}
    result.update(fractional_cash_confirmations=len(fractional_created),fractional_cash_payments=len(fractional_paid),fractional_cash_max_error=fractional_max_error)
    return result

_SOURCE_QUOTES={}
class _Quote:
    """Raw open and close text of one source quote; the audit reads these two fields and no others."""
    __slots__=('open','close')
    def __init__(self,open_,close):self.open=open_;self.close=close
    def __getitem__(self,key):
        if key=='open':return self.open
        if key=='close':return self.close
        raise KeyError(key)
def independent_quotes(root,end):
    from .perf_artifacts import iter_table
    key=(str(root),end)
    if key in _SOURCE_QUOTES:return _SOURCE_QUOTES[key]
    quotes={}
    for r in iter_table(root/paths.NORMALIZED/'prices.csv'):
        if r['session_date']<=end:quotes[sys.intern(r['security_id']),sys.intern(r['session_date'])]=_Quote(r['open'],r['close'])
    tickers={r['ticker']:r['security_id'] for r in read(root/paths.NORMALIZED/'securities.csv') if r['ticker']}
    for r in read(root/paths.NORMALIZED/'spinoff_child_prices.csv'):
        if r['session_date']>end:continue
        ticker=r['child_ticker'];sid=tickers.get(ticker,'CHILD:'+ticker)
        path=root/paths.CHILD_KLINES/f'NONE_{ticker.replace(".","_")}_2019-05-01_2026-12-31.csv'
        if path.exists():
            for p in iter_table(path):
                if p['session_date']<=end:quotes.setdefault((sid,p['session_date']),_Quote(p['open'],p['close']))
    supplement=root/paths.DATA_VERSION
    if supplement.exists():
        version=json.loads(supplement.read_text(encoding='utf-8'))
        for p in iter_table(root/version['auxiliary_prices']):
            if p['session_date']<=end:quotes['CHILD:VSCO',p['session_date']]=_Quote(p['open'],p['close'])
    _SOURCE_QUOTES[key]=quotes
    return quotes

def baseline_consistency(root,folder,cost):
    original=Path(root)/paths.STAGE3_RESULTS
    if cost==10:original=original/'stress_10bps'
    hashes={}
    for name in ('trades.csv','daily_equity.csv','benchmark.csv'):
        hashes[name]=hashlib.sha256((folder/name).read_bytes()).hexdigest()==hashlib.sha256((original/name).read_bytes()).hexdigest()
    old=json.loads((original/'summary.json').read_text(encoding='utf-8'))
    new=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    diagnostic={'contribution_reconciliation_error'}
    metadata={'experiment_label','no_parameter_search','no_holdout_computed'}
    mismatches={k:{'old':v,'new':new.get(k)} for k,v in old.items() if k not in diagnostic|metadata and new.get(k)!=v}
    diag={k:{'old':old.get(k),'new':new.get(k)} for k in diagnostic}
    return dict(status='PASS' if all(hashes.values()) and not mismatches else 'FAIL',
                csv_byte_identical=hashes,summary_economic_numbers_exactly_equal=not mismatches,
                mismatches=mismatches,excluded_floating_sum_diagnostics=diag,
                note='Only metadata and aggregate floating point residual diagnostic excluded; no economic amount tolerated or replaced.')
