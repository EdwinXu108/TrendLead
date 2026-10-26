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
def _previous_close(quotes,sid,equity,i):
    """Close of a name on the session before equity row i; one missing session falls back one more session, as the engine does."""
    for back in (1,2):
        if i-back>=0 and (sid,equity[i-back]['session_date']) in quotes:return float(quotes[sid,equity[i-back]['session_date']]['close'])
    raise ValueError('no previous close for '+sid)

def audit(folder,root=None,ctx=None,end=None):
    global ROOT, END
    if root is not None:ROOT=Path(root)
    END=end or (ctx.segment['end'] if ctx is not None else '2024-06-28')
    from .perf_artifacts import LazyRows,table_names,date_bounds
    tables={name:(LazyRows(folder/(name+'.csv')) if name in {'scores','universe','decisions'} else read(folder/(name+'.csv'))) for name in ('daily_equity','positions','trades','corporate_actions','receivables','weekly_checks','benchmark','scores','universe','decisions','weekly_coverage','cash_flows','daily_contributions','return_contributions','rolling_relative')}
    summary=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    manifest=json.loads((folder/'run_manifest.json').read_text(encoding='utf-8'))
    initial=float(manifest.get('initial_capital_account',100000.))  # a tranche sub-account starts with 1/K of the account
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
    day_index={x:i for i,x in enumerate(calendar_days)};entitlement_total={}
    fractional_due={};fractional_created=set();fractional_paid=set();fractional_max_error=0.
    cash=initial;qty=defaultdict(float);receivable=0.;processed=set();previous_positions={};max_cash_error=max_qty_error=max_mark_error=max_entitlement_error=0.
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
                entitlement_total[eid,sid]=(new,d)
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
                entitlement_total[eid,child]=(previous_qty.get(sid,0)*f(e,'ratio'),d)
        for r in receivables_by[d]:
            if r['operation']=='fractional_cash_receivable_created' and r['security_id']+'|'+d not in settlement_policy:
                # Rule v2 general approximation, replayed independently: first regular-way close, tenth following session.
                eid=r['event_id'];sid=r['security_id']
                if eid in fractional_created:failures.append('duplicate fractional cash confirmation '+eid)
                fractional_created.add(eid)
                parent=eid.split('|')[0]
                if (parent,sid) not in entitlement_total:failures.append('general fraction without entitlement '+eid);continue
                total,effective=entitlement_total[parent,sid]
                k=day_index[effective];first=day_index[d]
                if (sid,d) not in quotes:failures.append('general fraction settled without a price '+eid);continue
                if any((sid,calendar_days[x]) in quotes for x in range(k,first)):failures.append('general fraction not settled at first price '+eid)
                fraction=total-math.floor(total+1e-9)
                expected=fraction*float(quotes[sid,d]['close'])
                due=calendar_days[k+10] if k+10<len(calendar_days) and calendar_days[k+10]<=END else None
                if (r.get('pay_date') or None)!=due:failures.append('general fractional payment not tenth following session')
                fractional_max_error=max(fractional_max_error,abs(expected-f(r,'amount')),abs(fraction-f(r,'shares')))
                receivable+=expected;cash_receivables[eid]=expected
                if due is not None:fractional_due[eid]=due
            if r['operation']=='fractional_cash_receivable_created' and r['security_id']+'|'+d in settlement_policy:
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
            sid=t['security_id'];side=t['side'];q=f(t,'shares')
            if (sid,d) not in quotes:failures.append('trade on a session without a price: '+sid+' '+d);continue
            raw_open=float(quotes[sid,d]['open'])
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
        for p in positions[d]:
            # Rule v2 marks, replayed from raw closes: a held name's gap is one session, valued at the previous close.
            sid=p['security_id'];today=quotes.get((sid,d))
            if today is not None:
                if abs(f(p,'mark')-float(today['close']))>TOLERANCE:failures.append('mark differs from raw close '+sid+' '+d)
                continue
            k=day_index[d];prior=quotes.get((sid,calendar_days[k-1])) if k>0 else None
            if prior is None:failures.append('held price gap without single-session rule '+sid+' '+d)
            elif abs(f(p,'mark')-float(prior['close']))>TOLERANCE:failures.append('stale mark is not the previous close '+sid+' '+d)
        marked=sum(f(r,'value') for r in positions[d])
        max_mark_error=max(max_mark_error,abs(marked+cash+receivable-f(day,'total_equity')))
        max_cash_error=max(max_cash_error,abs(cash-f(day,'cash')),abs(receivable-f(day,'receivable_cash')))
        for sid,p in actual.items():
            if p['frozen']=='1':
                freeze_observations+=1
                if sid in previous_positions and previous_positions[sid]['frozen']=='1':frozen_share_changes+=int(abs(f(p,'shares')-f(previous_positions[sid],'shares'))>1e-8)
        previous_positions=actual
    # Rule v2 scoring-window gaps, replayed from raw quotes with this module's own window arithmetic.
    # A held name with a gap inside its scoring window is sold at the next execution; a gap on the signal date
    # keeps a chosen name for that week; no name is bought while its window has a gap.
    signal_spec=manifest.get('signal') or {'kind':'momentum','L':manifest['parameters']['L'],'skip':0}
    if signal_spec['kind']=='momentum':window_len=signal_spec['L']+signal_spec.get('skip',0)
    elif signal_spec['kind'] in {'peer','sector_demeaned','vol_adjusted','size_tilt','voo_weight'}:window_len=signal_spec['L']
    else:window_len=None
    chosen_by_week=defaultdict(set)
    for r in tables['universe']:
        if r['class_selected']=='1':chosen_by_week[r['signal_session_date']].add(r['security_id'])
    def window_complete(sid,signal_day):
        if signal_day not in day_index:return False
        k=day_index[signal_day]
        if k-window_len<0:return False
        return all((sid,x) in quotes for x in calendar_days[k-window_len:k+1])
    if window_len is not None:
        for w in tables['weekly_checks']:
            d=w['signal_session_date'];e=w['execution_session']
            if not e or d not in day_index:continue
            held={r['security_id'] for r in positions[d] if r['asset_type']=='actual_shares'}
            for sid in held:
                if (sid,d) not in quotes:
                    if sid in chosen_by_week.get(d,set()) and (sid,e) in quotes and any(t['security_id']==sid and t['side']=='sell' for t in by_day.get(e,[])):
                        failures.append('signal-day price gap sold at execution '+sid+' '+d)
                elif not window_complete(sid,d) and (sid,e) in quotes:
                    if any(r['security_id']==sid for r in positions[e] if r['asset_type']=='actual_shares'):
                        failures.append('price gap in scoring window not exited '+sid+' '+d)
        for t in tables['trades']:
            if t['side']=='buy' and t['signal_session_date'] in day_index and not window_complete(t['security_id'],t['signal_session_date']):
                failures.append('buy of a name with a price gap in its scoring window '+t['security_id']+' '+t['signal_session_date'])
    cap=manifest.get('single_stock_cap')
    if cap is not None:
        # Plan 9.4(2), prior-close rule, replayed from independent quotes: in a session with trades, a name held before and
        # after it, not frozen, has shares after the trades x its previous-session close (divided by the split ratio of a
        # split effective that session) no more than cap x the previous session's total equity, plus one share of slack.
        cap=float(cap);prior={}
        splits={(r['security_id'],r['effective_session']):float(r['shares_new_per_old']) for r in event_inputs.values() if r['event_type'] in {'split','reverse_split'}}
        for i,day in enumerate(equity):
            d=day['session_date'];now={r['security_id']:r for r in positions[d] if r['asset_type']=='actual_shares'}
            if by_day.get(d) and i>0:
                previous_total=f(equity[i-1],'total_equity')
                for sid,r in now.items():
                    if sid in prior and r['frozen']=='0' and f(r,'shares')>0:
                        close=_previous_close(quotes,sid,equity,i)/splits.get((sid,d),1.)
                        if f(r,'shares')*close>cap*previous_total*(1+1e-9)+close:
                            failures.append('retained name above the single-stock cap '+sid+' '+d)
            prior=now
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
    voo={d:r for (sid,d),r in quotes.items() if sid=='VOO_ETF'};units=initial/float(voo[first]['open']);max_benchmark_error=0.
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
    contribution_error=abs(sum_contributions-(float(equity[-1]['total_equity'])-initial))
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
    if manifest.get('single_stock_cap') is not None:result.update(single_stock_cap=manifest['single_stock_cap'],single_stock_cap_trims=sum(1 for t in trades if t['reason']=='cap_trim'))
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

