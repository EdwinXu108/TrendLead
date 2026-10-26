"""Archive OpenD NONE responses. No database mutation and no adjusted-price requests.
Run only after the policy commit. Resumable journal lives under work; every response is immutable raw data.
"""
from __future__ import annotations
import ast,csv,io,json,sqlite3,time
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from .sources import RawStore,ROOT,DB_PATH,sha256_bytes,utc_now_iso

WORK=ROOT/'work/top200_v2'
CONFIG=ROOT/'config/db_prices_v1.json'
START='2012-12-01'


def collect():
    import futu
    WORK.mkdir(parents=True,exist_ok=True)
    state_path=WORK/'collector_state.json'
    if state_path.exists():state=json.loads(state_path.read_text(encoding='utf8'))
    else:state=dict(stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),start=START,
                    end=datetime.now(timezone.utc).date().isoformat(),entries={},calendar=[],calls=0)
    stamp=state['stamp'];store=RawStore(offline=False);last=0.0
    def save():state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf8',newline='\n')
    def archive(tag,payload):
        raw=json.dumps(payload,ensure_ascii=False,default=str,allow_nan=False).encode()
        key=f'opend://prices/{stamp}/{tag}'
        if key not in store.records:store._store_new(key,f'opend/{stamp}/{tag}.json',raw,'other','opend_sdk',tag)
        return key
    def call(tag,method,**kwargs):
        nonlocal last
        key=f'opend://prices/{stamp}/{tag}'
        if key in store.records:
            for suffix in ['_retry2','_retry1','']:
                candidate=key+suffix
                if candidate not in store.records:continue
                cached=json.loads(store._read(candidate))
                if isinstance(cached.get('page'),str) and cached['page'].startswith("b'"):
                    cached['page']=ast.literal_eval(cached['page'])
                return cached,candidate
        for attempt in range(3):
            time.sleep(max(0,0.63-(time.monotonic()-last)));last=time.monotonic()
            state['calls']+=1
            try:
                result=method(**kwargs);ret=int(result[0]);data=result[1]
                records=json.loads(data.to_json(orient='records')) if hasattr(data,'to_json') else data
                page=result[2] if len(result)>2 else None
                payload=dict(ret=ret,data=records,error=str(data) if ret else '',page=page,
                             params={k:str(v) for k,v in kwargs.items() if k!='page_req_key'})
            except Exception as ex:payload=dict(ret=-1,data=None,error=type(ex).__name__+': '+str(ex),page=None,params={})
            url=archive(tag+('' if attempt==0 else f'_retry{attempt}'),payload)
            if payload['ret']==0 or 'quota' in payload['error'].lower() or '额度' in payload['error']:return payload,url
            if attempt<2:time.sleep(2**(attempt+1))
        return payload,url
    def copy(src,tag):
        data=src.read_bytes();h=sha256_bytes(data);url='local://'+src.relative_to(ROOT).as_posix()+'#sha256='+h
        if url not in store.records:store._store_new(url,f'opend/reused/{h[:16]}_{src.name}',data,'other','local_copy',str(src.relative_to(ROOT)))
        return url
    ctx=futu.OpenQuoteContext(host='127.0.0.1',port=11111)
    try:
        if 'quota_before' not in state:
            p,u=call('quota_before',ctx.get_history_kl_quota,get_detail=True);state['quota_before']=u;save()
        for year in range(2012,int(state['end'][:4])+1):
            tag=f'calendar_{year}'
            if not any(tag in x for x in state['calendar']):
                p,u=call(tag,ctx.request_trading_days,market=futu.TradeDateMarket.US,
                         start=max(START,f'{year}-01-01'),end=min(state['end'],f'{year}-12-31'))
                if p['ret']!=0:raise RuntimeError('US trading calendar unavailable: '+p['error'])
                state['calendar'].append(u);save()
        c=sqlite3.connect(f'file:{DB_PATH}?mode=ro',uri=True)
        codes=[r[0] for r in c.execute("""select current_ticker from strategy_securities ss join pool_versions p using(version_id)
                  where ss.strategy_eligible=1 group by current_ticker order by max(p.report_period) desc,current_ticker""")]
        state['priority']=codes;save();c.close()
        for no,ticker in enumerate(codes+['CMCSK','BK','TYC','ACE'],1):
            if ticker in state['entries'] and state['entries'][ticker].get('finished'):continue
            entry=state['entries'].setdefault(ticker,dict(reused=[],klines=[],actions={}))
            rows={}
            for src in sorted((ROOT/'data/v05/raw').glob('opend_kline*/*.csv')):
                meta=src.with_suffix(src.suffix+'.meta.json')
                if not meta.exists():continue
                m=json.loads(meta.read_text(encoding='utf8'))
                if str(m.get('code','')).removeprefix('US.')!=ticker or m.get('params',{}).get('autype')!='NONE':continue
                if sha256_bytes(src.read_bytes())!=m.get('sha256'):raise RuntimeError('v05 cache digest failed: '+src.name)
                url=copy(src,'reuse');entry['reused'].append(url)
                for r in csv.DictReader(io.StringIO(src.read_text(encoding='utf8'))):
                    if START<=r['session_date']<=state['end']:rows[r['session_date']]=r
            dates=sorted(rows)
            ranges=[]
            if dates:
                if dates[0]>START:ranges.append((START,(date.fromisoformat(dates[0])-timedelta(days=1)).isoformat()))
                if dates[-1]<state['end']:ranges.append(((date.fromisoformat(dates[-1])+timedelta(days=1)).isoformat(),state['end']))
            else:ranges=[(START,state['end'])]
            if ticker=='ACE':ranges=[(START,'2016-01-14')]
            if state.get('quota_exhausted'):entry['error']='quota exhausted before request';save();continue
            for first,end in ranges:
                page=None
                for n in range(12):
                    tag=f'kline_{ticker}_{first}_{end}_{n}'
                    p,u=call(tag,ctx.request_history_kline,code='US.'+ticker,start=first,end=end,
                       ktype=futu.KLType.K_DAY,autype=futu.AuType.NONE,fields=[futu.KL_FIELD.ALL],
                       max_count=1000,page_req_key=page,extended_time=False)
                    if u not in entry['klines']:entry['klines'].append(u)
                    if p['ret']!=0:
                        entry['error']=p['error']
                        if any(x in p['error'].lower() for x in ['quota','额度','限额']):state['quota_exhausted']=True
                        break
                    page=p['page']
                    if not page:break
                if state.get('quota_exhausted'):break
            if ticker in codes:
                for method,func in [('rehab',ctx.get_rehab),('dividends',ctx.get_corporate_actions_dividends)]:
                    if method not in entry['actions']:
                        p,u=call(f'{method}_{ticker}',func,code='US.'+ticker);entry['actions'][method]=u
                # Preserve the older same-source action archive too (endpoint history can be truncated).
                for ext in ['csv','json']:
                    for stem in ['rehab','dividends']:
                        src=ROOT/f'data/v05/raw/opend_corporate_actions/{stem}_US_{ticker}.{ext}'
                        if src.exists():entry.setdefault('reused_actions',[]).append(copy(src,'action_reuse'))
            entry['reused']=sorted(set(entry['reused']));entry['finished']=True;save()
            print(f'{no}/{len(codes)+4} {ticker}: cached={len(rows)} requests={len(entry["klines"])} error={entry.get("error","")[:120]}',flush=True)
        p,u=call('quota_after_'+str(len(state['entries'])),ctx.get_history_kl_quota,get_detail=True);state['quota_after']=u;save()
        index=archive('index_'+sha256_bytes(json.dumps(state,sort_keys=True).encode())[:12],state)
        CONFIG.write_text(json.dumps(dict(index_url=index,start=START,end=state['end'],price_stage='raw_opend_v1'),indent=2),encoding='utf8',newline='\n')
        print('COMPLETE',index,flush=True)
    finally:ctx.close()

if __name__=='__main__':collect()
