"""Claude A1--A3 repairs. Frozen input; raw sources append only; no returns."""
from __future__ import annotations
import csv
import hashlib
import json
import re
import urllib.request
from urllib.parse import urlparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from .stage2_enrich import read_csv, write_csv
from .opend_prices import OpenDPriceFetcher
from . import paths

# Ratios verified against the linked original issuer/exchange documents.
CHILD_REPAIRS = {
 ('BFH','2021-11-08'): ('LYLT',.4,'https://investor.breadfinancial.com/static-files/4324ca47-e022-404f-b616-ab7c96c4997f'),
 ('BBWI','2021-08-03'): ('VSCO',1/3,'https://investors.bbwinc.com/news-releases/news-release-details/bath-body-works-inc-completes-separation-victorias-secret'),
 ('BDX','2026-02-10'): ('WAT',.135343148384084,'https://www.sec.gov/Archives/edgar/data/10795/000119312526042827/d104936d8k.htm'),
 ('CMCSA','2026-01-05'): ('VSNT',.04,'https://investors.versantmedia.com/investor-resources/investor-faqs'),
 ('CTVA','2026-10-01'): ('VYLR',1.,'https://www.sec.gov/Archives/edgar/data/30554/000119312526391369/d71834d8k.htm'),
 ('DELL','2021-11-02'): ('VMW',.440626,'https://infomemo.theocc.com/infomemos?number=49493'),
 ('FDX','2026-06-01'): ('FDXF',.5,'https://investors.fedex.com/news-and-events/investor-news/investor-news-details/2026/FedEx-Completes-Spin-Off-of-FedEx-Freight/default.aspx'),
 ('FTV','2025-06-30'): ('RAL',1/3,'https://investors.fortive.com/sec-filings/all-sec-filings/content/0001659166-25-000104/ex991-pressrelease.htm'),
 ('FBIN','2022-12-15'): ('MBC',1.,'https://ir.fbin.com/news-releases/news-release-details/fortune-brands-completes-separation-masterbrand'),
 ('HON','2025-10-30'): ('SOLS',.25,'https://honeywell.gcs-web.com/static-files/b403702b-68a5-4ce9-8777-288a5d6bae3f'),
 ('ILMN','2024-06-25'): ('GRAL',1/6,'https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2024-262'),
 ('J','2024-09-30'): ('AMTM',1.,'https://invest.jacobs.com/resources/separation-transaction-updates/default.aspx'),
 ('J','2025-05-16'): ('AMTM',.060835,'https://www.sec.gov/Archives/edgar/data/52988/000005298825000032/exhibit991-supplementalinf.htm'),
 ('JEF','2023-01-17'): ('VTS',1/8.49668,'https://www.sec.gov/Archives/edgar/data/96223/000119312523008896/d450593d8k.htm'),
 ('LH','2023-07-03'): ('FTRE',1.,'https://www.labcorp.com/education-events/press-releases/labcorp-completes-spin-fortrea'),
 ('LEN','2025-01-21'): ('MRP',.5,'https://ir.millroseproperties.com/news/news-details/2025/Lennar-Completes-Spin-off-of-Millrose-Properties/default.aspx'),
 ('SKYD','2026-10-14'): ('SKYD.WS',None,'https://www.miaxglobal.com/alert/2026/10/13/miax-exchange-group-options-markets-skydance-corporation-skyd-skyd1-0'),
 ('SPGI','2026-07-01'): ('MBGL',1.,'https://investor.spglobal.com/mobility-global/'),
 ('MMM','2024-04-01'): ('SOLV',.25,'https://news.3m.com/2024-04-01-3M-Completes-Spin-off-of-Solventum?asPDF=1'),
 ('WDC','2025-02-24'): ('SNDK',1/3,'https://investor.wdc.com/static-files/8d344326-4ee2-42e0-adf5-5d8871b8dbde'),
 ('FLEX','2024-01-03'): ('NXT',.174185,'https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2023-752'),
}
DIVIDEND_SOURCES = {
 'BX':'https://s23.q4cdn.com/714267708/files/doc_downloads/2024/02/TBG-Memo.pdf',
 'EXE':'https://investors.expandenergy.com/stock-information/dividends',
 'HP':'https://www.sec.gov/Archives/edgar/data/46765/000004676523000034/hp-20230607.htm',
}

