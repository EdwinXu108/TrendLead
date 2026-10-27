"""Deterministic, development-only migration from the sealed schema-6 database.

Never calls the full builder or corr.build. Post-cutoff rows are byte-logically preserved.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path
from . import api
from .build import content_hash

END='2024-06-28'
RELEASE='development_release_v1'
BASE_HASH='7564971e17d828289fdf46cb04413fce3d1ec60faac5461d5376774ce9751e87'
BASE_CONTENT='97b55e453e09b5e40570ab5ab9ae9d6e317131ab448c9b11ec719cc3d06bde6f'
CASH=Path('config/db_cash_overrides_v1.json')
EVENTS=Path('config/db_corporate_events_v1.json')
ADDENDUM=Path('research/bt_v3/TERRA_R1/development_release_v1.json')


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(8388608),b''):h.update(block)
    return h.hexdigest()


def rows_hash(conn, table, where='1=1'):
    cols=len(conn.execute(f'pragma table_info("{table}")').fetchall())
    order=','.join(str(i) for i in range(1,cols+1));h=hashlib.sha256();n=0
    for row in conn.execute(f'select * from "{table}" where {where} order by {order}'):
        h.update(json.dumps(tuple(row),ensure_ascii=False,separators=(',',':')).encode('utf8')+b'\n');n+=1
    return dict(rows=n,sha256=h.hexdigest())


def migrate(base: Path, out: Path, manifest: Path) -> dict:
    base=Path(base);out=Path(out);manifest=Path(manifest)
    if base.resolve()==out.resolve():raise ValueError('candidate must differ from the sealed base')
    if file_hash(base)!=BASE_HASH:raise ValueError('sealed base file hash mismatch')
    add=json.loads(ADDENDUM.read_text(encoding='utf8'))
    if file_hash(CASH)!=add['cash_evidence']['sha256'] or file_hash(EVENTS)!=add['event_evidence']['sha256']:
        raise ValueError('registered migration inputs do not match their hashes')
    if out.exists():raise FileExistsError(out)
    out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(base,out)
    c=sqlite3.connect(out);c.row_factory=sqlite3.Row;c.execute('pragma foreign_keys=on')
    original_meta=dict(c.execute('select key,value from meta'))
    if original_meta.get('content_sha256')!=BASE_CONTENT:raise ValueError('base content metadata mismatch')
    original=sqlite3.connect(f'file:{base.as_posix()}?mode=ro',uri=True)
    post_before={table:rows_hash(original,table,f"{col} > '{END}'") for table,col in
                 [('corporate_actions','action_date'),('tri_daily','session_date')]}
    all_tables=[r[0] for r in original.execute("select name from sqlite_master where type='table' and name not in ('meta','build_log') and name not like 'sqlite_%' order by name")]
    immutable=[t for t in all_tables if t not in ('corporate_actions','tri_daily')]
    untouched_before={t:rows_hash(original,t) for t in immutable}
    changes=[];deltas=defaultdict(lambda:defaultdict(float))
    cash=json.loads(CASH.read_text(encoding='utf8'))
    for item in cash['entries']:
        sid,day=item['security_id'],item['ex_date']
        if day>END:raise ValueError('cash correction exceeds development cutoff')
        rows=c.execute("select * from corporate_actions where security_id=? and action_date=? and action_type in ('cash_dividend','special_dividend')",(sid,day)).fetchall()
        if len(rows)!=1:raise ValueError(f'{sid} {day}: expected one cash action')
        row=rows[0];expected=float(item['database_cash']);actual=float(row['cash_amount'])
        if abs(expected-actual)>1e-8:raise ValueError(f'{sid} {day}: base cash differs')
        value=float(item['official_cash']);review=int(row['needs_review'])
        if value!=actual or review:
            notes=row['notes']+';official_cash_source='+item['source_url']
            c.execute("update corporate_actions set cash_amount=?,needs_review=0,resolution='official_cash_override_v1',notes=? where action_id=?",(value,notes,row['action_id']))
            deltas[sid][day]+=value-actual
            changes.append(dict(table='corporate_actions',security_id=sid,date=day,kind='cash',before=actual,after=value,source=item['source_url']))
    ev=json.loads(EVENTS.read_text(encoding='utf8'))
    def price(sid,day):
        r=c.execute("select close from prices_daily where security_id=? and session_date=? and source='opend_none'",(sid,day)).fetchone()
        if not r or not r[0]:raise ValueError(f'{sid} {day}: missing documented child or parent close')
        return float(r[0])
    def previous(sid,day):
        r=c.execute("select close from tri_daily where security_id=? and session_date<? order by session_date desc limit 1",(sid,day)).fetchone()
        if not r:raise ValueError(f'{sid} {day}: missing previous close')
        return float(r[0])
    def meta_note(row,child,ratio,childclose,source):
        details=dict(child_security_id=child,child_ratio=ratio,child_close=childclose,source_url=source,
                     valuation='raw_child_close',cash_available='next_session_open')
        return row['notes']+';verified_entitlement='+json.dumps(details,sort_keys=True,separators=(',',':'))
    def set_distribution(sid,day,kind,child,ratio,childclose,source,old_value=None):
        rows=c.execute('select * from corporate_actions where security_id=? and action_date=? and action_type=?',(sid,day,kind)).fetchall()
        if len(rows)!=1:raise ValueError(f'{sid} {day}: distribution event not unique')
        row=rows[0];new=ratio*childclose
        if old_value is None:
            old_value=(1-float(row['vendor_factor']))*previous(sid,day)
        deltas[sid][day]+=new-old_value
        c.execute("update corporate_actions set ratio=?,spin_value=?,needs_review=0,resolution='verified_child_close_v1',notes=? where action_id=?",(ratio,new,meta_note(row,child,ratio,childclose,source),row['action_id']))
        changes.append(dict(table='corporate_actions',security_id=sid,date=day,kind=kind,before=old_value,after=new,source=source))
    google=ev['class_distributions'][0];day=google['ex_date'];child=google['child_security_id'];childclose=price(child,day)
    for sid in (google['parent_security_id'],'CUSIP:02079K305'):
        rows=c.execute("select * from corporate_actions where security_id=? and action_date=? and action_type='class_distribution'",(sid,day)).fetchall()
        if not rows:continue
        row=rows[0];p=price(sid,day);oldratio=float(row['ratio']);ratio=google['child_shares_per_parent']
        set_distribution(sid,day,'class_distribution',child,ratio,childclose,google['source'],old_value=(oldratio-1)*p)
    for key in ('ge_healthcare_2023','ge_vernova_2024'):
        e=ev[key];child=e['child_security_id'];day=e['ex_date'];childclose=float(e['child_raw_close']) if 'child_raw_close' in e else price(child,day)
        set_distribution(e['parent_security_id'],day,'spinoff',child,e['child_shares_per_parent'],childclose,e['source'])
    e=ev['dowdupont_2019'];sid=e['parent_security_id']
    for day in e['suppressed_vendor_splits']:
        row=c.execute("select * from corporate_actions where security_id=? and action_date=? and action_type='split'",(sid,day)).fetchone()
        if not row:raise ValueError(f'{sid} {day}: expected vendor split missing')
        p=price(sid,day);deltas[sid][day]+=(1-float(row['ratio']))*p
        c.execute('delete from corporate_actions where action_id=?',(row['action_id'],))
        changes.append(dict(table='corporate_actions',security_id=sid,date=day,kind='remove_vendor_split',before=row['ratio'],after=1.0,source=e['evidence']))
    next_id=c.execute('select max(action_id) from corporate_actions').fetchone()[0]+1
    for child_event in e['distributions']:
        day=child_event['ex_date'];child=child_event['child_security_id'];ratio=float(child_event['child_shares_per_parent']);close=price(child,day)
        value=ratio*close;deltas[sid][day]+=value
        note='verified_entitlement='+json.dumps(dict(child_security_id=child,child_ratio=ratio,child_close=close,
                  source_url=child_event['source'],valuation='raw_child_close',cash_available='next_session_open'),sort_keys=True,separators=(',',':'))
        c.execute("insert into corporate_actions(action_id,security_id,action_date,action_type,ratio,cash_amount,needs_review,currency,source,source_id,notes,sub_ticker,vendor_factor,spin_value,resolution) values(?,?,?,'spinoff',?,NULL,0,'USD','verified_config',NULL,?,?,NULL,?,'verified_child_close_v1')",(next_id,sid,day,ratio,note,child,value))
        next_id+=1;changes.append(dict(table='corporate_actions',security_id=sid,date=day,kind='add_spinoff',before=None,after=value,source=child_event['source']))
    for sid,events in sorted(deltas.items()):
        events={d:v for d,v in events.items() if abs(v)>1e-14}
        if not events:continue
        days=sorted(events);first=days[0];factor=1.0
        rows=c.execute('select session_date,close,tri,daily_return,flags from tri_daily where security_id=? and session_date<=? order by session_date',(sid,END)).fetchall()
        prev=None;updates=[];seen=set()
        for row in rows:
            day=row['session_date'];daily=row['daily_return']
            if day in events:
                if prev is None or daily is None or prev<=0:raise ValueError(f'{sid} {day}: TRI preceding close missing')
                oldgross=1+float(daily);newdaily=float(daily)+events[day]/prev
                if oldgross<=0 or 1+newdaily<=0:raise ValueError(f'{sid} {day}: invalid TRI return')
                factor*=(1+newdaily)/oldgross;daily=newdaily;seen.add(day)
                changes.append(dict(table='tri_daily',security_id=sid,date=day,kind='event_return',before=row['daily_return'],after=daily,source='registered_cash_and_event_evidence'))
            if day>=first:
                flags=row['flags']
                if day in events:flags=','.join(x for x in (flags,'development_release_v1') if x)
                updates.append((float(row['tri'])*factor,daily,flags,sid,day))
            prev=float(row['close'])
        if set(events)!=seen:raise ValueError(f'{sid}: missing TRI dates {sorted(set(events)-seen)}')
        c.executemany('update tri_daily set tri=?,daily_return=?,flags=? where security_id=? and session_date=?',updates)
    c.execute('create table benchmark_bars_development(session_date text primary key, open real not null, close real not null)')
    bars=api._bounded_benchmark_archive('2012-12-01',END)
    c.executemany('insert into benchmark_bars_development(session_date,open,close) values(?,?,?)',[(d,b['open'],b['close']) for d,b in sorted(bars.items())])
    c.execute("insert or replace into meta(key,value) values('economic_data_version',?)",(RELEASE,))
    c.execute("insert or replace into meta(key,value) values('economic_development_end',?)",(END,))
    c.execute("insert or replace into meta(key,value) values('economic_cash_config_sha256',?)",(file_hash(CASH),))
    c.execute("insert or replace into meta(key,value) values('economic_event_config_sha256',?)",(file_hash(EVENTS),))
    c.commit()
    integrity=c.execute('pragma integrity_check').fetchone()[0];foreign=c.execute('pragma foreign_key_check').fetchall()
    if integrity!='ok' or foreign:raise RuntimeError('candidate integrity or foreign key check failed')
    post_after={table:rows_hash(c,table,f"{col} > '{END}'") for table,col in
                [('corporate_actions','action_date'),('tri_daily','session_date')]}
    untouched_after={t:rows_hash(c,t) for t in immutable}
    if post_before!=post_after or untouched_before!=untouched_after:raise RuntimeError('untouched or post-cutoff data changed')
    # content_hash uses tuples; its scope includes the new bounded benchmark table.
    c.row_factory=None;digest=content_hash(c)
    c.execute("insert or replace into meta(key,value) values('content_sha256',?)",(digest,));c.commit();c.close();original.close()
    result=dict(release=RELEASE,base_file_sha256=BASE_HASH,base_content_sha256=BASE_CONTENT,
                candidate_file_sha256=file_hash(out),candidate_content_sha256=digest,end=END,
                cash_config_sha256=file_hash(CASH),event_config_sha256=file_hash(EVENTS),
                post_cutoff_hashes=post_after,untouched_tables=untouched_after,
                benchmark_rows=len(bars),foreign_key_violations=len(foreign),integrity=integrity,changes=changes)
    manifest.parent.mkdir(parents=True,exist_ok=True);manifest.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf8')
    return result


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--out',required=True);p.add_argument('--manifest',required=True)
    a=p.parse_args(argv);result=migrate(Path(a.base),Path(a.out),Path(a.manifest));print(json.dumps({k:v for k,v in result.items() if k not in ('changes','untouched_tables','post_cutoff_hashes')},indent=2))

if __name__=='__main__':main()