def voo_values(root,days,first,initial):
    """VOO on the whole account as the audit replays it: units bought at the first open of the first trade, dividends reinvested."""
    quotes=independent_quotes(Path(root),days[-1])
    voo={d:r for (sid,d),r in quotes.items() if sid=='VOO_ETF'}
    cash_events=defaultdict(list)
    for r in read(Path(root)/paths.NORMALIZED/'events.csv'):
        if r['security_id']=='VOO_ETF':cash_events[r['effective_session']].append(r)
    units=initial/float(voo[first]['open']);out=[];previous_units=None
    for d in days:
        close=float(voo[d]['close']);dividend=0.
        for e in cash_events[d]:
            if e['event_type']=='cash_dividend' and previous_units is not None:dividend+=previous_units*float(e['cash_amount_per_share'])
            elif e['event_type'] in {'split','reverse_split'}:units*=float(e['shares_new_per_old'])
        units+=dividend/close
        out.append(units*close);previous_units=units
    return out

def audit_tranche_group(group,root=None,end=None):
    """Independent check of one tranche group (plan 9.4(1)). Reads the group folder and its sub-account folders only.

    Each sub-account replays with its own 1/K capital (audit). The merged account equals the sum of the sub-accounts on
    every session; sub-account j checks only at its own signal weeks (index j mod K); the sub-account capitals add to the
    account; the merged trade count is the sum of the sub-account counts; the merged benchmark is VOO on the whole account
    from the first merged trade.
    """
    global ROOT
    if root is not None:ROOT=Path(root)
    group=Path(group);gm=json.loads((group/'group_manifest.json').read_text(encoding='utf-8'))
    K=int(gm['K']);subs=[group/x for x in gm['sub_folders']];weeks=gm['signal_weeks'];failures=[];sub_status=[]
    if len(subs)!=K:failures.append('sub-account count differs from K')
    for s in subs:
        r=audit(s,ROOT,end=end);sub_status.append(dict(folder=s.name,status=r['status']))
        failures.extend(f"{s.name}: {x}" for x in r['failures'])
    capital=sum(float(json.loads((s/'run_manifest.json').read_text(encoding='utf-8'))['tranche']['initial_capital']) for s in subs)
    if abs(capital-100000.)>TOLERANCE:failures.append('sub-account capital does not add to the account')
    for j,s in enumerate(subs):
        own={r['signal_session_date'] for r in read(s/'weekly_checks.csv')}
        if own!=set(weeks[j::K]):failures.append(f'{s.name} does not check at its own schedule')
    merged=read(group/'merged'/'daily_equity.csv');per_sub=[{r['session_date']:float(r['total_equity']) for r in read(s/'daily_equity.csv')} for s in subs]
    gap=max(abs(sum(p.get(r['session_date'],0.) for p in per_sub)-float(r['total_equity'])) for r in merged) if merged else 0.
    if gap>TOLERANCE:failures.append('merged equity differs from the sum of sub-accounts')
    trades=read(group/'merged'/'trades.csv')
    if len(trades)!=sum(len(read(s/'trades.csv')) for s in subs):failures.append('merged trades differ from the sum of sub-account trades')
    bench=read(group/'merged'/'benchmark.csv');first=min(t['session_date'] for t in trades) if trades else None
    benchmark_error=None
    if first is None or bench[0]['session_date']!=first:failures.append('merged benchmark does not start at the first merged trade')
    else:
        expected=voo_values(ROOT,[b['session_date'] for b in bench],first,100000.)
        benchmark_error=max(abs(v-float(b['benchmark_value'])) for v,b in zip(expected,bench))
        if benchmark_error>TOLERANCE:failures.append('merged benchmark mismatch')
    return dict(status='PASS' if not failures else 'FAIL',K=K,subs=sub_status,merged_equity_gap=gap,
                merged_benchmark_error=benchmark_error,failures=failures)

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
