"""Quarterly membership tables for the registered top-50 scope."""
import csv
import sqlite3
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'research/scope_top50_v1'
sys.path.insert(0, str(ROOT))
from leadtrend_db import api
links = {r['predecessor_issuer_id']: r['successor_issuer_id'] for r in api.issuer_continuity(ROOT/'data/db/leadtrend_db_v2.sqlite')}
def canonical(issuer):
    seen = set()
    while issuer in links and issuer not in seen:
        seen.add(issuer); issuer = links[issuer]
    return issuer
c=sqlite3.connect(f"file:{(ROOT/'data/db/leadtrend_db_v2.sqlite').as_posix()}?mode=ro",uri=True);c.row_factory=sqlite3.Row
versions=c.execute("select pv.version_id,pv.report_period,pv.form,min(pc.active_from) effective from pool_versions pv join price_quarter_coverage pc using(version_id) group by pv.version_id order by effective,pv.version_id").fetchall()
quarters=[]; members={}; previous=set()
for v in versions:
    items=c.execute("""select s.issuer_id,s.historical_rank,sc.current_ticker,t.issuer_name_at_period,t.amount_cents,t.fund_weight_raw from scope_members s join strategy_companies sc on sc.version_id=s.version_id and sc.issuer_id=s.issuer_id join top200_companies t on t.version_id=s.version_id and t.issuer_id=s.issuer_id where s.scope='top50_v1' and s.version_id=? order by s.historical_rank""",(v['version_id'],)).fetchall()
    # ticker comes from strategy_companies, not scope_members
    ids={x['issuer_id'] for x in items};rank50=c.execute('select issuer_name_at_period,amount_cents from top200_companies where version_id=? and rank=50',(v['version_id'],)).fetchone()
    quarters.append(dict(version_id=v['version_id'],quarter=v['report_period'],source=v['form'],effective=v['effective'],eligible=len(items),empty=50-len(items),entered=len(ids-previous),exited=len(previous-ids),rank1=items[0]['issuer_name_at_period'] if items else '',rank50=rank50[0] if rank50 else '',rank50_amount_100m=rank50[1]/1e10 if rank50 else '',fund_weight=sum(x['fund_weight_raw'] or 0 for x in items)))
    for x in items:
        key=canonical(x['issuer_id'])
        merged=c.execute('select name from issuers where issuer_id=?',(key,)).fetchone()
        m=members.setdefault(key,dict(ticker=x['current_ticker'],company=x['issuer_name_at_period'],
            merged_company=merged[0] if merged else x['issuer_name_at_period'],first=v['report_period'],
            last=v['report_period'],versions=set(),development=False))
        m['ticker']=x['current_ticker']; m['company']=x['issuer_name_at_period'];m['last']=v['report_period']
        m['versions'].add(v['version_id']);m['development']|=v['effective']<='2024-06-28'
    previous=ids
def write(name,rows):
    with (OUT/name).open('w',encoding='utf8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator='\n' if name == 'members.csv' else '\r\n');w.writeheader();w.writerows(rows)
for m in members.values(): m['quarters']=len(m.pop('versions'))
write('quarters.csv',quarters);write('members.csv',sorted(members.values(),key=lambda x:x['ticker']))
print('merged companies', len(members), 'development',sum(m['development'] for m in members.values()))
