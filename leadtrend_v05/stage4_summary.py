"""Prespecified grid aggregation and a separate, one-shot holdout gate."""
from pathlib import Path
from datetime import date
import csv
import json
import math
import uuid
import statistics
from .stage4_registry import verify,digest
from .stage2_enrich import write_csv
from .perf_artifacts import read_table as read_csv
from . import paths
from .stage4_remediation import holdout_authorized

def neighbors(p,registry):
    ls=sorted({x['L'] for x in registry['configurations']});ns=sorted({x['N'] for x in registry['configurations']})
    return [x['id'] for x in registry['configurations'] if x['b']==p['b'] and x['rebalance']==p['rebalance'] and
        abs(ls.index(x['L'])-ls.index(p['L']))+abs(ns.index(x['N'])-ns.index(p['N']))==1]

def evaluate_regions(registry,rows,exploration=False):
    by={(r['configuration_id'],int(r['cost_bps'])):r for r in rows};criteria=[]
    # Exploration runs record acceptance NOT_RUN; their completion rests on the in-engine explore_check.
    if exploration:complete=lambda r:r.get('status')=='completed' and r.get('explore_check_status')=='PASS'
    else:complete=lambda r:r.get('status')=='completed' and r.get('acceptance_status')=='PASS'
    for p in registry['configurations']:
        ns=neighbors(p,registry);r10=by[p['id'],10];r5=by[p['id'],5]
        wins=sum(complete(by[n,5]) and by[n,5].get('annualized_excess',-math.inf)>0 for n in ns)
        years=sum(r10.get(f'excess_{y}') is not None and r10[f'excess_{y}']>0 for y in range(2020,2024))
        a=complete(r10) and r10.get('annualized_excess',-math.inf)>0
        b=wins*2>len(ns);c=years>=3;d=complete(r10) and complete(r5)
        neighborhood=[r10]+[by[n,10] for n in ns]
        mean=sum(r['annualized_excess'] for r in neighborhood)/len(neighborhood) if all(complete(r) and r.get('annualized_excess') is not None for r in neighborhood) else None
        criteria.append(dict(configuration_id=p['id'],L=p['L'],N=p['N'],k=p['k'],b=p['b'],rebalance=p['rebalance'],
            a_stress_beats_voo=bool(a),b_neighbor_majority=bool(b),c_three_years=bool(c),d_complete=bool(d),
            neighbor_ids='|'.join(ns),neighbor_count=len(ns),neighbor_wins_5bps=wins,winning_years_10bps=years,
            annualized_excess_10bps=r10.get('annualized_excess'),neighborhood_mean_10bps=mean,
            neighborhood_mean_selection_value='-infinity' if mean is None else mean,
            candidate=bool(a and b and c and d)))
    candidates=[r for r in criteria if r['candidate']]
    selected=sorted(candidates,key=lambda r:(-(r['neighborhood_mean_10bps'] if r['neighborhood_mean_10bps'] is not None else -math.inf),r['configuration_id']))[:1]
    return criteria,[r['configuration_id'] for r in selected]

def interval(equity,benchmark,start,end,first,initial):
    es={r['session_date']:float(r['total_equity']) for r in equity};bs={r['session_date']:float(r['benchmark_value']) for r in benchmark}
    stop=max((d for d in es if d<=end),default=None)
    if stop is None:return None
    if start=='opening':sv=bv=initial;begin=first
    else:
        begin=max((d for d in es if d<=start and d in bs),default=None)
        if begin is None:return None
        sv=es[begin];bv=bs[begin]
    if stop not in bs or begin is None:return None
    years=(date.fromisoformat(stop)-date.fromisoformat(begin)).days/365.25
    sr=es[stop]/sv-1;br=bs[stop]/bv-1
    sa=(1+sr)**(1/years)-1 if years>0 else None;ba=(1+br)**(1/years)-1 if years>0 else None
    return dict(start=begin,end=stop,strategy_return=sr,benchmark_return=br,
                strategy_annualized=sa,benchmark_annualized=ba,annualized_excess=sa-ba if years>0 else None)

