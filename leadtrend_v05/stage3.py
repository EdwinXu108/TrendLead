"""Development-only real-data run. No holdout calculation and no trading SDK."""
from __future__ import annotations
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
from . import paths
from .config import load_config
from .dataio import load_inputs, SecurityRow, PriceRow, sha256_file
from .calendar import Calendar
from .eligibility import ClassSelector, build_price_index, eligibility_index
from .selection import Candidate, SELECTION_RULES, select_portfolio
from .selection_matched import MATCHED_RULES, load_match_table
from .selection_structured import percentile_by_rank
from .sectors import load_sector_map, sector_key
from .signals import is_default_momentum, normalize as normalize_signal, signal_key
from .execution import plan_equal_weight_trades, plan_exit_cash_trades
from .ledger import AccountState
from .benchmark import build_benchmark
from .universe import choose_snapshot
from .stage3_scoring import ScoreEngine
from .stage3_ledger import DevBook
from .stage3_metrics import summarize
from .stage2_enrich import read_csv, write_csv

DEV_MAX=date(2024,6,28)
EXPLORE_TABLES=('daily_equity','benchmark','trades','weekly_checks')
# v2: single-day held price gaps use the previous close; unverified fractional entitlements become cash
# receivables valued at the first regular-way close and paid on the tenth following session.
ACCOUNTING_RULES_VERSION='v2'

def guard_development(config, registry, end_override=None):
    start=date.fromisoformat(config['start_date']);end=date.fromisoformat(end_override or config['end_date'])
    registered=date.fromisoformat(registry['development']['end'])
    if end>registered or end>DEV_MAX or end>=date.fromisoformat(registry['holdout']['start']):
        raise ValueError('holdout protection: end date exceeds registered development segment')
    if start<date.fromisoformat(registry['development']['start']) or start>end:
        raise ValueError('development start/end outside registered range')
    for key,value in {'L':20,'N':10,'k':3,'b':2,'initial_capital':100000,'fee_per_trade':1}.items():
        if config.get(key)!=value:raise ValueError('unregistered default parameter: '+key)
    if config.get('slippage_bps') not in {5,10}:raise ValueError('only registered base/stress costs allowed')
    return start,end

class _StaleMarks:
    """One session's marks where some held names carry the previous close because they had a single-session gap."""
    def __init__(self, base, stale):
        self.base=base;self.stale=stale
    def __getitem__(self, sid):
        return self.stale[sid] if sid in self.stale else self.base[sid]
    def __contains__(self, sid):
        return sid in self.stale or sid in self.base