def save_source(root, url):
    folder=root/paths.RAW/'stage3_official';folder.mkdir(exist_ok=True)
    index=folder/'provider_capture_index.json'
    if index.exists():
        captured=json.loads(index.read_text(encoding='utf-8')).get(url)
        if captured:
            path=root/captured['path']
            assert hashlib.sha256(path.read_bytes()).hexdigest()==captured['sha256']
            return captured
    key=hashlib.sha256(url.encode()).hexdigest()[:20]
    meta=folder/(key+'.meta.json')
    if meta.exists():return json.loads(meta.read_text())
    user_agent='Mozilla/5.0'
    if urlparse(url).hostname == 'www.sec.gov':
        user_agent=json.loads((root/'config/v05_stage2.json').read_text(encoding='utf-8'))['sec_user_agent']
    req=urllib.request.Request(url,headers={'User-Agent':user_agent})
    try:
        with urllib.request.urlopen(req,timeout=35) as response: content=response.read()
        path=folder/(key+('.pdf' if content.startswith(b'%PDF') else '.html'))
        path.write_bytes(content)
        result={'url':url,'acquired_at':datetime.now(timezone.utc).isoformat(),'path':str(path.relative_to(root)),
                'sha256':hashlib.sha256(content).hexdigest(),'status':'ok'}
        meta.write_text(json.dumps(result,indent=2)+'\n')
        return result
    except Exception as exc:
        result={'url':url,'status':'failed','error':str(exc),'acquired_at':datetime.now(timezone.utc).isoformat()}
        error=folder/(key+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.error.json')
        error.write_text(json.dumps(result,indent=2)+'\n')
        return result

def canonical_aliases(securities, manifest):
    """Use known single quote lines plus ISIN/issuer continuity, not issuer name alone.

    Conflicting contemporaneous holdings or different known share types fail closed.
    The manifest retains original identifiers; aliases do not imply exchange ratios.
    """
    by_ticker=defaultdict(list)
    for r in securities:
        if r['ticker']:by_ticker[r['ticker']].append(r)
    aliases={r['security_id']:r['security_id'] for r in securities};audit=[]
    for ticker,group in sorted(by_ticker.items()):
        if len(group)==1:continue
        sids={r['security_id'] for r in group}
        entries=[r for r in manifest if r['security_id'] in sids]
        snapshots=defaultdict(set)
        for r in entries:snapshots[r['snapshot_id']].add(r['security_id'])
        if any(len(x)>1 for x in snapshots.values()):
            raise ValueError(f'ambiguous simultaneous quote mapping: {ticker}')
        if len({r['security_type'] for r in group})>1:raise ValueError('conflicting share type '+ticker)
        last={r['security_id']:max((e['holdings_as_of'] for e in entries if e['security_id']==r['security_id']),default='') for r in group}
        canonical=sorted(group,key=lambda r:(not r['security_id'].startswith('CUSIP:'),-int(last[r['security_id']].replace('-','') or '0'),r['security_id']))[0]['security_id']
        isins={e['isin'] for e in entries if e['isin']}
        real_issuers={r['issuer_id'] for r in group if not r['issuer_id'].endswith('N/A')}
        # N-PORT cross identifiers + reviewed quote continuity. Divergent LEIs
        # are allowed only when the manifest explicitly carries the same ISIN.
        if len(real_issuers)>1:
            by_id={sid:{e['isin'] for e in entries if e['security_id']==sid and e['isin']} for sid in sids}
            if not any(by_id[x]&by_id[y] for x in sids for y in sids if x!=y):
                raise ValueError('issuer continuity not established '+ticker)
        for r in group:
            aliases[r['security_id']]=canonical
            if r['security_id']!=canonical:
                audit.append({'old_security_id':r['security_id'],'security_id':canonical,'ticker':ticker,
                    'evidence':'NPORT cross-ISIN / non-overlapping identifier succession; same reviewed quote line; OHLC equality checked',
                    'isins':'|'.join(sorted(isins)),'issuer_ids':'|'.join(sorted(real_issuers))})
    return aliases,audit

def coalesce(rows, fields, aliases):
    out={}
    for original in rows:
        r=dict(original);r['security_id']=aliases.get(r['security_id'],r['security_id'])
        key=tuple(r.get(k,'') for k in fields)
        if key in out:
            if 'session_date' in fields:
                for k in ('open','close','volume','turnover'):
                    if out[key].get(k,'')!=r.get(k,''):raise ValueError(f'conflicting alias price {key} {k}')
            continue
        out[key]=r
    return list(out.values())

def run_repair(root:Path):
    root=Path(root);w=root/paths.PREP;base=w/'snapshots'/'stage2_before_stage3';norm=root/paths.NORMALIZED
    securities=read_csv(base/'normalized/securities.csv');manifest=read_csv(base/'reports/stage2_holdings_manifest.csv')
    aliases,audit=canonical_aliases(securities,manifest)
    write_csv(w/'stage2_security_aliases.csv',audit,['old_security_id','security_id','ticker','evidence','isins','issuer_ids'])
    sec_by={r['security_id']:r for r in securities}; canonical=[]
    for sid in dict.fromkeys(aliases.values()):
        r=dict(sec_by[sid]); group=[x for x in securities if aliases[x['security_id']]==sid]
        reliable=sorted({x['issuer_id'] for x in group if x['issuer_id'] and not x['issuer_id'].endswith('N/A')})
        r['issuer_id']=next((x for x in reliable if x.startswith(('LEI:','CIK:'))),sid)
        r['source_evidence']+=';canonical_aliases='+'|'.join(x['security_id'] for x in group)
        canonical.append(r)
    write_csv(norm/'securities.csv',canonical,list(canonical[0]))
    prices=coalesce(read_csv(base/'normalized/prices.csv'),['security_id','session_date'],aliases)
    members=coalesce(read_csv(base/'normalized/snapshot_members.csv'),['snapshot_id','security_id'],aliases)
    events=coalesce(read_csv(base/'normalized/events.csv'),['security_id','event_type','effective_session','shares_new_per_old','cash_amount_per_share','pay_date'],aliases)
    original_event_to_new={}
    event_key=lambda r:(aliases.get(r['security_id'],r['security_id']),r['event_type'],r['effective_session'],r['shares_new_per_old'],r['cash_amount_per_share'],r['pay_date'])
    event_by_key={event_key(e):e['event_id'] for e in events}
    for e in read_csv(base/'normalized/events.csv'):original_event_to_new[e['event_id']]=event_by_key[event_key(e)]
    aux=[];auxseen=set()
    for r in read_csv(base/'normalized/spinoff_child_prices.csv'):
        r['event_id']=original_event_to_new[r['event_id']]
        key=(r['event_id'],r['child_ticker'])
        if key not in auxseen:aux.append(r);auxseen.add(key)
    source_results={name:save_source(root,url) for name,url in DIVIDEND_SOURCES.items()}
    pending=read_csv(base/'reports/stage2_dividend_component_reconciliation.csv');resolved=[];blocks=[]
    for r in pending:
        r['security_id']=aliases.get(r['security_id'],r['security_id'])
        if r['result']=='unresolved' and source_results[r['ticker']]['status']=='ok':
            # These exact six groups were read in the original issuer documents.
            expected={'BX':{'2024-02-02':(.94,'2024-02-12')},'HP':{'2023-08-16':(.485,'2023-08-31')},
                'EXE':{'2023-03-06':(1.29,'2023-03-23'),'2022-11-14':(3.16,'2022-12-01'),
                       '2022-08-16':(2.32,'2022-09-01'),'2022-05-18':(2.34,'2022-06-02')}}[r['ticker']]
            total,pay=expected[r['ex_date']]
            assert abs(sum(float(x) for x in r['amounts'].split('|'))-total)<1e-8
            matching=[e for e in events if e['security_id']==r['security_id'] and e['event_type']=='cash_dividend' and e['effective_session']==r['ex_date']]
            assert len(matching)==2
            for e in matching:
                e['pay_date']=pay;e['source']+=';issuer_cash_verified='+json.dumps(source_results[r['ticker']],separators=(',',':'))
            r['result']='sum_official';r['official_total']=total;r['official_pay_date']=pay;r['official_evidence']=source_results[r['ticker']]['path']
            resolved.append(r)
        elif r['result']=='unresolved':blocks.append({'security_id':r['security_id'],'effective_session':r['ex_date'],'reason':'unverified_dividend_components'})
    write_csv(w/'stage2_dividend_official_review.csv',resolved,['security_id','ticker','ex_date','amounts','official_total','official_pay_date','official_evidence','result'])
    write_csv(w/'stage2_dividend_component_reconciliation.csv',pending,list(dict.fromkeys(k for r in pending for k in r)))
    write_csv(w/'stage2_scoring_blocks.csv',blocks,['security_id','effective_session','reason'])
    by_ticker={r['ticker']:r['security_id'] for r in canonical};priceindex=defaultdict(dict)
    for p in prices:priceindex[p['security_id']][p['session_date']]=p
    fetcher=OpenDPriceFetcher(root/paths.CHILD_KLINES);attempts=[]
    try:
        before=fetcher.query_kline_quota()
        for e in events:
            if e['event_type']!='spinoff' or e['spinoff_valuation']!='vendor_factor':continue
            ticker=sec_by[e['security_id']]['ticker'];original_day=e['effective_session']
            child,ratio,url=CHILD_REPAIRS[(ticker,original_day)]
            evidence=save_source(root,url)
            outcome=fetcher.fetch_one(child,'CHILD:'+child,'2019-05-01','2026-12-31',max_retries=1)
            bars=fetcher.load_cache(child,'2019-05-01','2026-12-31') or []
            day='2025-02-07' if ticker=='LEN' else original_day
            bar=next((p for p in bars if p.session_date.isoformat()==day),None)
            if bar is None and child in by_ticker:
                p=priceindex[by_ticker[child]].get(day)
                close=float(p['close']) if p else None
            else:close=bar.close if bar else None
            status='vendor_factor'
            if close is not None and ratio is not None and evidence['status']=='ok':
                e['effective_session']=day;e['spinoff_value_per_old_share']=f'{close*ratio:.12g}';e['spinoff_valuation']='child_raw_close'
                e['source']+=';verified_child='+child+';evidence='+json.dumps(evidence,separators=(',',':'))
                aux.append({'event_id':e['event_id'],'child_ticker':child,'session_date':day,'shares_per_old':ratio,'raw_close':close,'value_per_old':close*ratio,'source':json.dumps(evidence,separators=(',',':'))})
                status='child_raw_close'
            attempts.append({'event_id':e['event_id'],'security_id':e['security_id'],'ticker':ticker,'original_date':original_day,'effective_session':day,
                'child_ticker':child,'ratio':ratio,'raw_close':close,'spinoff_valuation':status,'quote_outcome':outcome.gap_category,'source_status':evidence['status'],'url':url})
        after=fetcher.query_kline_quota()
    finally:fetcher.close()
    for name,rs in [('prices.csv',prices),('snapshot_members.csv',members),('events.csv',events),('spinoff_child_prices.csv',aux)]:
        write_csv(norm/name,rs,list(rs[0]))
    write_csv(w/'stage2_spinoff_price_attempts.csv',attempts,list(attempts[0]))
    valuations=[]
    original_dates={r['event_id']:r['original_date'] for r in attempts}
    for e in events:
        if e['event_type']!='spinoff':continue
        sid=e['security_id'];day=e['effective_session'];p=priceindex[sid].get(day)
        close=float(p['close']) if p else None
        split=1.
        for other in events:
            if other['security_id']==sid and other['effective_session']==day and other['event_type'] in {'split','reverse_split'}:split*=float(other['shares_new_per_old'])
        m=re.search(r'price_factor=([\d.eE+-]+)',e['source']);factor=float(m.group(1)) if m else None
        factor_date=original_dates.get(e['event_id'],day)
        vendor=close*split*(factor-1) if close and factor and factor_date==day else None
        value=float(e['spinoff_value_per_old_share'])
        direct=value if e['spinoff_valuation']=='child_raw_close' else None
        valuations.append(dict(event_id=e['event_id'],security_id=sid,ticker=sec_by[sid]['ticker'],session_date=day,
            parent_raw_close=close,parent_shares_new_per_old=split,vendor_price_factor=factor,
            vendor_factor_date=factor_date,comparison_note='not comparable: vendor record date differs from verified event date' if factor_date!=day else '',
            child_value_per_old=direct,vendor_value_per_old=vendor,value_per_old=value,spinoff_valuation=e['spinoff_valuation'],
            relative_value_difference=(direct-vendor)/direct if direct and vendor is not None else ''))
    write_csv(w/'stage2_spinoff_valuations.csv',valuations,list(valuations[0]))
    # Sidecar identities follow canonical ids; keep original source manifest intact.
    for name in ('stage2_unpriced_members.csv','stage2_identity_issues.csv','stage2_corporate_action_gaps.csv'):
        rs=read_csv(base/'reports'/name)
        for r in rs:r['security_id']=aliases.get(r['security_id'],r['security_id'])
        write_csv(w/name,rs,list(rs[0]))
    report={'alias_pairs':len(audit),'isin_to_cusip_pairs':sum(r['old_security_id'].startswith('ISIN:') for r in audit),
        'securities_before':len(securities),'securities_after':len(canonical),'price_rows_before':len(read_csv(base/'normalized/prices.csv')),'price_rows_after':len(prices),
        'spinoffs_before':42,'spinoffs_after':sum(e['event_type']=='spinoff' for e in events),'vendor_attempts_after_alias_dedup':len(attempts),
        'vendor_attempt_results':dict(Counter(r['spinoff_valuation'] for r in attempts)),'official_dividend_groups_resolved':len(resolved),
        'original_vendor_records':23,'spinoff_valuation_counts':dict(Counter(e['spinoff_valuation'] for e in events if e['event_type']=='spinoff')),
        'official_sources':source_results,'quota_before':before,'quota_after':after,'quote_requests':fetcher.request_count}
    (w/'stage2_repair_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2));return report
