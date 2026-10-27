"""TERRA_R1 cash follow-up: isolate decisions before rerunning affected paths."""
from __future__ import annotations
import bisect
import csv
import hashlib
import json
import subprocess
from collections import defaultdict
from pathlib import Path

from leadtrend_db import api
from .engine import Backtest, Tranche, DataGapError
from . import metrics
from .terra_r1 import ROOT, DB, SPEC, EVENTS, IDS, SEED, sha, json_write, read_csv, write_csv, config, market_snapshot, save_result, weekly_paths, bootstrap

CASH = Path('config/terra_r1_cash_v1.json')
ADDENDUM = Path('research/bt_v3/TERRA_R1/cash_correction_v1.json')
SOURCE = ROOT/'cash_source_manifest.json'
ISOLATION = ROOT/'cash_impact_isolation.csv'
ISOLATION_META = ROOT/'cash_impact_isolation_meta.json'
SOURCE_ENGINE_COMMIT = '3b4ffbe1184efce6d5958e97906c9b3af1892a4c'
EXPECTED_CORE_DIFF = 'a15165cac49742a164f177464e1c0163973b88d1ae13f6ce1f5c2d1bdbe60cab'


def decision_difference(old, new):
    """Compare decisions in order, including the exact buy order and retained names."""
    expected = [(x['security_id'], x['action']) for x in old]
    actual = [(sid, action) for action, key in (('keep','keeps'),('sell','sells'),('buy','buys')) for sid in new[key]]
    return None if expected == actual else {'previous':expected, 'corrected':actual}


def is_cash_exposed(run_dir, corrections):
    for row in read_csv(run_dir/'corporate_actions.csv'):
        if (row['security_id'],row['date']) in corrections and row['method'] in ('cash_dividend','special_dividend'):
            return row
    return None


def isolate_one(cfg, market, rankings, corrections):
    out=ROOT/'runs'/cfg['run_id']
    exposure=is_cash_exposed(out,corrections)
    if cfg['mode']=='pool' or cfg['ranking']=='size':
        return dict(run_id=cfg['run_id'], status='rerun' if exposure else 'reuse', checks=0,
                    first_difference=exposure['date'] if exposure else '',
                    reason=('changed entitlement '+exposure['security_id'] if exposure else
                            'ranking independent of TRI values; date presence and positivity preserved; no changed cash entitlement'))
    bt=Backtest(cfg,db_path=DB,market=market)
    bt.issuer=market['issuer']; bt.full_calendar=market['full_calendar']; bt.tri=market['tri']
    bt._scored=lambda day, pool: rankings[day]
    days=[d for d in market['signal_days'] if d>=cfg['start']]
    checks=[(d,j) for i,d in enumerate(days) for j in range(cfg['K']) if i>=j and (i-j)%cfg['K']==0]
    wanted={d for d,_ in checks}
    positions=defaultdict(dict)
    with (out/'positions.csv').open(encoding='utf8',newline='') as f:
        for row in csv.DictReader(f):
            if row['date'] in wanted:
                positions[(row['date'],int(row['tranche']))][row['security_id']]=float(row['shares'])
    decisions=defaultdict(list)
    with (out/'weekly_decisions.csv').open(encoding='utf8',newline='') as f:
        for row in csv.DictReader(f): decisions[(row['signal_date'],int(row['tranche']))].append(row)
    first=None
    for n,(day,j) in enumerate(checks,1):
        t=Tranche(j,0,positions=positions[(day,j)])
        diff=decision_difference(decisions[(day,j)],bt._momentum_orders(t,day,market['pools'][day]))
        if diff:
            first=(day,j,diff);break
    if first:
        return dict(run_id=cfg['run_id'],status='rerun',checks=n,first_difference=first[0],
                    reason='corrected check differs tranche '+str(first[1])+' '+json.dumps(first[2],ensure_ascii=False))
    if exposure:
        return dict(run_id=cfg['run_id'],status='rerun',checks=len(checks),first_difference=exposure['date'],
                    reason='all decisions match but changed cash entitlement '+exposure['security_id'])
    return dict(run_id=cfg['run_id'],status='reuse',checks=len(checks),first_difference='',
                reason='every keep, sell and ordered buy matches; no corrected cash entitlement; same prices and costs')


