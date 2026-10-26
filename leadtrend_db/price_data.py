"""Offline reconstruction of original OpenD prices and actions, with identity and coverage evidence.
No adjusted series, signal, portfolio return or backtest is calculated here.
"""
from __future__ import annotations
import bisect,csv,io,json,math
from collections import defaultdict
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from .sources import ROOT,RAW_DIR,sha256_bytes
from .identity import load_overrides

CONFIG=ROOT/'config/db_prices_v1.json'
CALENDAR_CONFIG=ROOT/'config/db_nyse_calendar_v1.json'
COLS={
 'prices_daily':['security_id','session_date','source','open','high','low','close','adj_close','volume','turnover','source_id'],
 'corporate_actions':['action_id','security_id','action_date','action_type','ratio','cash_amount','needs_review','currency','source','source_id','notes'],
 'price_coverage':['security_id','source','first_date','last_date','n_days','status','source_id','detail'],
 'price_lineage':['security_id','opend_code','first_date','last_date','status','detail','source_id'],
 'price_quarter_coverage':['version_id','security_id','active_from','active_to','expected_sessions','present_sessions',
                          'warmup_expected','warmup_present','status','missing_dates','source_id'],
 'price_anomalies':['security_id','session_date','previous_date','close_ratio','detail','source_id'],
 'price_comparison':['security_id','session_date','differences','source_id'],
 'price_calendar':['session_date','session_type','source_id'],
 'price_continuity':['security_id','event_date','ratio','previous_close','current_close','status','detail','source_id'],
}

def finite(v):
    try:r=float(v);return r if math.isfinite(r) else None
    except (TypeError,ValueError):return None


def anchor_matches(close,amount,shares):
    if not shares or not close:return False
    expected=amount/shares
    return abs(close-expected)<=max(.03,1000/shares,expected*.005)


