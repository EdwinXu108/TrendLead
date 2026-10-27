"""TERRA_R1 registered runs and conditional paired bootstrap. No reserved-segment access."""
from __future__ import annotations
import csv
import bisect
import hashlib
import json
import math
import subprocess
from datetime import date
from pathlib import Path

import numpy as np
from leadtrend_db import api
from .engine import Backtest, DataGapError
from .runner import write_csv
from . import metrics

ROOT = Path('work/terra_r1')
DB = Path('data/db/leadtrend_db_v1.sqlite')
SPEC = Path('research/bt_v3/TERRA_R1/spec.json')
EVENTS = Path('config/terra_r1_events_v1.json')
IDS = ('MOM_EQ', 'MOM_AMT', 'SIZE_EQ', 'SIZE_AMT', 'POOL_AMT', 'TOP10_AMT', 'TOP10_EQ')
REGISTRATION = '6dc6979'
SEED = 20261027
PRIOR_FINGERPRINT = '32a6ab2dfb862e07ee346f1fe24cefbc145ef7294b3fae899ffd54bda4689e4e'
PRIOR_COMMIT = 'f504c82d807f9deb7495562c0761a5ae2dd98d8b'


def sha(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda: f.read(8388608), b''):
            h.update(block)
    return h.hexdigest()


def json_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)+'\n', encoding='utf8')


def market_snapshot(engine):
    return {name: getattr(engine, name) for name in ('calendar','full_calendar','signal_days','pools','pool_days',
                                                       'tri','opens','closes','issuer','actions')}


def config(name, bps, start, shift=0):
    cfg = dict(run_id=f'{name}_{bps}bp'+(f'_shift{shift}' if shift else ''), protocol='terra_r1_v1',
               start=start, end='2024-06-28', capital=100000.0, fee_per_order=1.0, cost_bps=bps,
               L=90, N=10, K=4, b=2, rebalance='R2', mode='momentum',
               ranking='size' if name.startswith('SIZE') else 'momentum',
               buy_weighting='amount' if name.endswith('AMT') else 'equal')
    if name in IDS[4:]:
        cfg.update(mode='pool', K=1, top_n=None if name == 'POOL_AMT' else 10,
                   weighting='ew' if name == 'TOP10_EQ' else 'cw', fractional=True)
    return cfg


def save_result(bt, result, cfg, fingerprint):
    out = ROOT / 'runs' / cfg['run_id']; out.mkdir(parents=True, exist_ok=True)
    rf = api.rf_daily(cfg['start'], cfg['end'], DB)
    summary = metrics.compute(result, rf)
    if abs(summary['total_pnl'] - (result['daily'][-1]['equity'] - cfg['capital'])) > 1e-5:
        raise ArithmeticError('final issuer P&L does not reconcile')
    summary['config'] = cfg
    files = {
        'daily_equity.csv': (['date','cash','positions_value','equity','benchmark'], result['daily']),
        'positions.csv': (['date','tranche','security_id','shares','close','value','stale','company_amount_cents'], result['positions']),
        'trades.csv': (['date','tranche','security_id','side','shares','price','market_price','fee','reason','signal_date'], result['trades']),
        'weekly_decisions.csv': (['signal_date','tranche','security_id','action','rank','score'], result['decisions']),
        'score_counts.csv': (['date','pool_companies','scoreable','insufficient_history','missing_days'], result['score_counts']),
        'corporate_actions.csv': (['date','tranche','security_id','issuer','method','shares','ratio','per_share',
                                   'child_security_id','gross','net','fee','cost','cash_available','source'], result['event_flows'])}
    for filename, (header, rows) in files.items():
        write_csv(out / filename, header, rows)
    json_write(out/'config.json', cfg)
    json_write(out/'summary.json', summary)
    json_write(out/'complete.json', {'fingerprint':fingerprint})
    return summary


def read_csv(path):
    with path.open(encoding='utf8', newline='') as f:
        return list(csv.DictReader(f))