def update_concentration(out, issuer, links):
    path=out/'summary.json'; summary=json.loads(path.read_text(encoding='utf8'))
    equity={r['date']:float(r['equity']) for r in read_csv(out/'daily_equity.csv')}
    linkmap={r['predecessor_issuer_id']:(r['successor_issuer_id'],r['effective_date']) for r in links}
    grouped=defaultdict(lambda:defaultdict(float))
    for row in read_csv(out/'positions.csv'):
        day=row['date']; current=issuer.get(row['security_id'],row['security_id']); seen=set()
        while current in linkmap and current not in seen and linkmap[current][1]<=day:
            seen.add(current);current=linkmap[current][0]
        grouped[day][current]+=float(row['value'])
    hhi=[];top5=[]
    for d,eq in equity.items():
        values=list(grouped[d].values())
        hhi.append(sum((v/eq)**2 for v in values))
        top5.append(sum(sorted(values,reverse=True)[:5])/eq)
    old=summary['average_hhi']; summary['average_hhi']=sum(hhi)/len(hhi);summary['max_hhi']=max(hhi)
    summary['average_top5_issuer_weight']=sum(top5)/len(top5);summary['max_top5_issuer_weight']=max(top5)
    json_write(path,summary)
    return old,summary['average_hhi']


def event_report(manifest, market, cash):
    calendar=market['full_calendar']; signals=market['signal_days']; touched={}
    all_events=[]
    for entry in cash['entries']:
        all_events.append(dict(security_id=entry['security_id'],date=entry['ex_date'],kind='cash_candidate',
          source=entry['source_url'], old_cash=entry['database_cash'],new_cash=entry['official_cash'],
          status=entry['status'], note=entry['evidence_note']))
    for entry in cash['supported_needs_review']:
        all_events.append(dict(security_id=entry['security_id'],date=entry['ex_date'],kind='cash_previously_reviewed',
          source=entry['source_url'],old_cash=entry['official_cash'],new_cash=entry['official_cash'],
          status='supported_unchanged',note=entry['decision']))
    for sid, rows in market['actions'].items():
        for e in rows:
            if e['action_type'] in ('spinoff','class_distribution'):
                all_events.append(dict(security_id=sid,date=e['action_date'],kind=e['action_type'],
                    source=e.get('source_url') or e.get('source_id',''),old_cash='',new_cash='',
                    status=e.get('resolution',''),note='valuation and liquidity per event ledger'))
    reference=[('CUSIP:025537101','2020-02-07'),('CUSIP:025537101','2020-05-07'),('CUSIP:025537101','2020-08-07'),
        ('CUSIP:09260D107','2022-07-29'),('CUSIP:12572Q105','2021-12-27'),('CUSIP:20825C104','2023-05-15'),
        ('CUSIP:24703L202','2021-11-02'),('CUSIP:26614N201','2019-06-03'),('CUSIP:30161N101','2019-08-14'),
        ('CUSIP:369604301','2023-01-04'),('CUSIP:438516205','2020-05-14'),('CUSIP:438516205','2020-08-13'),
        ('CUSIP:743315103','2021-12-17')]
    corrected={(x['security_id'],x['ex_date']) for x in cash['entries'] if x['status']=='corrected'}
    corrected.update({('CUSIP:26614N201','2019-06-03'),('CUSIP:369604301','2023-01-04')})
    for sid,day in reference:
        all_events.append(dict(security_id=sid,date=day,kind='tri_reference_discrepancy',
            source='work/bt_v1/tri_validation.json: development-period comparison',old_cash='',new_cash='',
            status='corrected_event' if (sid,day) in corrected else 'unresolved_comparison',
            note='comparison is a diagnostic; it does not itself identify the correct series'))
    for e in all_events:
        sid,day=e['security_id'],e['date'];i=bisect.bisect_left(calendar,day);series=market['tri'].get(sid,{})
        hit=[]
        for s in signals:
            j=bisect.bisect_left(calendar,s)
            if j>=90 and j-90<i<=j and any(x['security_id']==sid for x in market['pools'].get(s,{}).get('securities',[])) and all(series.get(x,0)>0 for x in calendar[j-90:j+1]):hit.append(s)
        touched[(sid,day)]=hit
    result=[]
    for item in manifest['runs']:
        if not item['status'].startswith('complete'):continue
        rid=item['id'];out=ROOT/'runs'/rid;cfg=json.loads((out/'config.json').read_text(encoding='utf8'))
        flows=defaultdict(list)
        for r in read_csv(out/'corporate_actions.csv'):flows[(r['security_id'],r['date'])].append(r)
        eq={r['date']:float(r['equity']) for r in read_csv(out/'daily_equity.csv')}
        for event in all_events:
            key=(event['security_id'],event['date']); f=flows[key]
            weeks=[d for d in touched[key] if d>=cfg['start']] if cfg['mode']!='pool' else []
            if event['kind'] not in ('cash_candidate','cash_previously_reviewed','tri_reference_discrepancy') and not f and not weeks:continue
            gross=sum(float(x['gross'] or 0) for x in f); net=sum(float(x['net'] or 0) for x in f)
            result.append(dict(run_id=rid,**event,held=bool(f),scoring_weeks=len(weeks),
                first_scoring=weeks[0] if weeks else '',last_scoring=weeks[-1] if weeks else '',
                actual_gross=gross,actual_net=net,fraction_equity=net/eq[event['date']] if event['date'] in eq else '',
                valuation=','.join(sorted({r['method'] for r in f})),
                value_known='ex-date open' if event['kind'].startswith('cash') else 'event close' if event['kind'] in ('spinoff','class_distribution') else '',
                cash_available='ex-date open' if event['kind'].startswith('cash') else 'next session open' if event['kind'] in ('spinoff','class_distribution') else ''))
    header=['run_id','security_id','date','kind','source','old_cash','new_cash','status','note','held',
            'scoring_weeks','first_scoring','last_scoring','actual_gross','actual_net','fraction_equity',
            'valuation','value_known','cash_available']
    write_csv(ROOT/'event_impact.csv',header,result)