def active_start(utc,calendar):
    # OpenD US calendar dates; normal NYSE open 09:30 ET. Do not use a same-day close known before publication.
    from zoneinfo import ZoneInfo
    dt=datetime.fromisoformat(utc.replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York'))
    day=dt.date().isoformat();i=bisect.bisect_left(calendar,day)
    if i<len(calendar) and calendar[i]==day and (dt.hour,dt.minute,dt.second)>=(9,30,0):i+=1
    return calendar[i] if i<len(calendar) else ''


def assemble(t,store,build):
    if not CONFIG.exists():return {}
    from leadtrend_v05.opend_prices import (normalize_rehab_rows,normalize_dividend_rows,verify_bny_dividend_identity,
                                           parse_dividend_statement)
    cfg=json.loads(CONFIG.read_text(encoding='utf8'));index=json.loads(store._read(cfg['index_url']))
    start,end=cfg['start'],cfg['end'];out={k:[] for k in COLS};reviews=[]
    sid_of=lambda u:'SRC-'+store.records[u]['sha256'][:16]
    def payload(u):return json.loads(store._read(u))
    def review(kind,subject,detail,source=None):reviews.append(dict(review_type=kind,subject_id=subject,snapshot_id='',detail=detail,source_id=source))
    calendar={}
    for u in index['calendar']:
        p=payload(u)
        for r in p['data']:
            d=(r.get('time') or r.get('trade_date') or '')[:10]
            if start<=d<=end:calendar[d]=(r.get('trade_date_type',''),sid_of(u))
    calendar_config=CALENDAR_CONFIG
    if calendar_config.exists():
        supplement=json.loads(calendar_config.read_text(encoding='utf8'))
        evidence=supplement['evidence_urls']
        # Read every primary source from the archive during each offline rebuild.
        for url in evidence:store._read(url)
        d=date.fromisoformat(supplement['first_date']);last=date.fromisoformat(supplement['last_date'])
        closed=set(supplement['closed_dates']);half=set(supplement['half_days'])
        while d<=last:
            text=d.isoformat()
            if d.weekday()<5 and text not in closed and start<=text<=end:
                src=evidence[0] if d.year==2012 else evidence[1] if d.year==2013 else evidence[2] if d.year<=2015 else evidence[3]
                calendar[text]=('HALF' if text in half else 'WHOLE',sid_of(src))
            d+=timedelta(days=1)
    dates=sorted(calendar)
    if not dates:raise ValueError('empty archived US trading calendar')
    out['price_calendar']=[dict(session_date=d,session_type=calendar[d][0],source_id=calendar[d][1]) for d in dates]
    o=load_overrides(ROOT/'config/db_overrides_v1.csv')
    issuer_start={r['key']:r['value'] for r in o if r['override_kind']=='issuer_start'}
    parent={r['successor_issuer_id'][4:]:r['predecessor_issuer_id'][4:] for r in t['issuer_links']}
    securities={r['security_id']:r for r in t['securities']}
    selected=defaultdict(set)
    for r in t['strategy_securities']:
        if r['strategy_eligible']:selected[r['current_ticker']].update([r['security_id'],r['current_security_id']])
    # The only alias-only class in the pool converted one-for-one on this evidenced date.
    aliases={'CMCSA':[('CMCSK',start,'2015-12-10')],'JCI':[('TYC',start,'2016-09-02')],
             'BNY':[('BK',start,'2026-05-20')],'CB':[('ACE',start,'2016-01-14')]}
    frames={};frame_sources={};action_raw={};action_sources={}
    for code,entry in sorted(index['entries'].items()):
        bars={};sources={}
        for u in sorted(set(entry.get('reused',[]))):
            for r in csv.DictReader(io.StringIO(store._read(u).decode('utf-8-sig'))):
                d=r['session_date']
                if start<=d<=end:bars[d]={k:finite(r.get(k)) for k in ['open','high','low','close','volume','turnover']};sources[d]=sid_of(u)
        for u in entry.get('klines',[]):
            p=payload(u)
            if p['ret']!=0:continue
            if p.get('params',{}).get('autype') not in ('NONE','None'):raise ValueError('refusing non-NONE raw prices')
            for r in p['data']:
                d=str(r.get('time_key',''))[:10]
                if start<=d<=end:bars[d]={k:finite(r.get(k)) for k in ['open','high','low','close','volume','turnover']};sources[d]=sid_of(u)
        frames[code]=bars;frame_sources[code]=sources
        actions=entry.get('actions',{});rr=[];dd=[];srcs={}
        for u in entry.get('reused_actions',[]):
            path=store.records[u]['path'];raw=store._read(u).decode('utf-8-sig')
            if 'rehab_' in path:rr+=[dict(x,_source_id=sid_of(u)) for x in csv.DictReader(io.StringIO(raw))];srcs['rehab']=sid_of(u)
            elif 'dividends_' in path:dd+=[dict(x,_source_id=sid_of(u)) for x in json.loads(raw).get('dividend_list',[])];srcs['dividends']=sid_of(u)
        for kind,u in actions.items():
            p=payload(u)
            if p['ret']!=0:review('price_action_request',code,p['error'],sid_of(u));continue
            if kind=='rehab':rr+=[dict(x,_source_id=sid_of(u)) for x in p['data']]
            elif kind=='dividends':dd+=[dict(x,_source_id=sid_of(u)) for x in p['data'].get('dividend_list',[])]
            srcs[kind]=sid_of(u)
        # Keep a provenance source for every individual record; identical repeated observations collapse.
        action_raw[code]=(rr,dd);action_sources[code]=srcs
    periods={r['version_id']:r for r in t['pool_versions']}
    holdings=defaultdict(list)
    for h in t['holdings']:
        if h.get('security_id') and h.get('issuer_id') and h['shares'] and h['value_usd']>0:
            p=periods[h['snapshot_id']]['report_period']
            d=dates[bisect.bisect_right(dates,p)-1] if p>=dates[0] else ''
            holdings[h['security_id']].append((d,h['value_usd'],h['shares'],h['issuer_id']))
    all_prices={};all_actions={};lineages={}
    for code,sids in sorted(selected.items()):
        entry=index['entries'].get(code,{})
        # Follow only retained single-company legal continuations to the earliest company.
        current_sid=next(r['current_security_id'] for r in t['strategy_securities'] if r['strategy_eligible'] and r['current_ticker']==code)
        root=securities[current_sid]['issuer_id'][4:];visited=set()
        while root in parent and root not in visited:visited.add(root);root=parent[root]
        floor=max(start,issuer_start.get(root,start))
        if code=='GOOG':floor=max(floor,'2014-04-03')
        changes={start,floor}
        if code=='JCI':changes.add('2016-09-06')
        if code=='CB':changes.add('2016-01-15')
        for r in o:
            if r['override_kind']=='link_rejected':
                a,b=r['key'].split('>')
                if b==securities[current_sid]['issuer_id'][4:]:changes.add(r['field'])
        for old,first,last in aliases.get(code,[]):changes.add(first);changes.add((date.fromisoformat(last)+timedelta(days=1)).isoformat())
        for r in t['issuer_links']:
            if r['predecessor_issuer_id'] in {securities[s]['issuer_id'] for s in sids} or r['successor_issuer_id']==securities[current_sid]['issuer_id']:changes.add(r['effective_date'])
        bounds=sorted(d for d in changes if start<=d<=end)+[(date.fromisoformat(end)+timedelta(days=1)).isoformat()]
        combined=dict(frames.get(code,{}));srcs=dict(frame_sources.get(code,{}));chosen={d:code for d in combined}
        for old,first,last in aliases.get(code,[]):
            for d,b in frames.get(old,{}).items():
                if first<=d<=last:combined[d]=b;srcs[d]=frame_sources[old][d];chosen[d]=old
        # Class C and Class A prices are never substituted. Special A is used only for its own historical security.
        for sid in sorted(sids):
            bars=dict(combined);sources=dict(srcs);which=dict(chosen)
            if code=='CMCSA' and securities[sid]['class_code']!='CL_A_SPECIAL':
                bars=dict(frames.get(code,{}));sources=dict(frame_sources.get(code,{}));which={d:code for d in bars}
            anchors=holdings.get(sid,[])
            if not anchors:anchors=[x for sk in sids for x in holdings.get(sk,[])]
            kept={};details=[]
            for lo,hi in zip(bounds,bounds[1:]):
                first=max(lo,floor);last=(date.fromisoformat(hi)-timedelta(days=1)).isoformat()
                if first>last:continue
                tests=[(d,v,sh,i) for d,v,sh,i in anchors if first<=d<=last and d in bars]
                bad=[(d,v/sh,bars[d]['close'],i) for d,v,sh,i in tests if not anchor_matches(bars[d]['close'],v,sh)]
                if bad:
                    detail='quarantined identity/price-basis segment; mismatched fund value/shares anchors='+json.dumps(bad,ensure_ascii=False)
                    review('price_identity',sid,f'{code} {first}..{last}: '+detail, sources.get(bad[0][0]))
                    status='quarantined'
                else:
                    status='fund_anchor_verified' if tests else 'no_fund_anchor_in_segment'
                    # A separate short segment can be covered by adjacent same-company prices; continuity events get an event-day check below.
                    detail=f'code={code};company_root=CIK:{root};root_start={floor};matching_anchors={len(tests)};historical/current projection kept explicit'
                    for d,b in bars.items():
                        if first<=d<=last and d in calendar:
                            if any(b.get(k) is None or b[k]<=0 for k in ['open','high','low','close']) or b['low']>min(b['open'],b['close'])+.001 or b['high']<max(b['open'],b['close'])-.001:
                                review('price_invalid_bar',sid,d,sources[d]);continue
                            kept[d]=(b,sources[d],which[d])
                details.append(dict(security_id=sid,opend_code='US.'+code,first_date=first,last_date=last,status=status,
                                    detail=detail,source_id=(sources.get(tests[0][0]) if tests else sid_of(cfg['index_url']))))
            # If a historical class differs from the projection, unverified current-class prices cannot cover its old era.
            if code=='CMCSA' and securities[sid]['class_code']=='CL_A_SPECIAL':
                for d in list(kept):
                    if d<'2015-12-11' and kept[d][2]!='CMCSK':del kept[d]
            known_reuse_start={'CB':'2016-01-15','JCI':'2016-09-06'}
            if code in known_reuse_start:
                cutoff=known_reuse_start[code]
                dropped=[d for d in kept if d<cutoff and kept[d][2]==code]
                for d in dropped:del kept[d]
                if dropped:review('price_code_reuse',sid,f'{code}: removed {len(dropped)} dates before {cutoff}; vendor ticker history belongs to the acquired company',sid_of(cfg['index_url']))
            all_prices[sid]=kept;lineages[sid]=details;out['price_lineage']+=details
            for d,(b,source,ticker) in sorted(kept.items()):
                out['prices_daily'].append(dict(security_id=sid,session_date=d,source='opend_none',**b,adj_close=None,source_id=source))
            gaps=[];rr,dd=action_raw.get(code,([],[]));rr=list({json.dumps(x,sort_keys=True,default=str):x for x in rr}.values())
            events=normalize_rehab_rows(rr,sid,'US.'+code,date.fromisoformat(floor),date.fromisoformat(end),gaps)
            cash=normalize_dividend_rows(dd,sid,'US.'+code,date.fromisoformat(floor),date.fromisoformat(end),gaps)
            if code=='BNY':cash=verify_bny_dividend_identity(cash,rr,sid,date.fromisoformat(floor),date.fromisoformat(end),gaps)
            # Rehab has explicit per-share cash even when the dividend endpoint omits older history or pay dates.
            # Use it only as a raw amount on its ex-date; no pay date is invented.
            known={r['effective_session'] for r in cash};ambiguous={}
            for r in rr:
                d=str(r.get('ex_div_date',''))[:10];amount=finite(r.get('per_cash_div'));special=finite(r.get('special_dividend'))
                if floor<=d<=end and amount and amount>0 and d not in known:
                    ambiguous.setdefault(d,set()).add(amount)
            for d,amounts in sorted(ambiguous.items()):
                if len(amounts)==1:cash.append(dict(event_type='cash_dividend',effective_session=d,shares_new_per_old='',
                    cash_amount_per_share=next(iter(amounts)),source='opend_get_rehab:per_cash_div;pay_date_unknown',pay_date=''))
                else:review('price_cash_ambiguity',sid,f'{d}: {sorted(amounts)}',action_sources.get(code,{}).get('rehab'))
            events+=cash
            for raw in rr:
                d=str(raw.get('ex_div_date',''))[:10];amount=finite(raw.get('special_dividend'))
                if floor<=d<=end and amount and amount>0:
                    # If endpoint already includes this exact special amount, classify that record rather than double count it.
                    duplicate=next((e for e in events if e['effective_session']==d and finite(e.get('cash_amount_per_share'))==amount),None)
                    if duplicate:
                        duplicate['event_type']='special_dividend'
                        duplicate['source']+=';special_classification_rehab_source='+raw['_source_id']
                    else:events.append(dict(event_type='special_dividend',effective_session=d,shares_new_per_old='',
                        cash_amount_per_share=amount,source='opend_get_rehab:special_dividend;pay_date_unknown',pay_date=''))
            ambiguous_dates={g['detail'].split('ex=')[1].split(';')[0] for g in gaps if g.get('message')=='distinct_cash_dividends_require_source_review' and 'ex=' in g.get('detail','')}
            unique={} 
            for r in events:
                d=r['effective_session'];typ=r['event_type'];ratio=finite(r['shares_new_per_old']);cash_amount=finite(r['cash_amount_per_share'])
                # Historical GOOG Class C distribution is not a doubling of retained Class A shares.
                if code=='GOOGL' and d=='2014-04-03' and typ=='split':typ='class_distribution';ratio=None
                src=action_sources.get(code,{}).get('dividends' if 'opend_dividends:' in r['source'] else 'rehab')
                if 'opend_dividends:' in r['source']:
                    provenance=[x['_source_id'] for x in dd if str(x.get('ex_date','')).replace('/','-')[:10]==d and parse_dividend_statement(x.get('statement',''))==cash_amount]
                else:provenance=[x['_source_id'] for x in rr if str(x.get('ex_div_date',''))[:10]==d]
                if provenance:src=provenance[-1]
                if not src:continue
                if code in ('CB','JCI') and d<{'CB':'2016-01-15','JCI':'2016-09-06'}[code]:continue
                if code=='CMCSA' and securities[sid]['class_code']=='CL_A_SPECIAL' and d<'2015-12-11':continue
                if any(lo['status']=='quarantined' and lo['first_date']<=d<=lo['last_date'] for lo in details):continue
                key=(d,typ,ratio,cash_amount)
                unique[key]=dict(security_id=sid,action_date=d,action_type=typ,ratio=ratio,cash_amount=cash_amount,
                                  needs_review=int(d in ambiguous_dates and cash_amount is not None),
                                  currency='USD',source='opend',source_id=src,notes=('needs_review=distinct_cash_items;do_not_sum_without_review;' if d in ambiguous_dates and cash_amount is not None else '')+r['source']+';pay_date='+r.get('pay_date',''))
            all_actions[sid]=list(unique.values())
            for gap in gaps:
                if gap.get('severity') in ('error','warning'):review('price_action_review',sid,gap['message']+': '+gap['detail'],sid_of(cfg['index_url']))
            ds=sorted(kept)
            out['price_coverage'].append(dict(security_id=sid,source='opend_none',first_date=ds[0] if ds else '',
                last_date=ds[-1] if ds else '',n_days=len(ds),status='available' if ds else 'missing',source_id=sid_of(cfg['index_url']),
                detail=json.dumps(dict(code=code,root='CIK:'+root,root_start=floor,request_error=entry.get('error',''),
                                       quarantined_segments=sum(x['status']=='quarantined' for x in details)),ensure_ascii=False)))
    for code,sids in sorted(selected.items()):
        issuer_ids={securities[s]['issuer_id'] for s in sids}
        events=[(r['effective_date'],1.0,'single-company SEC continuation') for r in t['issuer_links']
                if r['predecessor_issuer_id'] in issuer_ids or r['successor_issuer_id'] in issuer_ids]
        if code=='JCI':events.append(('2016-09-06',0.955,'Tyco same-CIK fixed share consolidation; SEC 8-K'))
        if code=='CB':events.append(('2016-01-15',1.0,'ACE to CB same-CIK ticker change'))
        if code=='BNY':events.append(('2026-05-21',1.0,'BK to BNY same-CIK ticker change'))
        if code=='CMCSA':events.append(('2015-12-11',1.0,'CMCSK to CMCSA class conversion'))
        for sid in sorted(sids):
            for ex,ratio,basis in sorted(set(events)):
                before=dates[bisect.bisect_left(dates,ex)-1] if ex>dates[0] else ''
                j=bisect.bisect_left(dates,ex);after=dates[j] if j<len(dates) else ''
                bars=all_prices[sid];a=bars.get(before);b=bars.get(after)
                prev=a[0]['close'] if a else None;now=b[0]['close'] if b else None
                actions=[x for x in all_actions[sid] if x['action_date']==after]
                unexplained=bool(prev and now and abs(now*ratio/prev-1)>.4 and not actions)
                status='pass' if prev and now and not unexplained else 'unverified'
                detail=basis+';date_before='+before+';date_after='+after+';no adjusted prices materialized'
                src=b[1] if b else sid_of(cfg['index_url'])
                out['price_continuity'].append(dict(security_id=sid,event_date=ex,ratio=ratio,previous_close=prev,
                    current_close=now,status=status,detail=detail,source_id=src))
                if status!='pass':review('price_continuity',sid,ex+': '+detail,src)
    for n,r in enumerate(sorted((x for rows in all_actions.values() for x in rows),key=lambda x:(x['security_id'],x['action_date'],x['action_type'],x['cash_amount'] or 0)),1):out['corporate_actions'].append(dict(action_id=n,**r))
    for sid,bars in sorted(all_prices.items()):
        ds=sorted(bars);ca=defaultdict(list)
        for r in all_actions[sid]:ca[r['action_date']].append(r)
        for prev,d in zip(ds,ds[1:]):
            ratio=bars[d][0]['close']/bars[prev][0]['close']
            if abs(ratio-1)>.4:
                actions=ca.get(d,[]);explained=False
                for a in actions:
                    if a['ratio'] and abs(ratio*a['ratio']-1)<=.4:explained=True
                    if a['cash_amount'] and abs((bars[d][0]['close']+a['cash_amount'])/bars[prev][0]['close']-1)<=.4:explained=True
                    if a['action_type'] in ('spinoff','class_distribution'):explained=True
                if not explained:out['price_anomalies'].append(dict(security_id=sid,session_date=d,previous_date=prev,
                  close_ratio=ratio,detail='unexplained >40% close change; gap_sessions='+str(bisect.bisect_left(dates,d)-bisect.bisect_left(dates,prev)-1),source_id=bars[d][1]))
    # Each actually active public version, including the noncanonical 2019 annual filing.
    timeline=[]
    for v in sorted(t['pool_versions'],key=lambda r:(r['available_at_utc'],r['version_id'])):
        if not timeline or v['report_period']>=timeline[-1]['report_period']:timeline.append(v)
    for n,v in enumerate(timeline):
        first=active_start(v['available_at_utc'],dates)
        nextday=active_start(timeline[n+1]['available_at_utc'],dates) if n+1<len(timeline) else ''
        if not first:continue
        last=(date.fromisoformat(nextday)-timedelta(days=1)).isoformat() if nextday else end
        expected=[d for d in dates if first<=d<=last];i=bisect.bisect_left(dates,first);warm=dates[max(0,i-252):i]
        for r in t['strategy_securities']:
            if r['version_id']!=v['version_id'] or not r['strategy_eligible']:continue
            sid=r['security_id'];bars=all_prices.get(sid,{})
            missing=[d for d in expected if d not in bars];wp=sum(d in bars for d in warm)
            out['price_quarter_coverage'].append(dict(version_id=v['version_id'],security_id=sid,active_from=first,active_to=last,
                 expected_sessions=len(expected),present_sessions=len(expected)-len(missing),warmup_expected=len(warm),warmup_present=wp,
                 status='complete' if not missing and len(warm)==252 and wp==252 and not any(x['security_id']==sid and x['status']!='pass' and (warm[0] if warm else first)<=x['event_date']<=last for x in out['price_continuity']) else 'incomplete',
                 missing_dates=json.dumps(dict(active=missing,warmup=[d for d in warm if d not in bars])),source_id=sid_of(cfg['index_url'])))
    # v05 normalized is a comparison reference only; never a source for price rows.
    compared=defaultdict(int)
    ref=cfg.get('comparison_url')
    if ref:
        src=sid_of(ref)
        for r in csv.DictReader(io.StringIO(store._read(ref).decode('utf-8-sig'))):
            sid=r['security_id'];d=r['session_date']
            if d<'2019-05-01' or d not in all_prices.get(sid,{}):continue
            compared[sid]+=1
            bar=all_prices[sid][d][0];diff={}
            for field in ['open','high','low','close','volume','turnover']:
                if field not in r:continue
                a=finite(r[field]);b=bar[field]
                if a is None and b is None:continue
                tolerance=max(1e-6,abs(a or 0)*1e-8)
                if a is None or b is None or abs(a-b)>tolerance:diff[field]=dict(v05=a,db=b)
            if diff:out['price_comparison'].append(dict(security_id=sid,session_date=d,differences=json.dumps(diff,sort_keys=True),source_id=src))
    for row in out['price_coverage']:
        sid=row['security_id'];detail=json.loads(row['detail'])
        qs=[x for x in out['price_quarter_coverage'] if x['security_id']==sid]
        detail.update(comparison_days=compared[sid],comparison_mismatched_days=sum(x['security_id']==sid for x in out['price_comparison']),
          quarters=[{k:v for k,v in x.items() if k not in ('source_id','security_id','missing_dates')} for x in qs],
          active_missing_sessions=sum(x['expected_sessions']-x['present_sessions'] for x in qs),
          warmup_missing_sessions=sum(x['warmup_expected']-x['warmup_present'] for x in qs))
        row['detail']=json.dumps(detail,ensure_ascii=False,sort_keys=True)
    maxid=max((r['review_id'] for r in t['review_items']),default=0)
    for n,r in enumerate(sorted(reviews,key=lambda x:(x['review_type'],x['subject_id'],x['detail'])),maxid+1):t['review_items'].append(dict(review_id=n,**r))
    return out


def export_csv(conn,out_dir):
    counts={}
    for table in ['price_coverage','price_quarter_coverage','price_lineage','price_continuity','price_anomalies',
                  'price_comparison','corporate_actions','price_calendar','ticker_checks','pool_exclusions']:
        if not conn.execute("select 1 from sqlite_master where name=?",(table,)).fetchone():continue
        header=[r[1] for r in conn.execute('pragma table_info('+table+')')]
        path=out_dir/(table+'.csv');n=0
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f,lineterminator='\n');writer.writerow(header)
            for row in conn.execute('select * from '+table+' order by '+','.join(str(i+1) for i in range(len(header)))):writer.writerow(row);n+=1
        counts[path.name]=n
    path=out_dir/'price_review_list.csv'
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f,lineterminator='\n');w.writerow(['review_type','security_id','detail','source_id'])
        rows=conn.execute("select review_type,subject_id,detail,source_id from review_items where review_type like 'price_%' order by review_type,subject_id,detail")
        w.writerows(rows)
    return counts