def weekly_paths():
    data = {}
    for name in IDS[:4]:
        rows = read_csv(ROOT/'runs'/f'{name}_10bp'/'daily_equity.csv')
        data[name] = {r['date']: (float(r['equity']),float(r['benchmark'])) for r in rows}
    dates = sorted(data[IDS[0]])
    if any(sorted(v) != dates for v in data.values()):
        raise ValueError('base paths do not share dates')
    prev = {name: 100000.0 for name in IDS[:4]}; previous_bench = 100000.0
    buckets = {}
    for d in dates:
        iso = date.fromisoformat(d).isocalendar(); segment = 'A' if d <= '2019-11-29' else 'B'
        key = (segment, iso.year, iso.week)
        row = buckets.setdefault(key, dict(segment=segment, iso_year=iso.year, iso_week=iso.week,
                                           start=d, end=d, sessions=0, VOO=0.0, **{n:0.0 for n in IDS[:4]}))
        row['end'] = d; row['sessions'] += 1
        for name in IDS[:4]:
            equity, bench = data[name][d]
            row[name] += math.log(equity/prev[name]); prev[name] = equity
            if abs(bench-data[IDS[0]][d][1]) > .00001:
                raise ValueError('benchmark mismatch')
        bench = data[IDS[0]][d][1]; row['VOO'] += math.log(bench/previous_bench); previous_bench = bench
    return list(buckets.values())


def comparisons(v):
    return np.stack([v[:,0]-v[:,2], v[:,1]-v[:,3], v[:,1]-v[:,0], v[:,3]-v[:,2],
                     (v[:,1]-v[:,0])-(v[:,3]-v[:,2])],axis=1)


def bootstrap(weeks):
    rng = np.random.default_rng(SEED)
    out = []
    names = ['MOM_EQ - SIZE_EQ','MOM_AMT - SIZE_AMT','MOM_AMT - MOM_EQ',
             'SIZE_AMT - SIZE_EQ','interaction']
    for period in ('all','A','B'):
        rows = [r for r in weeks if period == 'all' or r['segment'] == period]
        matrix = np.array([[r[n] for n in IDS[:4]] for r in rows]); counts=np.array([r['sessions'] for r in rows])
        n=len(rows); blocks=math.ceil(n/13)
        starts=rng.integers(0,n,size=(5000,blocks))
        ix=((starts[:,:,None]+np.arange(13))%n).reshape(5000,-1)[:,:n]
        sample=np.expm1(matrix[ix].sum(axis=1) * (252/counts[ix].sum(axis=1))[:,None])
        point=np.expm1(matrix.sum(axis=0)*252/counts.sum()).reshape(1,4)
        differences=comparisons(sample); observed=comparisons(point)[0]
        for i, name in enumerate(names):
            lo,hi,lo99,hi99=np.quantile(differences[:,i],[.025,.975,.005,.995])
            out.append(dict(period=period, comparison=name, point_pp=100*observed[i],
                            ci95_low_pp=100*lo, ci95_high_pp=100*hi,
                            ci99_low_pp=100*lo99, ci99_high_pp=100*hi99))
    return out