def publish(manifest, market, cash):
    completed=[i['id'] for i in manifest['runs'] if i['status'].startswith('complete')]
    summaries=[]; detailed={}
    for rid in sorted(completed):
        s=json.loads((ROOT/'runs'/rid/'summary.json').read_text(encoding='utf8'));detailed[rid]=s
        summaries.append(dict(run_id=rid,cagr=s['strategy']['cagr'],voo_cagr=s['benchmark']['cagr'],
          excess=s['excess_cagr'],a_excess=s['periods']['A']['excess'],b_excess=s['periods']['B']['excess'],
          max_drawdown=s['strategy']['max_drawdown'],max_single_weight=s['max_single_weight'],
          max_top5_issuer_weight=s['max_top5_issuer_weight'],average_hhi=s['average_hhi'],
          fees=s['fees'],total_execution_cost=s['total_execution_cost'],turnover=s['turnover_one_way_annual']))
    if summaries:write_csv(ROOT/'summary.csv',list(summaries[0]),summaries)
    pairs=[]
    if all(n+'_10bp' in completed for n in IDS[:4]):
        if manifest['paired_inputs_changed']:
            weeks=weekly_paths();write_csv(ROOT/'weekly_returns.csv',list(weeks[0]),weeks)
            pairs=bootstrap(weeks);write_csv(ROOT/'paired_comparisons.csv',list(pairs[0]),pairs)
        else:
            pairs=read_csv(ROOT/'paired_comparisons.csv')
    event_report(manifest,market,cash)
    lines=['# TERRA_R1：现金事件续修','','本轮是开发段上的修复确认，非独立样本验证。',
           '', '|运行|状态|现金修正影响|','|---|---|---|']
    lines += [f"|{x['id']}|{x['status']}|{x.get('reason','')}|" for x in manifest['runs']]
    lines+=['','|路径|策略CAGR|VOO CAGR|全期超额|A超额|B超额|最大回撤|单股峰值|公司HHI均值|',
            '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    lines += [f"|{r['run_id']}|{r['cagr']:.2%}|{r['voo_cagr']:.2%}|{r['excess']:.2%}|{r['a_excess']:.2%}|{r['b_excess']:.2%}|{r['max_drawdown']:.2%}|{r['max_single_weight']:.2%}|{r['average_hhi']:.5f}|" for r in summaries]
    if pairs:
        lines+=['','|时段|配对|差异 pp|95%区间 pp|99%边际区间 pp|','|---|---|---:|---:|---:|']
        lines += [f"|{r['period']}|{r['comparison']}|{float(r['point_pp']):.3f}|[{float(r['ci95_low_pp']):.3f}, {float(r['ci95_high_pp']):.3f}]|[{float(r['ci99_low_pp']):.3f}, {float(r['ci99_high_pp']):.3f}]|" for r in pairs]
        for r in pairs:
            if r['period']=='all':
                low,high=float(r['ci95_low_pp']),float(r['ci95_high_pp'])
                low99,high99=float(r['ci99_low_pp']),float(r['ci99_high_pp'])
                lines.append(f"\n全期 {r['comparison']} 为 {float(r['point_pp']):+.2f} pp；95%区间{'跨零' if low<=0<=high else '未跨零'}，调整后99%边际区间{'跨零' if low99<=0<=high99 else '未跨零'}。")
    lines+=['','|10bp基础路径|最大回撤|峰 / 谷 / 恢复|最长水下交易日 / 日历日|2020 / 2022回撤|最差完整年|',
            '|---|---:|---|---:|---:|---|']
    for rid,s in detailed.items():
        if not rid.endswith('_10bp'):continue
        for label,m in ((rid,s['strategy']),(rid+' VOO',s['benchmark'])):
            w=m.get('worst_complete_year',{})
            lines.append(f"|{label}|{m['max_drawdown']:.2%}|{m['peak_date']} / {m['trough_date']} / {m['recovery_date'] or '未恢复'}|{m['longest_underwater_sessions']} / {m['longest_underwater_calendar_days']}|{m['max_drawdown_2020']:.2%} / {m['max_drawdown_2022']:.2%}|{w.get('year','')} {w.get('return_',0):.2%}|")
    lines+=['','成本、每日持仓、交易、逐年收益、集中度和每个延迟起点均保存在各运行目录。2014 与 2024 为部分年。',
       '26 条候选中 24 条修正、2 条金额不变；Blackstone 2023 年两笔经官方来源支持而保留。详见现金配置和事件影响表。',
       '配对置信区间条件于已实现开发段路径，不校正所有时段、起点、成本与历史搜索；结构基线不是单变量配对。',
       '原始数据验证摘要在准备阶段意外返回截止日后的单日差异；GEHC 文件核对脚本遍历过截止日后的行情行，后续行未显示或计算。两次是访问偏差，并非一次保留段策略检验。',
       '历史主体经济连续性只用于公司集中度统计，交易使用历史证券身份。ACE 两项维持缺价阻断。']
    (ROOT/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    (ROOT/'handoff.md').write_text('# TERRA_R1 现金续修交接\n\n'+
      f"登记提交 {manifest['cash_registration_commit']}；实现提交 {manifest['implementation_commit']}。\n\n"+
      '复现：设置 PYTHONDONTWRITEBYTECODE=1，TMPDIR/TMP/TEMP 指向 work/terra_r1/tmp，运行 `python -B -m leadtrend_bt.terra_r1`。该命令先验证保存的隔离和输入指纹；缓存有效时不重跑。\n\n'+
      '原因：actions_v2.finalize.rehab 对同日不完全相同的归档记录直接相加并复合调整因子，可能把供应商修订重复算作多笔；后续以由相同记录导出的 implied cash 挑选候选，会循环确认错误金额。正式库后续应在 leadtrend_db/actions_v2.py 的 rehab 合并与 finalize 现金选择处按经济事件及官方依据修复，本轮未修改正式库。\n\n'+
      '两次截止日后访问偏差按 reviewer 技术裁决记录；没有运行保留段策略或重设授权。\n\n'+
      '详细逐条证据、路径隔离与指标见 config/terra_r1_cash_v1.json、cash_impact_isolation.csv、manifest.json、event_impact.csv、report.md。\n',encoding='utf8')


def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    dirty=subprocess.check_output(['git','status','--porcelain','--','leadtrend_bt','leadtrend_db/api.py','tests_bt',str(SPEC),str(EVENTS),str(CASH),str(ADDENDUM)],text=True)
    if dirty:raise RuntimeError('implementation, tests or registered inputs have uncommitted changes')
    cash=json.loads(CASH.read_text(encoding='utf8')); cashhash=sha(CASH); add=json.loads(ADDENDUM.read_text(encoding='utf8'))
    if cashhash!=add['cash_config_sha256']:raise RuntimeError('cash correction does not match addendum')
    if not SOURCE.exists():
        SOURCE.write_bytes((ROOT/'manifest.json').read_bytes())
        for filename in ('paired_comparisons.csv','summary.csv'):
            src=ROOT/filename
            if src.exists():(ROOT/('cash_source_'+filename)).write_bytes(src.read_bytes())
    source=json.loads(SOURCE.read_text(encoding='utf8'))
    if source['implementation_commit'] != SOURCE_ENGINE_COMMIT:
        raise RuntimeError('unexpected source implementation; path equivalence must be reassessed')
    core_diff=subprocess.check_output(['git','diff',SOURCE_ENGINE_COMMIT,commit,'--','leadtrend_bt/engine.py',
                                       'leadtrend_bt/metrics.py','leadtrend_db/api.py'])
    if hashlib.sha256(core_diff).hexdigest()!=EXPECTED_CORE_DIFF:
        raise RuntimeError('core changed beyond the reviewed cash and concentration correction')
    if source['database_content_sha256']!='97b55e453e09b5e40570ab5ab9ae9d6e317131ab448c9b11ec719cc3d06bde6f':raise RuntimeError('unexpected database')
    if source['database_file_sha256']!='7564971e17d828289fdf46cb04413fce3d1ec60faac5461d5376774ce9751e87':raise RuntimeError('source database fingerprint mismatch')
    dbhash=sha(DB)
    if dbhash!=source['database_file_sha256']:raise RuntimeError('database file differs from source fingerprint')
    dbmeta=dict(api.connect(DB).execute('select key,value from meta'))
    if dbmeta.get('content_sha256')!=source['database_content_sha256']:raise RuntimeError('database content metadata differs')
    signature=hashlib.sha256(f"{commit}|{source['fingerprint']}|{cashhash}|{sha(EVENTS)}|{sha(SPEC)}|{dbhash}".encode()).hexdigest()
    sigdays=api.friday_signal_days('2014-02-28','2014-04-01',DB)
    loader=Backtest(config('MOM_EQ',10,sigdays[0]),db_path=DB);loader.load();market=market_snapshot(loader)
    poolloader=Backtest(config('TOP10_AMT',10,sigdays[0]),db_path=DB);poolloader.load();poolmarket=market_snapshot(poolloader)
    issuer=dict(poolmarket['issuer']);issuer.update(market['issuer']);links=api.issuer_continuity(DB)
    corrections={(x['security_id'],x['ex_date']) for x in cash['entries'] if x['status']=='corrected'}
    meta={'fingerprint':signature,'source_fingerprint':source['fingerprint'],'cash_sha256':cashhash}
    if ISOLATION.exists() and ISOLATION_META.exists() and json.loads(ISOLATION_META.read_text(encoding='utf8'))==meta:
        isolation=read_csv(ISOLATION)
    else:
        rankings={d:loader._scored(d,market['pools'][d]) for d in market['signal_days']}
        isolation=[]
        for item in source['runs']:
            if not item['status'].startswith('complete'):continue
            cfg=json.loads((ROOT/'runs'/item['id']/'config.json').read_text(encoding='utf8'))
            row=isolate_one(cfg,market,rankings,corrections);isolation.append(row)
            print('isolation',row['run_id'],row['status'],row['first_difference'],flush=True)
        write_csv(ISOLATION,['run_id','status','checks','first_difference','reason'],isolation);json_write(ISOLATION_META,meta)
    byid={r['run_id']:r for r in isolation}; runs=[]
    manifest=dict(protocol='terra_r1_v1_cash_v1',registration_commit='6dc6979',cash_registration_commit='4c6ca0a',
        implementation_commit=commit,source_implementation_commit=source['implementation_commit'],
        database_content_sha256=source['database_content_sha256'],database_file_sha256=source['database_file_sha256'],
        original_events_sha256=sha(EVENTS),cash_correction_sha256=cashhash,spec_sha256=sha(SPEC),fingerprint=signature,
        seed=SEED,tests=(ROOT/'test_result.txt').read_text(encoding='utf8'),run_command='python -B -m leadtrend_bt.terra_r1',
        equity_paths_changed=any(r['status']=='rerun' for r in isolation),
        paired_inputs_changed=any(r['status']=='rerun' and r['run_id'] in {n+'_10bp' for n in IDS[:4]}
                                  for r in isolation),runs=runs)
    baseline_hhi=ROOT/'cash_hhi_before.csv'
    if not baseline_hhi.exists():
        before=[]
        for item in source['runs']:
            if item['status'].startswith('complete'):
                old_summary=json.loads((ROOT/'runs'/item['id']/'summary.json').read_text(encoding='utf8'))
                before.append(dict(run_id=item['id'],average_hhi=old_summary['average_hhi']))
        write_csv(baseline_hhi,['run_id','average_hhi'],before)
    original_hhi={r['run_id']:float(r['average_hhi']) for r in read_csv(baseline_hhi)}
    for old in source['runs']:
        rid=old['id'];out=ROOT/'runs'/rid
        if not old['status'].startswith('complete'):
            runs.append(dict(old,source_fingerprint=source['fingerprint']));continue
        iso=byid[rid]; cfg=json.loads((out/'config.json').read_text(encoding='utf8'));marker=out/'complete.json'
        if iso['status']=='rerun':
            if marker.exists() and json.loads(marker.read_text(encoding='utf8')).get('fingerprint')==signature:
                item=dict(id=rid,status='complete_cached',reason=iso['reason'])
            else:
                bt=Backtest(cfg,db_path=DB,market=poolmarket if cfg['mode']=='pool' else market)
                try:
                    result=bt.run();result['continuity_links']=links;save_result(bt,result,cfg,signature)
                    item=dict(id=rid,status='complete_rerun',reason=iso['reason'])
                except Exception as e:
                    item=dict(id=rid,status='blocked_missing_prices' if isinstance(e,DataGapError) else 'failed',reason=str(e))
                    json_write(out/'cash_failure.json',dict(error=str(e),fingerprint=signature,config=cfg))
                print('rerun',rid,item['status'],flush=True)
        else:
            old_hhi,new_hhi=update_concentration(out,issuer,links)
            item=dict(id=rid,status='complete_reused',reason=iso['reason'],source_fingerprint=old.get('source_fingerprint',source['fingerprint']),
                      source_path_commit=old.get('source_commit',source['implementation_commit']),
                      average_hhi_before=original_hhi[rid],average_hhi_after=new_hhi)
        runs.append(item);json_write(ROOT/'manifest.json',manifest)
    publish(manifest,market,cash);json_write(ROOT/'manifest.json',manifest)

if __name__=='__main__':main()
