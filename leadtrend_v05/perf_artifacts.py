"""Lossless CSV storage, shared tables and reversible decision projection."""
import csv,gzip,hashlib,io,json,os,sys
from pathlib import Path
from .perf_bench import dump,sha

DATE_KEYS=('session_date','signal_session_date','execution_session','holdings_as_of')
_VERIFIED={};_BOUNDS={};_BASES={}
STATIC=('issuer_id','industry','score','rank')

def csv_text(value):return '' if value is None else str(value)

def score_base(folder,rec):
    path=referenced(folder,rec);key=rec['physical_sha256']
    if key not in _BASES:
        base={}
        for r in csv_rows(path):
            k=(sys.intern(r['signal_session_date']),sys.intern(r['security_id']))
            base[k]=(sys.intern(r['issuer_id']),sys.intern(r['industry']),r['score'],sys.intern(r['rank']))
        _BASES[key]=base
    return _BASES[key]

def gzip_text(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    raw=path.open('wb');z=gzip.GzipFile(filename='',fileobj=raw,mode='wb',compresslevel=1,mtime=0)
    # TextIOWrapper owns GzipFile; GzipFile does not own passed fileobj.
    return raw,io.TextIOWrapper(z,encoding='utf-8',newline='')

def write_gzip_csv(path,rows,fields):
    raw,f=gzip_text(path)
    try:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
    finally:f.close();raw.close()

def fields_for(rows,default):return list(dict.fromkeys(k for r in rows for k in r)) if rows else list(default)

def logical_sha(path):
    h=hashlib.sha256();opener=gzip.open if str(path).endswith('.gz') else open
    with opener(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def csv_rows(path):
    path=Path(path);opener=gzip.open if path.suffix=='.gz' else open
    with opener(path,'rt',encoding='utf-8-sig',newline='') as f:yield from csv.DictReader(f)

def manifest(folder):
    p=Path(folder)/'artifacts.json'
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None

def referenced(folder,record):
    p=(Path(folder)/record['path']).resolve();stat=p.stat();key=(str(p),stat.st_size,stat.st_mtime_ns,record['physical_sha256'])
    if key not in _VERIFIED:
        if sha(p)!=record['physical_sha256']:raise ValueError('artifact hash mismatch: '+str(p))
        _VERIFIED[key]=True
    return p

def iter_table(path):
    path=Path(path)
    if path.exists():yield from csv_rows(path);return
    if path.with_suffix(path.suffix+'.gz').exists():yield from csv_rows(path.with_suffix(path.suffix+'.gz'));return
    doc=manifest(path.parent)
    if doc is None or path.name not in doc['tables']:raise FileNotFoundError(path)
    rec=doc['tables'][path.name]
    if rec['kind']=='shared':yield from csv_rows(referenced(path.parent,rec));return
    if rec['kind']=='decisions_projection':
        contexts={r['signal_session_date']:r for r in csv_rows(referenced(path.parent,rec['contexts']))}
        base=score_base(path.parent,rec['score_base']) if 'score_base' in rec else None
        overrides={(r['signal_session_date'],r['security_id']):json.loads(r['overrides_json']) for r in csv_rows(referenced(path.parent,rec['overrides']))} if 'overrides' in rec else {}
        for detail in csv_rows(referenced(path.parent,rec['details'])):
            key=(detail['signal_session_date'],detail['security_id'])
            static=dict(zip(STATIC,base.get(key,('','','','')))) if base is not None else {}
            combined={**static,**contexts[detail['signal_session_date']],**detail,**overrides.get(key,{})}
            yield {k:combined[k] for k in rec['fields']}
        return
    raise ValueError('unknown artifact encoding')

def read_table(path):return list(iter_table(path))

class LazyRows:
    """Auditor never consumes the big diagnostic tables except their date checks."""
    def __init__(self,path):self.path=path
    def __iter__(self):return iter_table(self.path)

def expanded_bytes(path):
    path=Path(path)
    if path.exists():return path.read_bytes()
    if path.with_suffix(path.suffix+'.gz').exists():
        with gzip.open(path.with_suffix(path.suffix+'.gz'),'rb') as f:return f.read()
    rec=manifest(path.parent)['tables'][path.name]
    if rec['kind']=='shared':
        with gzip.open(referenced(path.parent,rec),'rb') as f:return f.read()
    text=io.StringIO(newline='');w=csv.DictWriter(text,fieldnames=rec['fields']);w.writeheader();w.writerows(iter_table(path))
    return text.getvalue().encode('utf-8')

def table_names(folder):
    folder=Path(folder);names={p.name for p in folder.glob('*.csv')}
    names.update(p.name[:-3] for p in folder.glob('*.csv.gz') if p.name not in {'decision_details.csv.gz','decision_contexts.csv.gz','decision_overrides.csv.gz'})
    doc=manifest(folder)
    if doc:names.update(doc['tables'])
    return sorted(names)

def date_bounds(folder,name,end):
    folder=Path(folder);doc=manifest(folder);rec=doc['tables'].get(name) if doc else None
    # Each immutable shared universe/L file is independently date-scanned once.
    key=(rec['physical_sha256'],name,end) if rec and rec['kind']=='shared' else None
    if key is not None:
        referenced(folder,rec)
        if key in _BOUNDS:return _BOUNDS[key]
    latest='';late=[]
    for row in iter_table(folder/name):
        for k in DATE_KEYS:
            v=row.get(k,'')
            if v:
                latest=max(latest,v[:10])
                if v[:10]>end:late.append((name,k,v))
    result=(latest,late)
    if key is not None:_BOUNDS[key]=result
    return result

def record(path,folder,**extra):
    path=Path(path);return dict(path=os.path.relpath(path,folder),physical_sha256=sha(path),**extra)

def write_lean(out,tables,summary,run_manifest,defaults,shared,failure=None,mode='lean'):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);refs={}
    for name,rows in tables.items():
        if name in {'universe','scores'}:
            rec=shared[name];refs[name+'.csv']=dict(path=os.path.relpath(rec['path'],out),physical_sha256=rec['physical_sha256'],kind='shared',logical_sha256=rec['logical_sha256'])
            continue
        if name=='decisions' and rows:
            common=['signal_session_date','buffer_score','buffer_security_id','industry_counts_after','target_count','frozen_count','is_swap_week']
            contexts={}
            for r in rows:
                day=r['signal_session_date'];values={k:r[k] for k in common}
                if day in contexts and contexts[day]!=values:raise ValueError('nonconstant decision context')
                contexts[day]=values
            fields=fields_for(rows,defaults.get(name,['empty']));detail=[k for k in fields if k not in common or k=='signal_session_date']
            lookup=shared.get('score_lookup');exceptions=[]
            if lookup is not None:
                detail=[k for k in detail if k not in STATIC]
                for r in rows:
                    base=lookup.get((r['signal_session_date'],r['security_id']),{})
                    changed={k:csv_text(r[k]) for k in STATIC if csv_text(r[k])!=csv_text(base.get(k))}
                    if changed:exceptions.append(dict(signal_session_date=r['signal_session_date'],security_id=r['security_id'],overrides_json=json.dumps(changed,ensure_ascii=False,separators=(',',':'))))
            cp=out/'decision_contexts.csv.gz';dp=out/'decision_details.csv.gz'
            write_gzip_csv(cp,contexts.values(),common);write_gzip_csv(dp,rows,detail)
            refs['decisions.csv']=dict(kind='decisions_projection',fields=fields,contexts=record(cp,out),details=record(dp,out))
            if lookup is not None:
                ep=out/'decision_overrides.csv.gz';write_gzip_csv(ep,exceptions,['signal_session_date','security_id','overrides_json'])
                refs['decisions.csv'].update(score_base=refs['scores.csv'],overrides=record(ep,out))
            # Human-facing compact change log; lossless detail above remains available.
            changes=[];previous={}
            for r in rows:
                state=(r['action'],r['reason']);sid=r['security_id']
                if state!=previous.get(sid) or r['action'] in {'buy','exit','sell','retain_frozen'}:changes.append(r)
                previous[sid]=state
            write_gzip_csv(out/'decision_changes.csv.gz',changes,fields)
            continue
        write_gzip_csv(out/(name+'.csv.gz'),rows,fields_for(rows,defaults.get(name,['empty'])))
    if mode=='explore':
        run_manifest=dict(run_manifest,artifact_mode='explore',usage_note='仅供探索筛选，不得作为正式结果或保留段依据')
    else:
        run_manifest=dict(run_manifest,artifact_mode=mode,shared_tables={k:v for k,v in refs.items() if v['kind']=='shared'},decision_encoding='lossless per-week context + per-row detail; compact changes also provided')
    dump(out/'artifacts.json',dict(version=1,tables=refs))
    dump(out/'summary.json',summary);dump(out/'run_manifest.json',run_manifest)
    if failure is not None:dump(out/'last_explainable_state.json',failure)

def export_full(folder,output):
    folder=Path(folder);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    for name in table_names(folder):
        if name=='decision_changes.csv':continue
        (output/name).write_bytes(expanded_bytes(folder/name))
    for name in ['summary.json','run_manifest.json']:(output/name).write_bytes((folder/name).read_bytes())