def aggregate(root,*,make_decision=True,grid_dir=None,output_dir=None,decision_path=None,exploration=False):
    root=Path(root);registry=verify(root);out=Path(output_dir) if output_dir else root/paths.RUNS/'grid_summary';out.mkdir(parents=True,exist_ok=True)
    grid=Path(grid_dir) if grid_dir else root/paths.STAGE4_RUNS
    ticker_by={r['security_id']:r['ticker'] for r in read_csv(root/paths.NORMALIZED/'securities.csv')}
    suffix='_explore' if exploration else ''
    rows=[]
    for p in registry['configurations']:
        for cost in registry['costs_bps']:
            folder=grid/p['id']/f'cost_{cost}bps'
            checkpoint=folder/'stage4_completion.json'
            if not checkpoint.exists():checkpoint=folder/'completion.json'
            completion=json.loads(checkpoint.read_text(encoding='utf-8'))
            if completion.get('verification_level')=='explore' and not exploration:
                raise ValueError('explore-level runs are screening results, not formal outcomes; rerun with exploration enabled')
            s=json.loads((folder/'summary.json').read_text(encoding='utf-8')) if (folder/'summary.json').exists() else {}
            row=dict(configuration_id=p['id'],L=p['L'],N=p['N'],k=p['k'],b=p['b'],rebalance=p['rebalance'],cost_bps=cost,
                status=completion['status'],acceptance_status=completion.get('acceptance',{}).get('status','NOT_COMPLETE'),
                failure=((s.get('failure') or {}).get('message') or str(completion.get('failure'))) if completion['status']!='completed' else '',
                failure_state_path=str((folder/'last_explainable_state.json').relative_to(root)) if completion['status']!='completed' else '',
                last_valued_session=s.get('last_valued_session',s.get('comparison_end_session')),
                total_return=None,annualized_return=None,benchmark_annualized_return=None,annualized_excess=None)
            if exploration:row.update(result_class='explore',explore_check_status=(completion.get('explore_check') or {}).get('status'))
            if completion['status']=='completed':
                equity=read_csv(folder/'daily_equity.csv');benchmark=read_csv(folder/'benchmark.csv')
                first=s['comparison_start_session'];full=interval(equity,benchmark,'opening',registry['development']['end'],first,100000)
                row.update(total_return=s['total_return'],annualized_return=s['annualized_return'],benchmark_annualized_return=full['benchmark_annualized'],
                    annualized_excess=full['annualized_excess'],max_drawdown=s['drawdown']['max_drawdown'],
                    longest_relative_underwater_days=s['relative_nav_underwater']['longest_underwater_calendar_days'],
                    relative_underwater_unrecovered=s['relative_nav_underwater']['unrecovered_at_end'],
                    longest_negative_rolling_52_week_relative_weeks=s['longest_negative_rolling_52_week_relative_return_weeks'],
                    annual_turnover_one_way=s['annual_turnover_one_way_half_two_sided'],n_trades=s['n_trades'],
                    fees_total=s['fees_total'],slippage_cost=s['slippage_cost_dollars'],fees_plus_slippage=s['fees_total']+s['slippage_cost_dollars'],
                    average_holding_weeks=s['average_completed_holding_weeks'],sub_500_dollar_trades=s['sub_500_dollar_trades'],
                    top_five_contributions=json.dumps([dict(x,ticker=ticker_by.get(x['security_id'],x['security_id'])) for x in s['top_five_return_contributions']],separators=(',',':')))
                for y in range(2020,2024):
                    x=interval(equity,benchmark,f'{y-1}-12-31',f'{y}-12-31',first,100000)
                    row[f'strategy_{y}']=x['strategy_return'];row[f'voo_{y}']=x['benchmark_return'];row[f'excess_{y}']=x['strategy_return']-x['benchmark_return']
                for label,start,end in [('high_missing','opening','2021-12-31'),('lower_missing','2021-12-31','2024-06-28')]:
                    x=interval(equity,benchmark,start,end,first,100000)
                    row[label+'_annualized_excess']=x['annualized_excess'];row[label+'_strategy_annualized']=x['strategy_annualized'];row[label+'_voo_annualized']=x['benchmark_annualized']
                row['summary_sha256']=digest(folder/'summary.json')
                row.update(fractional_cash_approximation_total=s.get('fractional_cash_approximation_total',0),
                    fractional_cash_max_equity_ratio=max((x['equity_ratio'] for x in s.get('fractional_cash_events',[])),default=0),
                    fractional_cash_over_0_1_percent=len(s.get('fractional_cash_over_0_1_percent',[])))
            rows.append(row)
    fields=list(dict.fromkeys(k for r in rows for k in r))
    write_csv(out/('all_results'+suffix+'.csv'),rows,fields)
    criteria,selected=evaluate_regions(registry,rows,exploration=exploration)
    write_csv(out/('region_criteria'+suffix+'.csv'),criteria,list(criteria[0]))
    hashreg=digest(root/paths.STAGE4_REGISTRY)
    decision={'registry_sha256':hashreg,'attempted_configurations':72,'cost_runs':144,
        'results_csv_sha256':digest(out/('all_results'+suffix+'.csv')),'criteria_csv_sha256':digest(out/('region_criteria'+suffix+'.csv')),
        'candidate_count':sum(r['candidate'] for r in criteria),'selected':selected,
        'selected_evidence':[r for r in criteria if r['configuration_id'] in selected],
        'conclusion':'唯一预定区域代表点获准保留段一次性检验' if selected else '开发段未发现满足事先条件的参数区域',
        'holdout_permitted':len(selected)==1,'holdout_not_computed_by_aggregation':True}
    if not exploration and not holdout_authorized(root):
        decision.update(rule_qualified=bool(selected),holdout_permitted=False,holdout_execution_authorized=False,
            execution_note='Holdout forbidden until the investor creates the authorization file after Claude review',
            conclusion='出现满足原登记条件的唯一选定点；本轮不授权运行保留段' if selected else '开发段未发现满足事先条件的参数区域')
    path=Path(decision_path) if decision_path else out/'holdout_decision.json'
    if exploration:
        # Exploration outputs are explicitly not a holdout decision basis.
        decision=dict(exploration_only=True,result_class='explore',
            note='探索档汇总：仅供筛选，不得作为正式结果或保留段依据；不生成、不修改 holdout_decision',
            rows=len(rows),selected=selected,candidate_count=sum(r['candidate'] for r in criteria))
    elif make_decision:
        if path.exists():
            if json.loads(path.read_text(encoding='utf-8'))!=decision:raise ValueError('existing holdout decision differs; must disclose amendment, never overwrite')
        else:
            with path.open('x',encoding='utf-8') as f:f.write(json.dumps(decision,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    manifest={'registry_sha256':hashreg,'rows':len(rows),'configurations':len({r['configuration_id'] for r in rows}),
        'completed_cost_runs':sum(r['status']=='completed' for r in rows),'incomplete_cost_runs':sum(r['status']=='incomplete' for r in rows),
        'acceptance_failed_cost_runs':sum(r['status']=='acceptance_failed' for r in rows),'decision':decision}
    if exploration:
        manifest['exploration_only']=True
        manifest['holdout_decision']='not generated for exploration runs'
    (out/('summary_manifest'+suffix+'.json')).write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    coverage=read_csv(root/paths.WEEKLY_COVERAGE);periods=[]
    for label,start,end in [('high_missing','2019-11-29','2021-12-31'),('lower_missing','2022-01-01','2024-06-28')]:
        ratios=[float(r['historical_no_price_ratio']) for r in coverage if start<=r['signal_session_date']<=end and int(r['n_eligible'])>0]
        periods.append(dict(period=label,start=start,end=end,signal_weeks=len(ratios),historical_missing_min=min(ratios),historical_missing_median=statistics.median(ratios),historical_missing_max=max(ratios)))
    write_csv(out/('subperiod_coverage_summary'+suffix+'.csv'),periods,list(periods[0]))
    return rows,criteria,decision

def validate_holdout_decision(root):
    root=Path(root);registry=verify(root);path=root/paths.HOLDOUT_DECISION
    if not path.exists():raise ValueError('holdout refused: missing decision file')
    d=json.loads(path.read_text(encoding='utf-8'))
    if d.get('registry_sha256')!=digest(root/paths.STAGE4_REGISTRY):raise ValueError('holdout registry hash mismatch')
    if not isinstance(d.get('selected'),list) or len(d['selected'])!=1:raise ValueError('holdout requires exactly one selected configuration')
    if not d.get('holdout_permitted') or len(d.get('selected_evidence',[]))!=1:raise ValueError('no positive holdout authorization evidence')
    out=root/paths.STAGE4_RESULTS/'summary'
    if digest(out/'all_results.csv')!=d['results_csv_sha256'] or digest(out/'region_criteria.csv')!=d['criteria_csv_sha256']:
        raise ValueError('holdout evidence hash mismatch')
    # Re-evaluate all a--d and the prescribed selection; supplied booleans alone
    # never authorize a run. CSV numeric/boolean fields are parsed explicitly.
    rows=read_csv(out/'all_results.csv')
    for r in rows:
        r['cost_bps']=int(r['cost_bps'])
        for k in ['annualized_excess']+[f'excess_{y}' for y in range(2020,2024)]:r[k]=float(r[k]) if r.get(k) else None
    criteria,selected=evaluate_regions(registry,rows)
    if selected!=d['selected']:raise ValueError('decision does not match prescribed regional selection')
    expected=[r for r in criteria if r['configuration_id'] in selected]
    if expected!=d['selected_evidence']:raise ValueError('holdout a--d evidence does not reproduce')
    return registry,d

def verify_holdout_claim(root,configuration_id,token):
    _,decision=validate_holdout_decision(root)
    path=Path(root)/paths.HOLDOUT_LOCK
    if not token or not path.exists():raise ValueError('holdout requires exclusive one-shot execution claim')
    claim=json.loads(path.read_text(encoding='utf-8'))
    if claim.get('token')!=token or claim.get('configuration_id')!=configuration_id or decision['selected']!=[configuration_id]:
        raise ValueError('invalid holdout execution claim')

def run_holdout(root):
    from .stage4_remediation import ensure_no_holdout
    ensure_no_holdout(root)
    root=Path(root);registry,decision=validate_holdout_decision(root)
    path=root/paths.HOLDOUT_LOCK;path.parent.mkdir(parents=True,exist_ok=True);token=uuid.uuid4().hex;cid=decision['selected'][0]
    try:
        with path.open('x',encoding='utf-8') as f:f.write(json.dumps({'configuration_id':cid,'registry_sha256':digest(root/paths.STAGE4_REGISTRY),'token':token},indent=2)+'\n')
    except FileExistsError:raise ValueError('holdout already claimed; second execution forbidden')
    from .stage4_runner import PreparedGrid,run_point
    from .stage4_acceptance import audit
    ctx=PreparedGrid(root,holdout=True,decision=cid,claim_token=token)
    point=next(p for p in registry['configurations'] if p['id']==cid);results=[]
    for cost in registry['costs_bps']:
        folder=root/paths.HOLDOUT/cid/f'cost_{cost}bps'
        code=run_point(ctx,point,cost,folder)
        summary=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
        acceptance=audit(folder,root,ctx=ctx) if summary['status']=='completed' else {'status':'NOT_COMPLETE'}
        results.append({'configuration_id':cid,'cost_bps':cost,'exit_code':code,'status':summary['status'],'acceptance':acceptance})
    (root/paths.HOLDOUT_RESULTS).write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0 if all(r['status']=='completed' and r['acceptance']['status']=='PASS' for r in results) else 2

def heatmaps(root,rows,criteria,output_dir=None,exploration=False):
    import sys
    import os
    if output_dir is not None:sys.dont_write_bytecode=True
    os.environ.setdefault('MPLCONFIGDIR',str(Path(output_dir)/'.mplcache' if output_dir else Path(root)/paths.PLOT_CACHE))
    local_dependencies=Path(root)/paths.PLOT_DEPS
    if local_dependencies.is_dir():sys.path.insert(0,str(local_dependencies))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    root=Path(root);out=Path(output_dir) if output_dir else root/paths.RUNS/'grid_summary';out.mkdir(parents=True,exist_ok=True);registry=verify(root)
    marker='exploration_' if exploration else ''
    label=' | EXPLORATION ONLY (not a formal result)' if exploration else ''
    by={(r['configuration_id'],r['cost_bps']):r for r in rows};cr={r['configuration_id']:r for r in criteria}
    ls=sorted({p['L'] for p in registry['configurations']});ns=sorted({p['N'] for p in registry['configurations']})
    keys=['annualized_excess','neighborhood_mean_10bps','annual_turnover_one_way','max_drawdown']
    for b in [2,3]:
        for R in ['R1','R2']:
            figure,axes=plt.subplots(1,4,figsize=(23,5),constrained_layout=True);csvrows=[]
            selected=[p for p in registry['configurations'] if p['b']==b and p['rebalance']==R]
            for p in selected:
                r=by[p['id'],10];c=cr[p['id']]
                csvrows.append(dict(L=p['L'],N=p['N'],status=r['status'],annualized_excess=r.get('annualized_excess'),
                    neighborhood_mean_10bps=c['neighborhood_mean_10bps'],annual_turnover_one_way=r.get('annual_turnover_one_way'),max_drawdown=r.get('max_drawdown')))
            for axis,key,title in zip(axes,keys,['10bp CAGR excess (pp)','Own + neighbors CAGR excess (pp)','One-way annual turnover (x)','Maximum drawdown (%)']):
                data=np.full((len(ns),len(ls)),np.nan)
                for r in csvrows:
                    value=r.get(key)
                    if value is not None:data[ns.index(r['N']),ls.index(r['L'])]=value*(1 if key=='annual_turnover_one_way' else 100)
                cmap=plt.get_cmap('RdYlGn' if key in keys[:2] else 'viridis').copy();cmap.set_bad('#d9d9d9')
                if key in keys[:2]:
                    allvals=[(x.get('annualized_excess') if key=='annualized_excess' else cr[x['configuration_id']]['neighborhood_mean_10bps']) for x in rows if x['cost_bps']==10]
                    extent=max([abs(x*100) for x in allvals if x is not None]+[1]);vmin,vmax=-extent,extent
                else:vmin,vmax=None,None
                im=axis.imshow(np.ma.masked_invalid(data),aspect='auto',cmap=cmap,vmin=vmin,vmax=vmax)
                axis.set_xticks(range(len(ls)),ls);axis.set_yticks(range(len(ns)),ns);axis.set_xlabel('L (sessions)');axis.set_ylabel('N');axis.set_title(title)
                for i in range(len(ns)):
                    for j in range(len(ls)):axis.text(j,i,'NA' if np.isnan(data[i,j]) else f'{data[i,j]:.1f}',ha='center',va='center',fontsize=9)
                figure.colorbar(im,ax=axis,shrink=.8)
            figure.suptitle(f'LeadTrend development only{label} | b={b}, {R} | gray = incomplete / unavailable neighborhood')
            stem='heatmap_'+marker+f'b{b}_{R}';figure.savefig(out/(stem+'.png'),dpi=180);plt.close(figure)
            write_csv(out/(stem+'.csv'),csvrows,list(csvrows[0]))
