"""Reproducible, price-free SEC/OpenD basic-information cross-checks from archived responses."""
import json
from .sources import ROOT
from .build import source_id


def verify(tables, store):
    cfg=json.loads((ROOT/'config/db_verification_sources_v2.json').read_text(encoding='utf8'))
    sec_url=cfg['sec_keys']['https://www.sec.gov/files/company_tickers.json']
    sec=json.loads(store._read(sec_url))
    sec_codes={r['ticker']:f'CIK:{int(r["cik_str"]):010d}' for r in sec.values()}
    basic=json.loads(store._read(cfg['basic_url']))
    if basic['ret'] != 0:
        raise ValueError('archived OpenD basic-information request failed')
    broker={r['code']:r for r in basic['records']}
    issuers={r['security_id']:r['issuer_id'] for r in tables['securities']}
    selected={(r['current_security_id'],r['current_ticker']) for r in tables['strategy_securities'] if r['strategy_eligible']}
    rows=[]
    for sid,ticker in sorted(selected):
        actual=sec_codes.get(ticker.replace('.','-'),'')
        b=broker.get('US.'+ticker)
        rows.append(dict(security_id=sid,current_ticker=ticker,sec_issuer_id=actual,opend_code='US.'+ticker,
            opend_stock_id=str(b.get('stock_id','')) if b else '',
            sec_result='pass' if actual==issuers[sid] else 'mismatch',
            opend_result='pass' if b and not b.get('delisting') and b.get('exchange_type') in
                ('US_NYSE','US_NASDAQ','US_AMEX','US_CBOE') else 'needs_review',
            checked_at_utc=store.records[cfg['basic_url']]['retrieved_at_utc'],
            sec_source_id=source_id(store.records[sec_url]['sha256']),
            opend_source_id=source_id(store.records[cfg['basic_url']]['sha256'])))
    return rows