def write_artifacts(out, tables, summary, manifest, failure=None, *, artifact_mode='full',shared=None):
    out.mkdir(parents=True,exist_ok=True)
    fields={'universe':['signal_session_date','security_id','snapshot_id','eligible','class_selected'],
        'scores':['signal_session_date','security_id','score','unscorable_reason'],
        'decisions':['signal_session_date','security_id','action','reason'],
        'trades':['session_date','security_id','side','shares','reference_price','fill_price','fee','cash_delta'],
        'daily_equity':['session_date','cash','total_equity'],'benchmark':['session_date','benchmark_value'],
        'positions':['session_date','security_id','shares','mark','value','frozen','reason'],
        'receivables':['session_date','event_id','security_id','operation','shares','amount','source'],
        'cash_flows':['session_date','event_id','security_id','cash_delta','kind'],
        'corporate_actions':['session_date','event_id','security_id','event_type','handling','source'],
        'weekly_checks':['signal_session_date','is_swap_week','frozen_count','target_count'],
        'data_issues':['session_date','security_id','severity','message'],
        'weekly_coverage':['signal_session_date','n_eligible','historical_no_price_ratio'],
        'daily_contributions':['session_date','security_id','profit_dollars'],
        'rolling_relative':['session_date','rolling_52_week_relative_return'],
        'return_contributions':['security_id','profit_dollars','return_contribution_percentage_points']}
    if artifact_mode=='lean':
        from .perf_artifacts import write_lean
        return write_lean(out,tables,summary,manifest,fields,shared,failure)
    if artifact_mode=='explore':
        # Exploration screening: same economics, only the small screening subset,
        # written with the lean writer so physical bytes match daily artifacts.
        from .perf_artifacts import write_lean
        return write_lean(out,{n:tables[n] for n in EXPLORE_TABLES},summary,manifest,fields,{},failure,mode='explore')
    for name,rs in tables.items():
        columns=list(dict.fromkeys(k for r in rs for k in r)) if rs else fields.get(name,['empty'])
        write_csv(out/(name+'.csv'),rs,columns)
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (out/'run_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if failure is not None:(out/'last_explainable_state.json').write_text(json.dumps(failure,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def load_children(root, inputs, end):
    """Auxiliary prices stay outside universe. No network calls from run-dev."""
    securities={r.security_id:r for r in inputs.securities};ticker_ids={r.ticker:r.security_id for r in inputs.securities if r.ticker}
    prices={(p.security_id,p.session_date):p for p in inputs.prices if p.session_date<=end}
    children=defaultdict(list);raw_hashes={}
    for child in read_csv(root/paths.NORMALIZED/'spinoff_child_prices.csv'):
        if date.fromisoformat(child['session_date'])>end:continue
        ticker=child['child_ticker'];sid=ticker_ids.get(ticker,'CHILD:'+ticker)
        row=dict(child);row['security_id']=sid;children[row['event_id']].append(row)
        if sid not in securities:
            securities[sid]=SecurityRow(sid,sid,ticker,None,None,'common_stock','US_LISTED','UNKNOWN:'+sid,'not independently classified child; explicit unknown','USD')
        path=root/paths.CHILD_KLINES/f'NONE_{ticker.replace(".","_")}_2019-05-01_2026-12-31.csv'
        if path.exists():
            raw_hashes[str(path.relative_to(root))]=sha256_file(path)
            for r in read_csv(path):
                d=date.fromisoformat(r['session_date'])
                if d>end:continue
                prices.setdefault((sid,d),PriceRow(sid,d,float(r['open']),float(r['close']),float(r['volume']) if r['volume'] else None,float(r['turnover']) if r['turnover'] else None,'USD'))
    return securities,prices,children,raw_hashes

def run_dev(root:Path, config_path:Path, output:Path, *, end_override=None, stress=False, _stage4_context=None,_performance=None):
    root=Path(root);raw=json.loads(Path(config_path).read_text(encoding='utf-8'))
    registry_path=root/paths.STAGE2_REGISTRY;registry=json.loads(registry_path.read_text(encoding='utf-8'))
    if stress:raw['slippage_bps']=10
    if _stage4_context is None:
        start,end=guard_development(raw,registry,end_override)
    else:
        from .stage4_runner import PreparedGrid
        if not isinstance(_stage4_context,PreparedGrid):raise ValueError('invalid Stage4 authorization context')
        start,end=_stage4_context.authorize(raw,end_override)
    # All checks precede output creation, input loading or score construction.
    acceptance=json.loads((root/paths.STAGE2_ACCEPTANCE).read_text(encoding='utf-8'))
    if acceptance['status']!='PASS':raise ValueError('A-part data acceptance has not passed')
    if registry['status']!='registered_before_viewing_any_real_returns':raise ValueError('experiment not preregistered')
    if _performance is None or not _performance.verified_normalized:
        for name,digest in registry['data_version'].items():
            if sha256_file(root/paths.NORMALIZED/name)!=digest:raise ValueError('registered normalized hash mismatch: '+name)
    cfg=load_config(config_path)
    if stress:cfg=replace(cfg,slippage_bps=10.)
    if _performance is not None:full=replace(_performance.full,config=cfg)
    else:full=load_inputs(cfg,root,score_only=True) if _stage4_context is None else replace(_stage4_context.full,config=cfg)
    blocking=[r for r in full.data_issues if r['severity'] in {'fatal','error'}]
    if blocking:raise ValueError('invalid normalized inputs: '+str(blocking[:3]))
    # Physical slicing prevents any downstream method from seeing holdout prices.
    if _performance is not None:inputs=full
    else:
        inputs=replace(full,calendar=[c for c in full.calendar if c.session_date<=end],
            prices=[p for p in full.prices if p.session_date<=end],events=[e for e in full.events if e.effective_session<=end],
            snapshots=[s for s in full.snapshots if s.available_at.date()<=end and s.holdings_as_of<=end])
    del full
    cal=Calendar(inputs.calendar);dates=[c.session_date for c in cal.sessions];sessions=[c for c in cal.sessions if start<=c.session_date<=end]
    positions={d:i for i,d in enumerate(dates)}
    # Tranche sub-account j (plan 9.4(1)) checks at the signal weeks with index j, j+K, ... and holds 1/K of the account.
    tranche=raw.get('tranche');K=tranche['K'] if tranche else 1;j=tranche['j'] if tranche else 0
    signal_days=sorted(c.session_date for c in cal.week_signal_sessions() if start<=c.session_date<=end)
    if K>1:signal_days=[d for i,d in enumerate(signal_days) if i%K==j]
    signals=set(signal_days);init_capital=cfg.initial_capital/K if K>1 else cfg.initial_capital
    def paid_on_tenth(created):
        """Pay date of a general fractional settlement: the tenth session after the effective session, inside the segment."""
        i=positions[created]+10
        return dates[i] if i<len(dates) and dates[i]<=end else None
    if _performance is not None:
        sec,px,children,raw_hashes,marks_by_date,engine=_performance.prepared
    elif _stage4_context is None:
        sec,px,children,raw_hashes=load_children(root,inputs,end)
        marks_by_date=defaultdict(dict)
        for (sid,day),p in px.items():marks_by_date[day][sid]=p.close
        engine=ScoreEngine(inputs,dates,root,end)
    else:
        sec,px,children,raw_hashes,marks_by_date,engine=_stage4_context.prepared
    if _performance is None:
        elig=eligibility_index(inputs);selector=ClassSelector(cfg,build_price_index(inputs.prices))
    events_by_date=defaultdict(list)
    for e in inputs.events:events_by_date[e.effective_session].append(e)
    book=DevBook(AccountState(init_capital));tables={name:[] for name in ('universe','scores','decisions','trades','daily_equity','benchmark','positions','receivables','cash_flows','corporate_actions','weekly_checks','data_issues','weekly_coverage','daily_contributions','rolling_relative','return_contributions')}
    if _stage4_context is not None:book.fractional_settlements=getattr(_stage4_context,'fractional_settlements',{})
    coverage={r['signal_session_date']:r for r in read_csv(root/paths.WEEKLY_COVERAGE)}
    # Sector keys are needed only by the structured selection rules; the reviewed default path does not read the map.
    sector_by_sid=None;selection_counts=Counter();gap_exit_count=0
    if raw.get('selection','default')!='default':
        sector_map=load_sector_map(root);sector_by_sid={sid:sector_key(r.industry,sector_map)[0] for sid,r in sec.items()}
    pending=None;first_trade=None;first_open_value=None;contributions=defaultdict(float);previous_assets={};last_marks={};stop=None
    # Strategy signal: absent means the existing momentum rule on cfg.L, so grid outputs are unchanged.
    signal=normalize_signal(raw['signal']) if raw.get('signal') is not None else {'kind':'momentum','L':cfg.L,'skip':0}
    if raw.get('signal') is not None and _performance is None:raise ValueError('signals need the cached score tables')
    table_key=None
    if _performance is not None:
        table_key=signal['L'] if is_default_momentum(signal) and signal['L'] in _performance.shared else signal_key(signal)
        if table_key not in _performance.shared:raise ValueError('score table not built for signal '+str(table_key))
    manifest={'stage':3,'scope':'development_only','start_date':start.isoformat(),'end_date':end.isoformat(),
        'registered_development':registry['development'],'registry_sha256':sha256_file(registry_path),
        'normalized_file_hashes':registry['data_version'],'auxiliary_raw_hashes':raw_hashes,
        'parameters':{k:raw[k] for k in ('L','N','k','b','initial_capital','fee_per_trade','slippage_bps')},
        'engineering_defaults_unvalidated':True,'no_holdout_computed':True,'accounting_rules_version':ACCOUNTING_RULES_VERSION,
        'execution_assumption':'next-session raw open is observable allocation quote; whole shares, sell before buy; only swap weeks; no extra settlement delay',
        'fractional_right_assumption':'unverified fractional cash-in-lieu stays a restricted security entitlement at observed child mark; no invented payment',
        'limitations':['survivorship bias from missing historical member quotes','current industry classification','unvalidated engineering defaults']}
    if tranche:manifest.update(tranche=dict(K=K,j=j,initial_capital=init_capital,schedule_weeks=len(signal_days)),initial_capital_account=init_capital)
    if raw.get('cap') is not None:manifest.update(single_stock_cap=raw['cap'])
    if raw.get('signal') is not None or raw.get('experiment') is not None:
        manifest.update(signal=signal,signal_key=signal_key(signal),experiment=raw.get('experiment'))
    if _stage4_context is not None:
        manifest.update(stage=4,scope=_stage4_context.scope,stage4_registry_sha256=_stage4_context.registry_hash,
                        rebalance=raw['rebalance'],no_holdout_computed=_stage4_context.scope=='development_only')
    # Entry rules read the short-window percentiles of every signal week up front: one pass keeps one window table loaded.
    # Matched random controls (plan 9.3(1)) read their match table once: the pool groups and the reference's buys.
    match_table=load_match_table(raw['selection_params']['match_file']) if raw.get('selection','default') in MATCHED_RULES else {}
    short_pct={}
    if raw.get('selection','default') in {'ent_pos','ent_neg'}:
        if _performance is None:raise ValueError('entry rules need the cached score tables')
        short_window=raw.get('selection_params',{}).get('short_window',20)
        for d in sorted(signals):
            short_pct[d]=percentile_by_rank(sorted(_performance.frames[d]['windows'][short_window]['candidates'],key=lambda c:c.sort_key))
    for session in sessions:
        d=session.session_date;iso=d.isoformat();cash_start=book.account.cash;payment=0.;trade_flow=0.;structural=defaultdict(float)
        entitled=dict(book.account.positions)
        try:
            required=set(book.account.positions)|{r.security_id for r in book.security_receivables}
            same_day_ids={e.security_id for e in events_by_date[d]}|{c['security_id'] for e in events_by_date[d] for c in children.get(e.event_id,[])}
            prior=dates[positions[d]-1] if positions[d]>0 else None
            stale={}
            for sid in sorted(required):
                if (sid,d) in px:continue
                # Rule v2: one missing session with no corporate action values the name at the previous close; two stop.
                if sid in same_day_ids or prior is None or (sid,prior) not in px:
                    raise ValueError('held asset price interrupted without verified disposition: '+sid+' '+iso)
                stale[sid]=px[sid,prior].close
                tables['data_issues'].append(dict(session_date=iso,security_id=sid,severity='stale_mark',message='single-session price gap valued at previous close'))
            marks=_StaleMarks(marks_by_date[d],stale) if stale else marks_by_date[d]
            # Corporate actions precede open trades, entitlement uses previous close.
            for e in sorted(events_by_date[d],key=lambda e:(e.event_type not in {'split','reverse_split'},e.event_id)):
                q=entitled.get(e.security_id,0)
                if not q:continue
                if e.event_type in {'split','reverse_split'}:book.split(e,marks[e.security_id])
                elif e.event_type=='cash_dividend':book.dividend(e,q)
                elif e.event_type=='spinoff':
                    before=len(book.event_rows);book.spinoff(e,children.get(e.event_id,[]),sec,marks,entitled_shares=q)
                    for row in book.event_rows[before:]:
                        child=row['child_security_id'];value=(row['child_shares_delivered']+row['receivable_shares'])*marks[child]
                        structural[e.security_id]+=value;structural[child]-=value
                else:raise ValueError('held unsupported event '+e.event_id)
            # Rule v2: an unverified fractional entitlement becomes cash as soon as its first regular-way close exists.
            book.settle_fractions(d,marks_by_date[d],lambda created:paid_on_tenth(created))
            flows_before=len(book.cash_flows);payment=book.pay(d)
            payment_by=book.payments_on(d,flows_before)
            if pending is not None:
                target,signal_day,swap,gap_retained=pending
                # Newly created rights cannot be silently sold or ignored by an
                # older pending target. Unknown cash-in-lieu stops before orders.
                book.require_resolved_partial_delivery()
                frozen=book.frozen_ids()
                protected_children=book.children_after_signal(signal_day)
                tradable={sid:q for sid,q in book.account.positions.items() if sid not in frozen|protected_children|gap_retained}
                # Rule v2: no price today voids that name's order (a held name keeps its shares, a buy is dropped, nothing is postponed).
                voided=sorted({sid for sid in set(tradable)|set(target) if (sid,d) not in px})
                for sid in voided:tables['data_issues'].append(dict(session_date=iso,security_id=sid,severity='void_order',message='no price on execution session; order voided, not postponed'))
                voided_held={sid for sid in voided if sid in tradable}
                tradable={sid:q for sid,q in tradable.items() if sid not in voided_held}
                target=[sid for sid in target if sid not in voided]
                temp=AccountState(book.account.cash,dict(tradable))
                opens={sid:px[sid,d].open for sid in set(tradable)|set(target)}
                if first_trade is None:first_open_value=init_capital
                planner=plan_equal_weight_trades
                if _stage4_context is not None and raw['rebalance']=='R2':
                    from .execution import plan_exit_cash_trades
                    planner=plan_exit_cash_trades
                cap=raw.get('cap')
                if cap is not None and planner is not plan_exit_cash_trades:raise ValueError('single-stock cap is a round 2 option')
                extra={} if cap is None else dict(cap=cap,marks=marks,total_pre=book.components(marks)['total_equity'])
                trades=planner(temp,target,opens,cfg.fee_per_trade,cfg.slippage_rate,d,swap,
                    allocation_slots=max(0,cfg.N-len(frozen)-len(gap_retained)-len(voided_held)),**extra)
                running_cash=book.account.cash;qs=dict(tradable)
                for t in trades:
                    old=qs.get(t.security_id,0);new=old+(t.shares if t.side=='buy' else -t.shares)
                    row=asdict(t);row['session_date']=iso;row.update(signal_session_date=signal_day,
                        cash_before=running_cash,cash_after=running_cash+t.cash_delta,shares_before=old,shares_after=new,
                        is_swap_week=int(swap),slippage_bps=cfg.slippage_bps)
                    tables['trades'].append(row);running_cash+=t.cash_delta;qs[t.security_id]=new;trade_flow+=t.cash_delta
                    structural[t.security_id]+=t.cash_delta
                    if first_trade is None:first_trade=d
                book.account.cash=temp.cash
                for sid in tradable:book.account.positions.pop(sid,None)
                book.account.positions.update(temp.positions)
                pending=None
            comp=book.components(marks);total=comp['total_equity']
            if book.account.cash < -1e-7:raise ValueError('negative cash')
            assets=defaultdict(float)
            for sid,q in book.account.positions.items():assets[sid]+=q*marks[sid]
            for r in book.security_receivables:assets[r.security_id]+=r.shares*marks[r.security_id]
            for r in book.account.receivables:assets[r.security_id]+=r.amount
            for sid in set(assets)|set(previous_assets)|set(structural)|set(payment_by):
                pnl=assets[sid]-previous_assets.get(sid,0)+structural[sid]+payment_by[sid]
                contributions[sid]+=pnl
                if abs(pnl)>1e-10:tables['daily_contributions'].append(dict(session_date=iso,security_id=sid,profit_dollars=pnl))
            previous_assets=dict(assets)
            tables['daily_equity'].append(dict(session_date=iso,stock_value=comp['tradable_stock_value']+comp['frozen_stock_value'],
                **comp,n_positions=len(book.account.positions),frozen_count=len(book.frozen_ids()),
                n_security_slots=len(set(book.account.positions)|book.frozen_ids()),
                cash_start=cash_start,dividend_payments=payment,trade_cash_delta=trade_flow,
                cash_reconciliation_error=book.account.cash-cash_start-payment-trade_flow,
                passive_total_limit_exceeded=int(len(set(book.account.positions)|book.frozen_ids())>cfg.N)))
            for sid,q in sorted(book.account.positions.items()):
                tables['positions'].append(dict(session_date=iso,security_id=sid,shares=q,mark=marks[sid],value=q*marks[sid],
                    frozen=int(sid in book.frozen),reason=book.frozen.get(sid,''),industry=sec[sid].industry,asset_type='actual_shares'))
            for r in book.security_receivables:
                tables['positions'].append(dict(session_date=iso,security_id=r.security_id,shares=r.shares,mark=marks[r.security_id],value=r.shares*marks[r.security_id],
                    frozen=1,reason=r.reason,industry=sec[r.security_id].industry,asset_type='receivable_security',event_id=r.event_id))
            if d in signals:
                book.require_resolved_partial_delivery()
                week_score_start=len(tables['scores'])
                if _performance is not None:
                    frame=_performance.frames[d];window=frame['windows'][table_key]
                    chosen=frame['chosen'];candidates=window['candidates'];unscored=window['unscored']
                    # Universe rows reach the output only through full artifacts; lean and explore read the shared file.
                    if getattr(_performance,'artifact_mode','lean')=='full':tables['universe'].extend(frame['universe'])
                    tables['scores'].extend(window['scores'])
                else:
                    choice=choose_snapshot(inputs,session.close_time_utc)
                    if choice.snapshot is None:raise ValueError('no published member snapshot at signal')
                    by_issuer=defaultdict(list)
                    for member in choice.members:
                        item=elig[member.security_id]
                        if item.eligible:by_issuer[item.issuer_id].append(item)
                    chosen={selector.select_for_issuer(issuer,rs,d) for issuer,rs in sorted(by_issuer.items())};chosen.discard(None)
                    candidates=[];unscored={}
                    for member in choice.members:
                        sid=member.security_id;item=elig[sid]
                        tables['universe'].append(dict(signal_session_date=iso,security_id=sid,snapshot_id=choice.snapshot.snapshot_id,
                            holdings_as_of=choice.snapshot.holdings_as_of.isoformat(),available_at=choice.snapshot.available_at.isoformat(),
                            issuer_id=item.issuer_id,eligible=int(item.eligible),class_selected=int(sid in chosen),eligibility_reason=item.reason))
                    week_score_start=len(tables['scores'])
                    for sid in sorted(chosen):
                        score,tend,tstart,n,reason=engine.score(sid,d,cfg.L);item=elig[sid]
                        tables['scores'].append(dict(signal_session_date=iso,security_id=sid,issuer_id=item.issuer_id,industry=item.industry,L=cfg.L,
                            window_sessions=n,tri_start=tstart,tri_end=tend,score=score,unscorable_reason=reason))
                        if score is not None:candidates.append(Candidate(sid,item.issuer_id,item.industry,score))
                        else:unscored[sid]=reason
                frozen_ids=book.frozen_ids()
                gap_ids=set();forced_ids=set()
                for sid in book.account.positions:
                    # Ineligible spin-off child is an ordinary eligibility exit;
                    # input defects of an otherwise eligible held name must stop.
                    new_child=sid in book.spinoff_children
                    short_child=new_child and unscored.get(sid)=='insufficient_history'
                    if sid in chosen and sid in unscored and sid not in frozen_ids and not short_child:
                        # Rule v2: a missing price on the signal date keeps the held name this week (it holds a slot).
                        if unscored[sid]=='missing_price_at_signal':
                            gap_ids.add(sid);continue
                        # A gap inside the scoring window forces an exit at this week's execution. The name is not a
                        # candidate, so no selection rule can keep it, and the exit does not count against exit_cap.
                        if unscored[sid]=='price_gap_in_window':
                            forced_ids.add(sid);continue
                        raise ValueError('held eligible security cannot be scored: '+sid+' '+unscored[sid]+' '+iso)
                gap_retained=frozenset(gap_ids)
                forced_exits=frozenset(forced_ids)
                gap_exit_count+=len(forced_exits)
                frozen_ids=frozen_ids|gap_retained
                frozen_candidates=[Candidate(sid,sec[sid].issuer_id,sec[sid].industry,0.) for sid in sorted(frozen_ids)]
                held=[sid for sid in book.account.positions if sid not in frozen_ids]
                rule=raw.get('selection','default')
                if rule=='default':selected=select_portfolio(candidates,held,cfg.N,cfg.k,cfg.b,frozen=frozen_candidates)
                else:
                    params=dict(raw.get('selection_params',{}))
                    aux={'sector':sector_by_sid}
                    if rule in MATCHED_RULES:aux['match']=match_table[iso]
                    if rule in {'ent_pos','ent_neg'}:aux['short']=short_pct[d]
                    selected=SELECTION_RULES[rule](candidates,held,cfg.N,cfg.k,cfg.b,frozen=frozen_candidates,signal_date=iso,
                        held_meta={sid:(sec[sid].issuer_id,sec[sid].industry) for sid in held},aux=aux,**params)
                    selection_counts.update(selected.counts)
                ranked={c.security_id:i+1 for i,c in enumerate(sorted(candidates,key=lambda c:c.sort_key))}
                if _performance is None:
                    for row in tables['scores'][week_score_start:]:row['rank']=ranked.get(row['security_id'])
                target=selected.target_ids;swap=set(target)!=set(held)
                decisions={r.security_id:r for r in selected.decisions}
                industry_counts_after=json.dumps(Counter(sec[x].industry for x in set(target)|frozen_ids),sort_keys=True)
                for sid in sorted(set(decisions)|set(held)|frozen_ids):
                    dec=decisions.get(sid)
                    action=dec.action if dec else 'retain_frozen' if sid in frozen_ids else 'exit'
                    reason=dec.reason if dec else 'price_gap_retained_held' if sid in gap_retained else book.frozen.get(sid,'restricted_entitlement_occupies_slot') if sid in frozen_ids else 'lost_eligibility_or_unscorable_child'
                    if sid in forced_exits:action,reason='exit','unscorable_price_gap_exit'
                    tables['decisions'].append(dict(signal_session_date=iso,security_id=sid,issuer_id=sec[sid].issuer_id,industry=sec[sid].industry,
                        score=dec.score if dec else None,rank=ranked.get(sid),action=action,reason=reason,
                        buffer_score=selected.buffer_score,buffer_security_id=selected.buffer_security_id,
                        industry_counts_after=industry_counts_after,
                        target_count=len(target),frozen_count=len(frozen_ids),is_swap_week=int(swap)))
                next_index=positions[d]+1;next_day=dates[next_index] if next_index<len(dates) else None
                if next_day is not None and next_day<=end:pending=(target,iso,swap,gap_retained)
                tables['weekly_checks'].append(dict(signal_session_date=iso,is_swap_week=int(swap),frozen_count=len(frozen_ids),
                    target_count=len(target),total_capacity=max(0,cfg.N-len(frozen_ids)),
                    passive_limit_exceeded=int('passive_frozen_limit_exceeded' in selected.notes),
                    execution_session=next_day.isoformat() if next_day and next_day<=end else '',
                    buffer_score=selected.buffer_score,buffer_security_id=selected.buffer_security_id))
                coverage_row=dict(coverage[iso])
                if _stage4_context is not None:
                    coverage_row.update(source_coverage_window=20,actual_window=cfg.L,actual_n_scored=len(candidates))
                tables['weekly_coverage'].append(coverage_row)
            last_marks=marks
        except (ValueError,KeyError) as exc:
            stop={'attempted_session':iso,'message':str(exc),'last_valued_session':tables['daily_equity'][-1]['session_date'] if tables['daily_equity'] else None,
                'cash':book.account.cash,'positions':book.account.positions,'receivable_cash':book.account.receivable_cash,
                'receivable_security':[dict(event_id=r.event_id,security_id=r.security_id,shares=r.shares,reason=r.reason) for r in book.security_receivables],
                'processed_event_ids':sorted(book.account.processed_event_ids),'investor_decision_required':True}
            if _stage4_context is not None:
                from .stage4_evidence import unvalued_entitlements
                stop['unresolved_security_entitlements']=unvalued_entitlements(d,entitled,events_by_date[d],children,sec)
            tables['data_issues'].append(dict(session_date=iso,security_id='',severity='fatal',message=str(exc)));break
    tables['corporate_actions']=book.event_rows;tables['receivables']=book.receivable_rows;tables['cash_flows']=book.cash_flows
    # Rule v2 general settlements carry no equity ratio yet; the day's closing equity gives it.
    equity_on={r['session_date']:r['total_equity'] for r in tables['daily_equity']}
    for row in book.fractional_rows:
        if 'equity_ratio' not in row and row['session_date'] in equity_on:
            row['equity_at_confirmation']=equity_on[row['session_date']];row['equity_ratio']=row['amount']/row['equity_at_confirmation']
            row['exceeds_0_1_percent']=row['equity_ratio']>0.001
    v2_counts=Counter(r['severity'] for r in tables['data_issues'] if r['severity'] in {'stale_mark','void_order'})
    if book.fractional_settlements:tables['fractional_cash']=book.fractional_rows
    if stop is None and first_trade is not None:
        benchmark=build_benchmark(cfg.benchmark_security_id,inputs.prices,inputs.events,dates,init_capital,first_trade)
        tables['benchmark']=[dict(session_date=p.session_date.isoformat(),voo_tri=p.voo_tri,benchmark_value=p.benchmark_value) for p in benchmark.points]
        summary,rolling,dependency=summarize(tables['daily_equity'],tables['benchmark'],tables['trades'],tables['weekly_checks'],contributions,init_capital,first_trade,end)
        tables['rolling_relative']=rolling;tables['return_contributions']=dependency
        summary.update(slippage_bps=cfg.slippage_bps,held_event_counts=dict(Counter(r['event_type'] for r in book.event_rows)),
            frozen_security_days=sum(r['frozen'] for r in tables['positions']),passive_limit_weeks=sum(r['passive_limit_exceeded'] for r in tables['weekly_checks']),
            max_asset_identity_error=max(abs(r['total_equity']-r['cash']-r['stock_value']-r['receivable_cash']-r['receivable_security_value']) for r in tables['daily_equity']))
    else:
        summary={'status':'incomplete','scope':'development_only','failure':stop,'no_complete_return_claim':True,
            'last_valued_session':tables['daily_equity'][-1]['session_date'] if tables['daily_equity'] else None,'n_trades':len(tables['trades'])}
    if _stage4_context is not None:
        if book.fractional_settlements:
            summary.update(fractional_cash_approximation_total=sum(r['amount'] for r in book.fractional_rows if r['fractional_cash_valuation']=='approximation'),
                fractional_cash_events=book.fractional_rows,fractional_cash_over_0_1_percent=[r for r in book.fractional_rows if r['exceeds_0_1_percent']])
            manifest.update(fractional_right_assumption='authorized fallback: first regular-way raw close, no fee, receivable then available on tenth following trading session',
                supplemental_data_sha256=sha256_file(root/paths.DATA_VERSION))
        summary.update(no_parameter_search=False,stage4_configuration_id=raw['configuration_id'],rebalance=raw['rebalance'],
            no_holdout_computed=_stage4_context.scope=='development_only',scope=_stage4_context.scope)
        if _stage4_context.scope!='development_only':
            summary['experiment_label']='真实数据近似实验：唯一预先选定配置的保留段一次性检验；不再调参'
    if raw.get('selection','default')!='default':manifest['selection_counts']=dict(sorted(selection_counts.items()))
    if gap_exit_count:summary.update(unscorable_price_gap_exits=gap_exit_count)
    cap_trims=sum(1 for t in tables['trades'] if t['reason']=='cap_trim')
    if cap_trims:summary.update(single_stock_cap_trims=cap_trims)
    if v2_counts:
        # Rule v2 events are reported only when they occur, so runs without them keep their reviewed bytes.
        summary.update(accounting_rules_version=ACCOUNTING_RULES_VERSION,single_session_stale_marks=v2_counts.get('stale_mark',0),
            voided_orders=v2_counts.get('void_order',0))
    if _performance is None:write_artifacts(output,tables,summary,manifest,stop)
    else:
        manifest.update(performance_cache_key=_performance.cache_key,performance_data_scope=getattr(_performance,'data_scope','uniform'))
        if _performance.artifact_mode=='explore':
            # No decision projection is written, so the score lookup is not built.
            write_artifacts(output,tables,summary,manifest,stop,artifact_mode='explore')
        else:
            shared={'universe':_performance.shared['universe'],'scores':_performance.shared[table_key],'score_lookup':_performance.score_lookup(table_key)}
            write_artifacts(output,tables,summary,manifest,stop,artifact_mode=_performance.artifact_mode,shared=shared)
    print(json.dumps(summary,ensure_ascii=False,indent=2));return 0 if summary['status']=='completed' else 2
