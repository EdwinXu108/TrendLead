"""Registered top50 membership migration; the input database stays unchanged."""
import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from .build import content_hash
from .pool_exclusion_release import file_hash

BASE = '4e46f93f6c7e836dbd5e30dfe3ecfd8ebbb076180ac2d0397c4f8789381862bc'

def migrate(base, out, manifest, base_sha256=BASE):
    """Copy `base`, add the top50 membership, and check the counts. The base file itself is never touched."""
    base, out, manifest = map(Path, (base, out, manifest))
    if base.resolve() == out.resolve() or out.exists(): raise ValueError('output must be a new database')
    if file_hash(base) != base_sha256: raise ValueError('base hash mismatch')
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base, out)
    c = sqlite3.connect(out)
    try:
        c.execute('create table scope_members (scope text not null, version_id text not null, issuer_id text not null, historical_rank integer not null, primary key (scope, version_id, issuer_id))')
        c.execute("insert into scope_members select 'top50_v1',version_id,issuer_id,historical_rank from strategy_companies where historical_rank between 1 and 50 and strategy_eligible=1")
        counts = c.execute('select version_id,count(*) from scope_members group by version_id').fetchall()
        total = c.execute('select count(distinct issuer_id) from scope_members').fetchone()[0]
        dev = c.execute("select count(distinct s.issuer_id) from scope_members s join price_quarter_coverage p on p.version_id=s.version_id where p.active_from<= '2024-06-28'").fetchone()[0]
        excluded = c.execute("select distinct issuer_id from strategy_companies where historical_rank<=50 and strategy_eligible=0").fetchall()
        if len(counts)!=52 or min(n for _,n in counts)!=48 or max(n for _,n in counts)!=50 or sum(50-n for _,n in counts)!=19 or total!=98 or dev!=85 or {r[0] for r in excluded}!={'CIK:0000816284','CIK:0001578845'}: raise ValueError('scope counts do not match registration')
        if c.execute('pragma integrity_check').fetchone()[0] != 'ok': raise ValueError('integrity check failed')
        c.execute("insert or replace into meta values ('scope_release','scope_top50_v1')")
        digest=content_hash(c)
        c.execute("insert or replace into meta values ('content_sha256',?)",(digest,));c.commit()
    finally: c.close()
    result=dict(base_sha256=base_sha256,file_sha256=file_hash(out),content_sha256=digest,versions=52,empty_slots=19,development_companies=85,companies=98,integrity='ok')
    manifest.write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--out',required=True);p.add_argument('--manifest',required=True)
    p.add_argument('--base-sha256',default=BASE,help='expected sha256 of the base file; full_v1 is not the v1 base')
    a=p.parse_args()
    print(json.dumps(migrate(a.base,a.out,a.manifest,a.base_sha256),indent=2))
if __name__=='__main__': main()
