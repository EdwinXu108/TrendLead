"""Content-addressed, development-only immutable precomputation.

The cache keeps prices as numpy arrays and the shared score/universe tables as lossless gzip CSV. The pickle
holds only small objects; runtime views (quotes, marks, signal-week frames) are rebuilt from those files on
every load and are never pickled.
"""
import json,math,os,pickle,shutil,time,sys,platform
from collections import defaultdict
from datetime import datetime,timezone
from dataclasses import replace
from pathlib import Path
import numpy as np
from .perf_bench import dump,sha
from .perf_artifacts import write_gzip_csv,logical_sha,csv_rows
from .stage4_runner import PreparedGrid
from .stage4_registry import verify,digest
from . import paths

def identity(root,scope='uniform'):
    root=Path(root);files=set((root/'leadtrend_v05').glob('*.py'))
    files.update((root/paths.NORMALIZED).glob('*.csv'))
    for n in ['config/v05_stage3.json',paths.STAGE2_REGISTRY,paths.STAGE4_REGISTRY,paths.STAGE4_REGISTRY_SEAL,
      paths.SCORING_BLOCKS,paths.WEEKLY_COVERAGE,paths.STAGE2_ACCEPTANCE]:
        files.add(root/n)
    from .stage2_enrich import read_csv
    for c in read_csv(root/paths.NORMALIZED/'spinoff_child_prices.csv'):
        files.add(root/paths.CHILD_KLINES/f'NONE_{c["child_ticker"].replace(".","_")}_2019-05-01_2026-12-31.csv')
    if scope=='uniform':
        for n in [paths.DATA_VERSION,paths.SCORE_OVERLAY,paths.VSCO_EXTENSION]:
            p=root/n;files.add(p);files.add(p.with_suffix(p.suffix+'.sha256'))
            if p.exists():
                doc=json.loads(p.read_text(encoding='utf-8'))
                files.update(root/f for f in doc.get('files',{}))
    # Missing optional files are part of the identity as well.
    hashes={p.relative_to(root).as_posix():sha(p) if p.exists() else None for p in sorted(files)}
    doc=dict(version=1,project_root=str(root.resolve()),scope=scope,hash_seed=os.environ.get('PYTHONHASHSEED'),inputs_and_code=hashes,
      python=sys.version,numpy=np.__version__,implementation=platform.python_implementation(),platform=platform.system(),pickle_protocol=pickle.HIGHEST_PROTOCOL,
      windows=[10,20,40,60,90,126] if scope=='uniform' else [20],boundary='2019-11-29..2024-06-28')
    import hashlib
    doc['key']=hashlib.sha256(json.dumps(doc,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return doc

class OptimizedContext(PreparedGrid):
    """Read-only prepared object. Inherits the reviewed sealed-grid authorization."""
    def __init__(self,*a,**kw):raise TypeError('use load_cache; holdout construction is not supported')
    def score_lookup(self,L):
        if not hasattr(self,'_score_lookup'):self._score_lookup={}
        if L not in self._score_lookup:
            self._score_lookup[L]={(r['signal_session_date'],r['security_id']):r for frame in self.frames.values() for r in frame['windows'][L]['scores']}
        return self._score_lookup[L]

def build_context(root,scope,check_fast=False):
    root=Path(root)
    if scope=='uniform':ctx=PreparedGrid(root)
    else:
        from .config import load_config
        from .dataio import load_inputs
        from .calendar import Calendar
        from .stage3 import load_children
        from .stage3_scoring import ScoreEngine
        from datetime import date
        ctx=OptimizedContext.__new__(OptimizedContext);ctx.root=root;ctx.registry=verify(root);ctx.registry_hash=digest(root/paths.STAGE4_REGISTRY)
        ctx.scope='development_only';ctx.segment=ctx.registry['development'];ctx.start=date.fromisoformat(ctx.segment['start']);ctx.end=date.fromisoformat(ctx.segment['end']);ctx.allowed_holdout=None
        full=load_inputs(load_config(root/'config/v05_stage3.json'),root,score_only=True)
        ctx.full=replace(full,calendar=[x for x in full.calendar if x.session_date<=ctx.end],prices=[x for x in full.prices if x.session_date<=ctx.end],events=[x for x in full.events if x.effective_session<=ctx.end],snapshots=[x for x in full.snapshots if x.available_at.date()<=ctx.end and x.holdings_as_of<=ctx.end])
        dates=[x.session_date for x in Calendar(ctx.full.calendar).sessions]
        sec,px,children,raw_hashes=load_children(root,ctx.full,ctx.end);marks=defaultdict(dict)
        for (sid,d),p in px.items():marks[d][sid]=p.close
        ctx.prepared=(sec,px,children,raw_hashes,marks,ScoreEngine(ctx.full,dates,root,ctx.end));ctx.fractional_settlements={}
    ctx.__class__=OptimizedContext
    ctx.prepared[-1].__dict__.pop('score',None)  # remove the process-local lru wrapper before pickling
    ctx.frames=precompute_frames(ctx,[10,20,40,60,90,126] if scope=='uniform' else [20],check_fast=check_fast)
    ctx.verified_normalized=True
    return ctx

class FastScores:
    """Closed-form momentum for cells whose result the TRI series alone determines.

    A cell is answered here only when ScoreEngine.score would take the plain ratio branch: the L+1 window
    is complete on the TRI axis, no scoring block applies, the security has no split or spin-off event, no
    event in its history is unpriced, and the window start is non-zero. Any other cell returns None and is
    scored by ScoreEngine itself.
    """
    EXCLUDED={'split','reverse_split','spinoff'}
    def __init__(self,engine):
        self.pos=engine.pos;self.tri={};self.first_block={};self.corporate=set();self.first_bad={}
        n=len(engine.sessions)
        for sid,res in engine.tri.items():
            if res.invalid_from is not None:continue
            v=np.full(n,np.nan)
            for d,x in res.tri.items():v[self.pos[d]]=x
            self.tri[sid]=v
        for sid in self.tri:
            blocks=engine.blocks.get(sid,[])
            if blocks:self.first_block[sid]=min(since for since,_ in blocks)
            events=engine.events.get(sid,[])
            if any(e.event_type in self.EXCLUDED for e in events):self.corporate.add(sid)
            px=engine.prices.get(sid,{})
            bad=[e.effective_session for e in events if e.effective_session not in px]
            if bad:self.first_bad[sid]=min(bad)
    def score(self,sid,d,L):
        v=self.tri.get(sid);i=self.pos.get(d)
        if v is None or i is None or i<L:return None
        if sid in self.corporate:return None
        first=self.first_block.get(sid)
        if first is not None and first<=d:return None
        bad=self.first_bad.get(sid)
        if bad is not None and bad<=d:return None
        if np.isnan(v[i-L:i+1]).any():return None
        start=float(v[i-L]);end=float(v[i])
        if start==0:return None
        score=end/start-1.0
        if not math.isfinite(score):return None
        return score,end,start,L+1,''

def precompute_frames(ctx,windows,check_fast=False):
    from .calendar import Calendar
    from .eligibility import ClassSelector,build_price_index,eligibility_index
    from .selection import Candidate
    from .universe import choose_snapshot
    data=ctx.full;cfg=data.config;elig=eligibility_index(data);selector=ClassSelector(cfg,build_price_index(data.prices));frames={}
    engine=ctx.prepared[-1];fast=FastScores(engine);stats=dict(fast=0,fallback=0)
    for session in Calendar(data.calendar).week_signal_sessions():
        d=session.session_date
        if not ctx.start<=d<=ctx.end:continue
        choice=choose_snapshot(data,session.close_time_utc)
        if choice.snapshot is None:raise ValueError('missing weekly snapshot')
        by_issuer=defaultdict(list)
        for member in choice.members:
            item=elig[member.security_id]
            if item.eligible:by_issuer[item.issuer_id].append(item)
        chosen={selector.select_for_issuer(issuer,rs,d) for issuer,rs in sorted(by_issuer.items())};chosen.discard(None)
        iso=d.isoformat();universe=[]
        for member in choice.members:
            sid=member.security_id;item=elig[sid]
            universe.append(dict(signal_session_date=iso,security_id=sid,snapshot_id=choice.snapshot.snapshot_id,
                holdings_as_of=choice.snapshot.holdings_as_of.isoformat(),available_at=choice.snapshot.available_at.isoformat(),
                issuer_id=item.issuer_id,eligible=int(item.eligible),class_selected=int(sid in chosen),eligibility_reason=item.reason))
        frame=dict(chosen=frozenset(chosen),universe=universe,windows={})
        for L in windows:
            scores=[];candidates=[];unscored={}
            for sid in sorted(chosen):
                cell=fast.score(sid,d,L)
                if cell is None:cell=engine.score(sid,d,L);stats['fallback']+=1
                else:
                    stats['fast']+=1
                    if check_fast:
                        slow=engine.score(sid,d,L)
                        if not (slow==cell and repr(slow)==repr(cell)):raise AssertionError(f'fast path differs from ScoreEngine: {sid} {iso} L={L} {cell} {slow}')
                score,tend,tstart,n,reason=cell;item=elig[sid]
                scores.append(dict(signal_session_date=iso,security_id=sid,issuer_id=item.issuer_id,industry=item.industry,L=L,
                    window_sessions=n,tri_start=tstart,tri_end=tend,score=score,unscorable_reason=reason))
                if score is not None:candidates.append(Candidate(sid,item.issuer_id,item.industry,score))
                else:unscored[sid]=reason
            ranked={c.security_id:i+1 for i,c in enumerate(sorted(candidates,key=lambda c:c.sort_key))}
            for row in scores:row['rank']=ranked.get(row['security_id'])
            frame['windows'][L]=dict(scores=scores,candidates=candidates,unscored=unscored,ranked=ranked)
        frames[d]=frame
    ctx.cell_stats=stats
    return frames

def write_price_arrays(folder,px):
    """Price table as three dense arrays over (security, session); absent cells are False in presence."""
    sids=list(dict.fromkeys(sid for sid,_ in px));days=sorted({d for _,d in px})
    si={s:i for i,s in enumerate(sids)};dj={d:j for j,d in enumerate(days)}
    opn=np.zeros((len(sids),len(days)));cls=np.zeros(opn.shape);pre=np.zeros(opn.shape,dtype=bool)
    for (sid,d),p in px.items():
        i=si[sid];j=dj[d];opn[i,j]=p.open;cls[i,j]=p.close;pre[i,j]=True
    for name,arr in (('px_open',opn),('px_close',cls),('px_present',pre)):np.save(Path(folder)/(name+'.npy'),arr)
    return dict(sids=sids,days=days)

class _Quote:
    __slots__=('open','close')
    def __init__(self,open_,close):self.open=open_;self.close=close

class QuoteView:
    """(security, session) membership and quotes over the price arrays, with the former dict's KeyError."""
    def __init__(self,axes,opn,cls,pre):
        self.sid={s:i for i,s in enumerate(axes['sids'])};self.day={d:j for j,d in enumerate(axes['days'])}
        self.opn=opn;self.cls=cls;self.pre=pre
    def _at(self,key):
        sid,d=key;i=self.sid.get(sid);j=self.day.get(d)
        if i is None or j is None or not self.pre[i,j]:return None
        return i,j
    def __contains__(self,key):return self._at(key) is not None
    def __getitem__(self,key):
        ij=self._at(key)
        if ij is None:raise KeyError(key)
        i,j=ij;return _Quote(float(self.opn[i,j]),float(self.cls[i,j]))

class _DayMarks:
    def __init__(self,quotes,j):self.quotes=quotes;self.j=j
    def _at(self,sid):
        i=self.quotes.sid.get(sid)
        if i is None or self.j is None or not self.quotes.pre[i,self.j]:return None
        return i
    def __contains__(self,sid):return self._at(sid) is not None
    def __getitem__(self,sid):
        i=self._at(sid)
        if i is None:raise KeyError(sid)
        return float(self.quotes.cls[i,self.j])

class MarkView:
    """Closing marks by session; an unknown session reads as an empty mapping, as the former defaultdict did."""
    def __init__(self,quotes):self.quotes=quotes
    def __getitem__(self,d):return _DayMarks(self.quotes,self.quotes.day.get(d))

def _num(s):return None if s=='' else float(s)
def _int(s):return None if s=='' else int(s)

class SharedFrames:
    """Signal-week frames rebuilt from the immutable shared tables.

    The chosen sets are read once; each window length and the universe rows are read only when first used,
    so a run holds the one window it needs. Values round-trip exactly (repr floats, integer counts).
    """
    def __init__(self,shared):
        self.shared=shared;self._chosen=None;self._universe=None;self._table_L=None;self._table=None
    def _load_chosen(self):
        chosen=defaultdict(set)
        for r in csv_rows(self.shared['universe']['path']):
            day=chosen[r['signal_session_date']]
            if r['class_selected']=='1':day.add(r['security_id'])
        self._chosen={k:frozenset(v) for k,v in chosen.items()}
    def weeks(self):
        if self._chosen is None:self._load_chosen()
        return sorted(self._chosen)
    def universe(self,iso):
        if self._universe is None:
            rows=defaultdict(list)
            for r in csv_rows(self.shared['universe']['path']):
                rows[r['signal_session_date']].append(dict(signal_session_date=r['signal_session_date'],security_id=r['security_id'],
                    snapshot_id=r['snapshot_id'],holdings_as_of=r['holdings_as_of'],available_at=r['available_at'],issuer_id=r['issuer_id'],
                    eligible=int(r['eligible']),class_selected=int(r['class_selected']),eligibility_reason=r['eligibility_reason']))
            self._universe=rows
        return self._universe.get(iso,[])
    def window_table(self,L):
        # Only the window in use is held; switching L releases the previous table before the next is read.
        if self._table_L!=L:
            from .selection import Candidate
            self._table_L=None;self._table=None
            table={}
            for r in csv_rows(self.shared[L]['path']):
                row=dict(signal_session_date=r['signal_session_date'],security_id=r['security_id'],issuer_id=r['issuer_id'],industry=r['industry'],
                    L=int(r['L']),window_sessions=_int(r['window_sessions']),tri_start=_num(r['tri_start']),tri_end=_num(r['tri_end']),
                    score=_num(r['score']),unscorable_reason=r['unscorable_reason'],rank=_int(r['rank']))
                w=table.setdefault(row['signal_session_date'],dict(scores=[],candidates=[],unscored={},ranked={}))
                w['scores'].append(row)
                if row['score'] is None:w['unscored'][row['security_id']]=row['unscorable_reason']
                else:
                    w['candidates'].append(Candidate(row['security_id'],row['issuer_id'],row['industry'],row['score']))
                    w['ranked'][row['security_id']]=row['rank']
            self._table_L=L;self._table=table
        return self._table
    def window(self,L,iso):
        return self.window_table(L).get(iso) or dict(scores=[],candidates=[],unscored={},ranked={})
    def __getitem__(self,d):
        iso=d.isoformat()
        if iso not in self._chosen_keys():raise KeyError(d)
        return _Week(self,iso)
    def _chosen_keys(self):
        if self._chosen is None:self._load_chosen()
        return self._chosen
    def values(self):
        return [_Week(self,iso) for iso in self.weeks()]

class _Week:
    def __init__(self,store,iso):self.store=store;self.iso=iso
    def __getitem__(self,key):
        if key=='chosen':return self.store._chosen_keys().get(self.iso,frozenset())
        if key=='universe':return self.store.universe(self.iso)
        if key=='windows':return _Windows(self.store,self.iso)
        raise KeyError(key)

class _Windows:
    def __init__(self,store,iso):self.store=store;self.iso=iso
    def __getitem__(self,L):return self.store.window(L,self.iso)

def attach(ctx,folder):
    """Runtime views over the immutable files of one cache folder; nothing attached here is pickled."""
    folder=Path(folder);sec,_,children,raw_hashes,_,_=ctx.prepared
    quotes=QuoteView(ctx.axes,np.load(folder/'px_open.npy',mmap_mode='r'),np.load(folder/'px_close.npy',mmap_mode='r'),
        np.load(folder/'px_present.npy',mmap_mode='r'))
    ctx.prepared=(sec,quotes,children,raw_hashes,MarkView(quotes),None)
    # Experiment signal tables are recorded in cache.json after the build; they join the shared map here.
    shared=dict(ctx.shared)
    info=json.loads((folder/'cache.json').read_text(encoding='utf-8')) if (folder/'cache.json').exists() else {}
    for key,rec in info.get('signal_tables',{}).items():
        shared[key]=dict(path=folder/rec['path'],physical_sha256=rec['physical_sha256'],logical_sha256=rec['logical_sha256'])
    ctx.shared=shared
    ctx.frames=SharedFrames(shared)
    return ctx

def _table_by_week(path):
    per=defaultdict(dict)
    for r in csv_rows(path):per[r['signal_session_date']][r['security_id']]=r
    return per

def _cell(row):
    return (_num(row['score']),_num(row['tri_end']),_num(row['tri_start']),_int(row['window_sessions']),row['unscorable_reason'])

def ensure_signal_tables(root,doc,signals,cache_root=None):
    """Write the score table of every signal not yet in this cache, and record it in cache.json.

    Momentum with skip 0 on a cached window uses the existing table. Composite tables are derived from their
    component tables. Momentum with a skip, or on a window the cache does not hold, needs the engine
    and is built with the full context once.
    """
    from datetime import date
    from .eligibility import eligibility_index
    from .signals import OVERLAY_KINDS,SCORE_FIELDS,canonical,composite_cells,momentum_cell,normalize,signal_key,week_rows
    root=Path(root);folder=(Path(cache_root) if cache_root else root/paths.CACHE)/doc['key']
    info=json.loads((folder/'cache.json').read_text(encoding='utf-8'))
    if info['identity']!=doc:raise ValueError('cache identity mismatch')
    tables=info.setdefault('signal_tables',{});files=info.setdefault('files',{})
    weeks=defaultdict(dict)
    for r in csv_rows(folder/'universe.csv.gz'):
        day=weeks[r['signal_session_date']]
        if r['class_selected']=='1':day[r['security_id']]=r['issuer_id']
    state={}
    def momentum_rows(n,key):
        if 'engine' not in state:
            built=build_context(root,doc['scope'])
            state['engine']=built.prepared[-1];state['elig']=eligibility_index(built.full)
        engine,elig=state['engine'],state['elig']
        rows=[]
        for iso in sorted(weeks):
            d=date.fromisoformat(iso);sids=sorted(weeks[iso])
            cells={s:momentum_cell(engine,s,d,n['L'],n['skip']) for s in sids}
            rowinfo={s:(elig[s].issuer_id,elig[s].industry) for s in sids}
            rows.extend(week_rows(iso,sids,cells,rowinfo,n['L'],key))
        return rows
    def context():
        if 'engine' not in state:
            built=build_context(root,doc['scope'])
            state['engine']=built.prepared[-1];state['elig']=eligibility_index(built.full);state['members']=built.full.members
        return state['engine'],state['elig'],state['members']
    def overlay_rows(n,key):
        from .overlay_signals import daily_log_volatility,peer,sector_demeaned,size_tilt,vol_adjusted,voo_weight
        from .sectors import load_sector_map,sector_key
        engine,elig,members=context()
        base=_table_by_week(ensure({'kind':'momentum','L':n['L'],'skip':0}))
        second=_table_by_week(ensure({'kind':'momentum','L':n['L2'],'skip':0})) if n['kind']=='peer' else None
        sector_map=load_sector_map(root)
        weight={(m.snapshot_id,m.security_id):(m.weight or 0.0) for m in members}
        members_by_week=defaultdict(list)
        for r in csv_rows(folder/'universe.csv.gz'):members_by_week[r['signal_session_date']].append(r)
        rows=[]
        for iso in sorted(weeks):
            d=date.fromisoformat(iso);sids=sorted(weeks[iso])
            cells90={s:_cell(base[iso][s]) for s in sids}
            sectors={s:sector_key(elig[s].industry,sector_map) for s in sids}
            # Issuer VOO weight: the snapshot used this week, summed over every member class of the issuer.
            issuer_w=defaultdict(float)
            for r in members_by_week[iso]:issuer_w[r['issuer_id']]+=weight.get((r['snapshot_id'],r['security_id']),0.0)
            issuer_of={r['security_id']:r['issuer_id'] for r in members_by_week[iso]}
            by_security={s:issuer_w.get(issuer_of.get(s),0.0) for s in sids}
            if n['kind']=='peer':
                out=peer(cells90,{s:_cell(second[iso][s]) for s in sids if s in second.get(iso,{})},sectors,n['weight'],n['min_peers'])
            elif n['kind']=='sector_demeaned':
                out=sector_demeaned(cells90,sectors)
            elif n['kind']=='vol_adjusted':
                sigma={s:daily_log_volatility(engine.tri[s].tri,engine.sessions,engine.pos[d],n['vol_L']) for s in sids if s in engine.tri and cells90[s][0] is not None}
                out=vol_adjusted(cells90,sigma)
            elif n['kind']=='size_tilt':
                out=size_tilt(cells90,by_security,n['weight'])
            else:
                out=voo_weight(cells90,by_security)
            rowinfo={s:(elig[s].issuer_id,elig[s].industry) for s in sids}
            rows.extend(week_rows(iso,sids,out,rowinfo,n['L'],key))
        return rows
    def ensure(sig):
        n=normalize(sig)
        if n['kind']=='momentum' and n['skip']==0 and n['L'] in doc['windows']:
            return folder/f"scores_L{n['L']}.csv.gz"
        key=signal_key(n);name=f'scores_{key}.csv.gz'
        if key in tables:return folder/tables[key]['path']
        if n['kind']=='momentum':
            rows=momentum_rows(n,key);L=n['L']
        elif n['kind'] in OVERLAY_KINDS:
            rows=overlay_rows(n,key);L=n['L']
        elif n['kind']=='composite':
            parts=[_table_by_week(ensure(c)) for c in n['components']];L=n['components'][0]['L']
            rows=[]
            for iso in sorted(weeks):
                sids=sorted(weeks[iso])
                cells=[{s:_cell(p[iso][s]) for s in sids} for p in parts]
                out=composite_cells(sids,cells,n['weights'])
                rowinfo={s:(parts[0][iso][s]['issuer_id'],parts[0][iso][s]['industry']) for s in sids}
                rows.extend(week_rows(iso,sids,out,rowinfo,L,key))
        p=folder/name;write_gzip_csv(p,rows,SCORE_FIELDS+['signal_key'])
        files[name]=sha(p);tables[key]=dict(path=name,physical_sha256=sha(p),logical_sha256=logical_sha(p),signal=n,canonical=canonical(n))
        return p
    paths_out=[ensure(s) for s in signals]
    dump(folder/'cache.json',info)
    return paths_out

CACHE_KEEP=3

def prune_cache(root,base,keep=CACHE_KEEP):
    """Keep the newest `keep` directories directly under base (by modification time); delete the rest.

    base is work/cache (children are cache folders) or work/cache_cold (children are cold namespaces). Nothing else
    is touched. Every removal, and every kept folder, is appended to work/logs/cache_prune.log as one JSON line.
    """
    root=Path(root);base=Path(base)
    if base.resolve() not in {(root/paths.CACHE).resolve(),(root/paths.COLD_CACHE).resolve()}:
        raise ValueError('pruning is limited to work/cache and work/cache_cold')
    if not base.is_dir():return []
    dirs=[p for p in base.iterdir() if p.is_dir() and not p.is_symlink()]
    dirs.sort(key=lambda p:(p.stat().st_mtime,p.name),reverse=True)
    removed,failed=[],[]
    for p in dirs[keep:]:
        size=sum(f.stat().st_size for f in p.rglob('*') if f.is_file())
        try:
            shutil.rmtree(p);removed.append(dict(path=p.relative_to(root).as_posix(),bytes=size))
        except OSError as err:failed.append(dict(path=p.relative_to(root).as_posix(),error=str(err)))
    entry=dict(time=datetime.now(timezone.utc).isoformat(timespec='seconds'),base=base.relative_to(root).as_posix(),keep=keep,
        kept=[p.relative_to(root).as_posix() for p in dirs[:keep]],removed=removed,failed=failed)
    logs=root/paths.LOGS;logs.mkdir(parents=True,exist_ok=True)
    with (logs/'cache_prune.log').open('a',encoding='utf-8') as f:f.write(json.dumps(entry,ensure_ascii=False)+'\n')
    return removed

def build_cache(root,doc,cache_root=None):
    root=Path(root);folder=(Path(cache_root) if cache_root else root/paths.CACHE)/doc['key'];folder.mkdir(parents=True,exist_ok=True)
    start=time.perf_counter();ctx=build_context(root,doc['scope']);ctx.cache_key=doc['key'];ctx.cache_folder=folder
    shared={}
    for L in doc['windows']:
        if not shared:
            rows=[r for frame in ctx.frames.values() for r in frame['universe']];p=folder/'universe.csv.gz'
            write_gzip_csv(p,rows,list(rows[0]));shared['universe']=dict(path=p,logical_sha256=logical_sha(p),physical_sha256=sha(p))
        rows=[r for frame in ctx.frames.values() for r in frame['windows'][L]['scores']];p=folder/f'scores_L{L}.csv.gz'
        write_gzip_csv(p,rows,list(rows[0]));shared[L]=dict(path=p,logical_sha256=logical_sha(p),physical_sha256=sha(p))
    ctx.shared=shared
    # Prices move to arrays and the scoring engine, frames and oversized member lists leave the pickle.
    sec,px,children,raw_hashes,_,_=ctx.prepared
    ctx.axes=write_price_arrays(folder,px)
    benchmark=ctx.full.config.benchmark_security_id
    ctx.full=replace(ctx.full,prices=[p for p in ctx.full.prices if p.security_id==benchmark],members=[])
    ctx.prepared=(sec,None,children,raw_hashes,None,None);ctx.frames=None
    tmp=folder/'prepared.pkl.tmp'
    with tmp.open('wb') as f:pickle.dump(ctx,f,protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp,folder/'prepared.pkl')
    files={p.name:sha(p) for p in folder.iterdir() if p.is_file() and p.name!='cache.json'}
    dump(folder/'cache.json',dict(identity=doc,files=files,build_seconds=time.perf_counter()-start,immutable=True,
        cell_stats=ctx.cell_stats))
    # After a successful build: the default namespace keeps its newest three cache folders; a cold namespace keeps
    # its newest three namespaces. Any other location is left alone.
    if cache_root is None:prune_cache(root,root/paths.CACHE)
    elif Path(cache_root).resolve().parent==(root/paths.COLD_CACHE).resolve():prune_cache(root,root/paths.COLD_CACHE)
    return attach(ctx,folder)

def load_cache(root,doc=None,scope='uniform',cache_root=None):
    root=Path(root);doc=doc or identity(root,scope);folder=(Path(cache_root) if cache_root else root/paths.CACHE)/doc['key'];manifest=folder/'cache.json'
    if not manifest.exists():return build_cache(root,doc,cache_root),False
    info=json.loads(manifest.read_text(encoding='utf-8'))
    if info['identity']!=doc:raise ValueError('cache identity mismatch')
    for n,h in info['files'].items():
        if sha(folder/n)!=h:raise ValueError('cache content corrupted: '+n)
    with (folder/'prepared.pkl').open('rb') as f:ctx=pickle.load(f)
    return attach(ctx,folder),True

def worker_cache(path,expected):
    # Parent has already hashed the entire immutable cache exactly once.
    path=Path(path)
    with path.open('rb') as f:ctx=pickle.load(f)
    if ctx.cache_key!=expected:raise ValueError('worker cache identity mismatch')
    return attach(ctx,path.parent)