def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    dirty=subprocess.check_output(['git','status','--porcelain','--','leadtrend_bt','leadtrend_db/api.py',
                                   'tests_bt',str(SPEC),str(EVENTS)],text=True)
    if dirty:
        raise RuntimeError('registered implementation or tests have uncommitted changes')
    dbhash=sha(DB); spechash=sha(SPEC); eventhash=sha(EVENTS)
    meta=dict(api.connect(DB).execute('select key,value from meta'))
    if meta.get('content_sha256') != '97b55e453e09b5e40570ab5ab9ae9d6e317131ab448c9b11ec719cc3d06bde6f':
        raise RuntimeError('unexpected database content hash')
    if dbhash != '7564971e17d828289fdf46cb04413fce3d1ec60faac5461d5376774ce9751e87':
        raise RuntimeError('database file differs from registered input')
    sigdays=api.friday_signal_days('2014-02-28','2014-04-01',DB)
    basecfg=config(IDS[0],10,sigdays[0]); loader=Backtest(basecfg,db_path=DB); loader.load()
    signal_market=market_snapshot(loader)
    pool_loader=Backtest(config('TOP10_AMT',10,sigdays[0]),db_path=DB); pool_loader.load()
    pool_market=market_snapshot(pool_loader)
    prep=ROOT/'prepared'; prep.mkdir(exist_ok=True)
    for sid, filename in [('CIKCLS:0001288776:CL_A','google_a_derived_tri.csv'),
                          ('CUSIP:26614N201','dowdupont_derived_tri.csv'),
                          ('CUSIP:369604301','ge_derived_tri.csv')]:
        write_csv(prep/filename,['date','tri'],[{'date':d,'tri':v} for d,v in signal_market['tri'].get(sid,{}).items()])
    plan=[config(name,bps,sigdays[0]) for name in IDS for bps in (5,10)]
    plan += [config(name,10,sigdays[s],s) for name in IDS[:4] for s in range(1,5)]
    fingerprint=hashlib.sha256(f'{commit}|{dbhash}|{spechash}|{eventhash}|terra_r1_v1'.encode()).hexdigest()
    manifest=dict(protocol='terra_r1_v1',registration_commit=REGISTRATION,implementation_commit=commit,
                  database_content_sha256=meta['content_sha256'],database_file_sha256=dbhash,
                  correction_sha256=eventhash,spec_sha256=spechash,fingerprint=fingerprint,seed=SEED,
                  run_command='python -B -m leadtrend_bt.terra_r1',
                  test_command='python -B -m pytest tests_db tests_bt tests_v05 -q -p no:cacheprovider --basetemp=work/terra_r1/pytest',
                  tests=(ROOT/'test_result.txt').read_text(encoding='utf8') if (ROOT/'test_result.txt').exists() else 'not recorded', runs=[])
    json_write(ROOT/'manifest.json',manifest)
    for cfg in plan:
        rid=cfg['run_id']; out=ROOT/'runs'/rid; marker=out/'complete.json'
        item=dict(id=rid,status='not_started')
        old_marker=json.loads(marker.read_text(encoding='utf8')) if marker.exists() else {}
        failure=out/'failure.json'
        old_failure=json.loads(failure.read_text(encoding='utf8')) if failure.exists() else {}
        unaffected_mode=cfg['ranking']=='size' or cfg['mode']=='pool'
        actions_file=out/'corporate_actions.csv'
        no_ge_exposure=(actions_file.exists() and not any(
            r['security_id']=='CUSIP:369604301' and r['date']=='2024-04-02'
            for r in read_csv(actions_file)))
        if old_marker.get('fingerprint')==fingerprint:
            item['status']='complete_cached'
        elif old_marker.get('fingerprint')==PRIOR_FINGERPRINT and unaffected_mode and no_ge_exposure:
            # Size ranking only uses the completeness of GE's TRI, not its value. Structural baselines do not
            # score it. Neither held GE on the event date, so the verified child value cannot affect their paths.
            item.update(status='complete_reused_unaffected',source_commit=PRIOR_COMMIT,
                        source_fingerprint=PRIOR_FINGERPRINT,
                        reason='GE event not held; ranking does not use TRI values')
        elif (old_failure.get('fingerprint')==PRIOR_FINGERPRINT and cfg['mode']=='pool'
              and 'CUSIP:H1467J104 2015-12-01' in old_failure.get('error','')):
            item.update(status='blocked_missing_prices',reason=old_failure['error'],source_commit=PRIOR_COMMIT,
                        source_fingerprint=PRIOR_FINGERPRINT)
        else:
            bt=Backtest(cfg,db_path=DB,market=pool_market if cfg['mode']=='pool' else signal_market)
            try:
                result=bt.run(); save_result(bt,result,cfg,fingerprint); item['status']='complete'
            except Exception as e:
                item['status']=('blocked_event' if isinstance(e,DataGapError) and ('unvalued' in str(e) or 'valuation' in str(e))
                                else 'blocked_missing_prices' if isinstance(e,DataGapError) else 'failed')
                item['reason']=f'{type(e).__name__}: {e}'
                out.mkdir(parents=True,exist_ok=True)
                json_write(out/'failure.json',dict(config=cfg,error=item['reason'],fingerprint=fingerprint))
                write_csv(out/'partial_daily.csv',['date','cash','positions_value','equity','benchmark'],bt.daily)
                write_csv(out/'partial_positions.csv',['date','tranche','security_id','shares','close','value','stale'],bt.position_rows)
                write_csv(out/'partial_trades.csv',['date','tranche','security_id','side','shares','price','fee','reason','signal_date'],bt.trades)
                write_csv(out/'partial_decisions.csv',['signal_date','tranche','security_id','action','rank','score'],bt.decisions)
                write_csv(out/'partial_actions.csv',['date','tranche','security_id','issuer','method','shares','gross','net','source'],bt.event_flows)
            print(rid,item['status'],item.get('reason',''),flush=True)
        manifest['runs'].append(item); json_write(ROOT/'manifest.json',manifest)
    completed={x['id'] for x in manifest['runs'] if x['status'].startswith('complete')}
    summaries=[]; all_summaries={}
    for rid in sorted(completed):
        s=json.loads((ROOT/'runs'/rid/'summary.json').read_text(encoding='utf8'))
        all_summaries[rid]=s
        row={'run_id':rid,'cagr':s['strategy']['cagr'],'voo_cagr':s['benchmark']['cagr'],
             'excess':s['excess_cagr'],'a_excess':s['periods']['A']['excess'],'b_excess':s['periods']['B']['excess'],
             'max_drawdown':s['strategy']['max_drawdown'],'max_single_weight':s['max_single_weight'],
             'max_top5_issuer_weight':s['max_top5_issuer_weight'],'fees':s['fees'],
             'total_execution_cost':s['total_execution_cost'],'turnover':s['turnover_one_way_annual']}
        summaries.append(row)
    if summaries:
        write_csv(ROOT/'summary.csv',list(summaries[0]),summaries)
    comparisons_out=[]
    if all(f'{n}_10bp' in completed for n in IDS[:4]):
        weeks=weekly_paths(); write_csv(ROOT/'weekly_returns.csv',list(weeks[0]),weeks)
        comparisons_out=bootstrap(weeks); write_csv(ROOT/'paired_comparisons.csv',list(comparisons_out[0]),comparisons_out)
    flows=[]
    for rid in sorted(completed):
        for r in read_csv(ROOT/'runs'/rid/'corporate_actions.csv'):
            r['run_id']=rid; flows.append(r)
    # Include scoring exposure even when no account held the issuer on the event date.
    calendar=signal_market['full_calendar']; all_signals=signal_market['signal_days']
    watched=[]
    for sid, events in signal_market['actions'].items():
        for event in events:
            if event['action_type'] not in ('spinoff','class_distribution') and not event.get('needs_review'):
                continue
            ed=event['action_date']; ei=bisect.bisect_left(calendar,ed)
            touched=[]
            for d in all_signals:
                i=bisect.bisect_left(calendar,d)
                if not (i-90 < ei <= i):
                    continue
                pool=signal_market['pools'].get(d,{})
                if any(s['security_id']==sid for s in pool.get('securities',[])):
                    series=signal_market['tri'].get(sid,{})
                    if i>=90 and all(series.get(x,0)>0 for x in calendar[i-90:i+1]):
                        touched.append(d)
            watched.append(dict(security_id=sid,date=ed,method=event['action_type'],
                source=event.get('source_url') or event.get('source_id',''),needs_review=event.get('needs_review',0),
                touched=touched))
    # Development-period examples in the pre-existing validation; a disagreement is not proof either series is right.
    discrepancies=[('CUSIP:025537101','2020-02-07'),('CUSIP:025537101','2020-05-07'),
        ('CUSIP:025537101','2020-08-07'),('CUSIP:09260D107','2022-07-29'),
        ('CUSIP:12572Q105','2021-12-27'),('CUSIP:20825C104','2023-05-15'),
        ('CUSIP:24703L202','2021-11-02'),('CUSIP:26614N201','2019-06-03'),
        ('CUSIP:30161N101','2019-08-14'),('CUSIP:369604301','2023-01-04'),
        ('CUSIP:438516205','2020-05-14'),('CUSIP:438516205','2020-08-13'),
        ('CUSIP:743315103','2021-12-17')]
    for sid, ed in discrepancies:
        ei=bisect.bisect_left(calendar,ed); series=signal_market['tri'].get(sid,{})
        touched=[]
        for d in all_signals:
            i=bisect.bisect_left(calendar,d)
            if i>=90 and i-90<ei<=i and any(s['security_id']==sid for s in signal_market['pools'].get(d,{}).get('securities',[])) and all(series.get(x,0)>0 for x in calendar[i-90:i+1]):
                touched.append(d)
        watched.append(dict(security_id=sid,date=ed,method='tri_discrepancy',
            source='work/bt_v1/tri_validation.json: development-period comparison with v05',
            needs_review=1,touched=touched))
    event_rows=[]
    for rid in sorted(completed):
        own=[r for r in flows if r['run_id']==rid]
        cfg=json.loads((ROOT/'runs'/rid/'config.json').read_text(encoding='utf8'))
        equity={r['date']:float(r['equity']) for r in read_csv(ROOT/'runs'/rid/'daily_equity.csv')}
        for w in watched:
            matching=[r for r in own if r['security_id']==w['security_id'] and r['date']==w['date']]
            touched=[d for d in w['touched'] if d>=cfg['start']] if cfg['mode'] != 'pool' else []
            if not matching and not touched:
                continue
            gross=sum(float(r['gross'] or 0) for r in matching)
            net=sum(float(r['net'] or 0) for r in matching)
            methods=','.join(sorted({r['method'] for r in matching}))
            meta={k:v for k,v in w.items() if k!='touched'}
            event_rows.append(dict(run_id=rid,**meta,scoring_weeks=len(touched),
                                   first_scoring_day=touched[0] if touched else '',
                                   last_scoring_day=touched[-1] if touched else '',
                                   held=bool(matching),cash_gross=gross,cash_net=net,
                                   cash_fraction_equity=net/equity[w['date']] if w['date'] in equity else '',
                                   actual_valuation=methods,entitlement='before ex-date open',
                                   value_known='event close' if w['method'] in ('spinoff','class_distribution') else 'declared amount',
                                   cash_available='next session' if w['method'] in ('spinoff','class_distribution') else 'ex-date open'))
    headers=['run_id','security_id','date','method','source','needs_review','scoring_weeks','first_scoring_day',
             'last_scoring_day','held','cash_gross','cash_net','cash_fraction_equity','actual_valuation',
             'entitlement','value_known','cash_available']
    write_csv(ROOT/'event_impact.csv',headers,event_rows)
    lines=['# TERRA_R1', '', '已看过的开发段修复确认，不是独立验证。', '', '|运行|状态|原因|','|---|---|---|']
    lines.extend(f"|{r['id']}|{r['status']}|{r.get('reason','')}|" for r in manifest['runs'])
    if summaries:
        lines+=['','|运行|净CAGR|VOO CAGR|全期超额|A超额|B超额|最大回撤|最大单股|前五公司峰值|',
                '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
        lines += [f"|{r['run_id']}|{r['cagr']:.2%}|{r['voo_cagr']:.2%}|{r['excess']:.2%}|{r['a_excess']:.2%}|{r['b_excess']:.2%}|{r['max_drawdown']:.2%}|{r['max_single_weight']:.2%}|{r['max_top5_issuer_weight']:.2%}|" for r in summaries]
    if comparisons_out:
        lines+=['','|时段|配对|差异 pp|95%区间 pp|99%边际区间 pp|','|---|---|---:|---:|---:|']
        lines += [f"|{r['period']}|{r['comparison']}|{r['point_pp']:.3f}|[{r['ci95_low_pp']:.3f}, {r['ci95_high_pp']:.3f}]|[{r['ci99_low_pp']:.3f}, {r['ci99_high_pp']:.3f}]|" for r in comparisons_out]
    if all_summaries:
        lines+=['','|10bp基础路径|最大回撤|峰 / 谷 / 恢复|最长水下交易日 / 日历日|2020 / 2022 回撤|最差完整年|',
                '|---|---:|---|---:|---:|---|']
        for rid,s in all_summaries.items():
            if not rid.endswith('_10bp'):
                continue
            for label, m in ((rid,s['strategy']), (rid+' VOO',s['benchmark'])):
                worst=m.get('worst_complete_year',{})
                lines.append(f"|{label}|{m['max_drawdown']:.2%}|{m['peak_date']} / {m['trough_date']} / {m['recovery_date'] or '未恢复'}|{m['longest_underwater_sessions']} / {m['longest_underwater_calendar_days']}|{m['max_drawdown_2020']:.2%} / {m['max_drawdown_2022']:.2%}|{worst.get('year','')} {worst.get('return_',0):.2%}|")
        lines+=['','|10bp基础路径|平均持股数|平均账户重合数量|平均单股 / 峰值|前五公司平均 / 峰值|平均 HHI|平均仓位|单边年换手|总成本|',
                '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
        for rid,s in all_summaries.items():
            if rid.endswith('_10bp'):
                lines.append(f"|{rid}|{s['average_positions']:.2f}|{s['average_positions_sum']-s['average_positions']:.2f}|{s['average_single_weight']:.2%} / {s['max_single_weight']:.2%}|{s['average_top5_issuer_weight']:.2%} / {s['max_top5_issuer_weight']:.2%}|{s['average_hhi']:.4f}|{s['average_invested_ratio']:.2%}|{s['turnover_one_way_annual']:.2%}|{s['total_execution_cost']:.2f}|")
        lines+=['','年度回报、25% / 35% / 40%权重日期比例、基金公司金额暴露及全部起点的完整指标见各运行 summary.json。2014 和 2024 为部分年。']
    lines += ['', '区间条件于已实现路径，未校正全部起点、时段、成本或历史搜索；不是保留段验证。',
              '结构基线与主配置的检查频率及整股约束不同，不是单变量配对。',
              '公司行动代理与 needs_review 事项须结合 event_impact.csv 审阅；旧随机结果不适用于修复后路径。',
              '完整风险指标、逐年收益与持仓明细保存在各运行目录。']
    lines += ['', '过程偏差：准备阶段读取既有 TRI 验证摘要时，工具意外返回了截止日之后的若干单日差异；这些值未用于选参数或评价本轮策略。']
    (ROOT/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    (ROOT/'handoff.md').write_text(f'# TERRA_R1 交接\n\n登记 {REGISTRATION}；实现 {commit}。\n\n复现：先将 PYTHONDONTWRITEBYTECODE=1、TMPDIR/TMP/TEMP 指向 work/terra_r1/tmp，再运行 `python -B -m leadtrend_bt.terra_r1`。\n\n默认测试：{manifest["tests"]}\n\n准备阶段误读了既有验证摘要中截止日之后的若干单日差异；没有用于参数选择或策略评价。请 reviewer 对此过程偏差裁决。\n\nGoogle 与 DowDuPont 的来源和事件解释见 config/terra_r1_events_v1.json；其他分拆保留标记的代理。ACE 的原始旧代码请求失败，未把另一公司的 CB 行情套用。\n\n详细状态与证据见 manifest.json、report.md 和各运行目录；事件代理与标记为 needs_review 的事项仍需 reviewer 审阅。\n',encoding='utf8')

if __name__ == '__main__':
    main()
