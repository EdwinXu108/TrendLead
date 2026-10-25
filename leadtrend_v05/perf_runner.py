"""Spawn workers, independently audited incremental development experiments."""
import contextlib,gc,hashlib,io,json,multiprocessing,os,subprocess,sys,threading,time
from pathlib import Path
from .perf_bench import Timer,SMALL_IDS,dump,sha,working_sets,peak_working_set,available_physical
from .perf_cache import identity,load_cache,worker_cache
from .perf_artifacts import expanded_bytes
from .stage4_registry import verify
from . import paths

_CTX=None;_TIMER=None

def fixed_seed_main(argv):
    if os.environ.get('PYTHONHASHSEED')=='0':return None
    return subprocess.call([sys.executable,'-X','utf8','-m','leadtrend_v05',*argv],env=dict(os.environ,PYTHONHASHSEED='0'))

def initialize(cache_path,key):
    global _CTX,_TIMER
    _TIMER=Timer();_TIMER.instrument()
    with _TIMER.section('data_loading'):_CTX=worker_cache(cache_path,key)

def run_key(key,p,cost,mode,legacy=False,verify_level='daily'):
    return hashlib.sha256(json.dumps(dict(cache_key=key,point=p,cost=cost,artifacts=mode,legacy=legacy,verification_level=verify_level),sort_keys=True).encode()).hexdigest()

def accepted(folder,key):
    if (Path(folder)/'BYTE_MISMATCH_STOP.json').exists():return False
    p=Path(folder)/'completion.json'
    if not p.exists():return False
    try:
        d=json.loads(p.read_text(encoding='utf-8'))
        level=d.get('verification_level')
        # daily/regression require the independent replay PASS; explore records
        # NOT_RUN because that replay is intentionally not executed. Records
        # without a level predate the level field and follow the PASS rule.
        if level is None:ok=d['acceptance']['status']=='PASS'
        else:ok=(level in {'daily','regression'} and d['acceptance']['status']=='PASS') or (level=='explore' and d['acceptance']['status']=='NOT_RUN')
        if d['run_key']!=key or d['status']!='completed' or not ok:return False
        if not all((p.parent/n).is_file() and sha(p.parent/n)==h for n,h in d['physical_sha256'].items()):return False
        from .perf_artifacts import manifest,referenced
        doc=manifest(p.parent)
        if doc:
            for rec in doc['tables'].values():
                if rec['kind']=='shared':referenced(p.parent,rec)
                elif 'score_base' in rec:referenced(p.parent,rec['score_base'])
        return True
    except (KeyError,ValueError,OSError):return False

def explore_check(folder):
    """Explore-level in-engine consistency; tolerances are the acceptance constants."""
    from .stage4_acceptance import TOLERANCE,CASH_FLOOR
    from .perf_artifacts import read_table
    folder=Path(folder)
    summary=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    failures=[]
    if summary.get('status')!='completed':failures.append('run incomplete')
    identity=float(summary.get('max_asset_identity_error') or 0.0)
    equity=read_table(folder/'daily_equity.csv')
    recon=max((abs(float(r.get('cash_reconciliation_error') or 0)) for r in equity),default=0.0)
    min_cash=min((float(r['cash']) for r in equity),default=0.0)
    if identity>TOLERANCE:failures.append('asset identity')
    if recon>TOLERANCE:failures.append('daily cash reconciliation')
    if min_cash<CASH_FLOOR:failures.append('negative cash')
    return dict(status='PASS' if not failures else 'FAIL',max_asset_identity_error=identity,
        max_daily_cash_reconciliation_error=recon,min_daily_cash=min_cash,
        tolerances=dict(asset_identity=TOLERANCE,cash_reconciliation=TOLERANCE,cash_floor=CASH_FLOOR),failures=failures)

def worker_record(result):
    """Process measurements travel with the returned record only; completion.json stays as written."""
    return dict(result,worker_pid=os.getpid(),peak_working_set_bytes=peak_working_set())

def one_job(job):
    from .stage3 import run_dev
    from .stage4_acceptance import audit
    ctx=_CTX;timer=_TIMER;p,cost,folder,mode,legacy,verify_level=job
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    if (folder/'BYTE_MISMATCH_STOP.json').exists():raise ValueError('previous byte mismatch requires investigation before rerunning')
    raw=json.loads((ctx.root/'config/v05_stage3.json').read_text(encoding='utf-8'))
    raw.update({k:p[k] for k in ('L','N','k','b','rebalance')})
    raw.update(configuration_id=p['id'],slippage_bps=cost,start_date=ctx.segment['start'],end_date=ctx.segment['end'],
        authorization='Stage4 sealed grid / conditional one-shot holdout',engineering_defaults_unvalidated=True)
    for field in ('signal','selection','experiment'):
        if field in p:raw[field]=p[field]
    dump(folder/'config.json',raw);ctx.artifact_mode=mode;ctx.data_scope='legacy' if legacy else 'uniform'
    before=dict(timer.seconds);start=time.perf_counter()
    with (folder/'command.log').open('w',encoding='utf-8') as log,contextlib.redirect_stdout(log):
        with timer.section('execution_ledger_and_other'):
            code=run_dev(ctx.root,folder/'config.json',folder,_stage4_context=None if legacy else ctx,_performance=ctx)
    if code:
        result=dict(configuration_id=p['id'],cost_bps=cost,status='incomplete',exit_code=code,verification_level=verify_level,run_key=run_key(ctx.cache_key,p,cost,mode,legacy,verify_level))
        dump(folder/'completion.json',result);return worker_record(result)
    explore=verify_level=='explore'
    with timer.section('acceptance'):
        a=explore_check(folder) if explore else audit(folder,ctx.root,ctx=ctx)
    if explore:
        # No independent replay artifact is written; the check travels inside completion.json.
        result_status='completed' if a['status']=='PASS' else 'failed'
        acceptance={'status':'NOT_RUN'}
    else:
        dump(folder/'acceptance.json',a)
        if a['status']!='PASS':raise ValueError('independent acceptance failed '+str(folder)+': '+str(a['failures']))
        result_status='completed';acceptance=a
    manifest_path=folder/'run_manifest.json'
    doc=json.loads(manifest_path.read_text(encoding='utf-8'))
    doc['verification_level']=verify_level
    dump(manifest_path,doc)
    if verify_level=='regression':
        with timer.section('byte_comparison'):
            ref=ctx.root/paths.BASELINE/'stage3'/f'cost_{cost}bps' if legacy else ctx.root/paths.BASELINE/'runs'/p['id']/f'cost_{cost}bps'
            proof=compare_reference(ctx.root,folder,ref)
            dump(folder/'byte_comparison.json',proof)
    with timer.section('hashing'):
        hashes={f.name:sha(f) for f in folder.iterdir() if f.is_file() and f.name!='completion.json'}
    stages={k:timer.seconds[k]-before.get(k,0) for k in timer.seconds}
    result=dict(configuration_id=p['id'],cost_bps=cost,status=result_status,
        exit_code=0,run_key=run_key(ctx.cache_key,p,cost,mode,legacy,verify_level),verification_level=verify_level,cache_key=ctx.cache_key,physical_sha256=hashes,acceptance=acceptance,
        wall_seconds=time.perf_counter()-start,stage_seconds=stages,artifact_mode=mode,data_scope=ctx.data_scope)
    if explore:result['explore_check']=a
    dump(folder/'completion.json',result)
    return worker_record(result)

def safe_output(root,path):
    root=Path(root).resolve();path=Path(path)
    if not path.is_absolute():path=root/path
    path=path.resolve()
    # New runs always go to the scratch area; research/ and data/ are never written by a run.
    work=root/paths.WORK
    if not path.is_relative_to(work):raise ValueError('output must be inside '+paths.WORK+'/; research/ and data/ are protected')
    if path==work or path==root/paths.RUNS:raise ValueError('choose a new child output directory')
    return path

class _MemorySampler:
    """Lowest available physical memory seen while jobs run; sampled twice a second, reads no data."""
    def __init__(self):
        self.low=None;self._stop=threading.Event();self._thread=threading.Thread(target=self._sample,daemon=True)
    def _sample(self):
        while not self._stop.is_set():
            value=available_physical()
            if value is not None and (self.low is None or value<self.low):self.low=value
            self._stop.wait(0.5)
    def start(self):self._thread.start()
    def stop(self):self._stop.set();self._thread.join()

AUTO_RESERVE_BYTES=3*2**30  # always left free for other programs
AUTO_WORKER_CAP=4  # B1 timings, hot cache: 1→4 workers 3.2× faster, 4→8 only 1.24× more
# Measured worker peaks in B1 (explore ≈243 MB, daily ≈1002 MB) with a margin.
WORKER_PEAK_BYTES={'explore':260*2**20,'daily':1100*2**20}

def auto_workers(available,per_worker,cpus,*,cap=AUTO_WORKER_CAP,reserve=AUTO_RESERVE_BYTES):
    """Largest worker count that keeps `reserve` free, fits the measured per-worker peak, the cap and the CPUs."""
    if available is None:return 1
    by_memory=max(0,available-reserve)//per_worker
    return int(max(1,min(cap,by_memory,cpus)))

def run_grid(root,*,output=None,workers=1,artifacts='lean',force=False,ids=None,tag='grid',legacy=False,compare=None,verify_level='daily',cold_cache=False):
    root=Path(root);start=time.perf_counter();reg=verify(root)
    if workers is None:workers=1
    if compare is not None:verify_level='regression' if compare else 'daily'
    if verify_level not in {'daily','regression','explore'}:raise ValueError('verification level must be daily, regression or explore')
    auto_record=None
    if workers=='auto':
        mode='explore' if verify_level=='explore' else 'daily'
        available=available_physical();cpus=os.cpu_count() or 1
        workers=auto_workers(available,WORKER_PEAK_BYTES[mode],cpus)
        auto_record=dict(mode=mode,available_bytes=available,per_worker_peak_bytes=WORKER_PEAK_BYTES[mode],reserve_bytes=AUTO_RESERVE_BYTES,
            cap=AUTO_WORKER_CAP,cpus=cpus,chosen=workers)
    if not 1<=workers<=8:raise ValueError('workers must be 1..8')
    if artifacts not in {'lean','full'}:raise ValueError('unknown artifacts mode')
    # Explore always writes its own small screening artifact set.
    job_artifacts='explore' if verify_level=='explore' else artifacts
    output=safe_output(root,output or (paths.RUNS+'/explore_grid' if verify_level=='explore' else paths.RUNS+'/grid'))
    if force and output.exists():
        target=output.with_name(output.name+'_archive_'+str(time.time_ns()))
        if not target.resolve().is_relative_to(root.resolve()):raise ValueError('unsafe archive')
        os.replace(output,target)  # preserve every prior artifact, never delete
    output.mkdir(parents=True,exist_ok=True)
    t=time.perf_counter();doc=identity(root,'legacy' if legacy else 'uniform');identity_seconds=time.perf_counter()-t
    previous=output/'measurement.json'
    if previous.exists():
        prior=json.loads(previous.read_text(encoding='utf-8'))
        if prior['cache_key']!=doc['key'] or prior['artifacts']!=job_artifacts:
            target=output.with_name(output.name+'_archive_'+str(time.time_ns()))
            if not target.resolve().is_relative_to(root.resolve()):raise ValueError('unsafe version archive')
            os.replace(output,target);output.mkdir(parents=True)
    points=[p for p in reg['configurations'] if ids is None or p['id'] in ids]
    if ids and len(points)!=len(ids):raise ValueError('unregistered configuration')
    jobs=[];reused=[];hash_start=time.perf_counter()
    for p in points:
        for cost in [5,10]:
            folder=output/p['id']/f'cost_{cost}bps';key=run_key(doc['key'],p,cost,job_artifacts,legacy,verify_level)
            if not force and accepted(folder,key):reused.append(dict(configuration_id=p['id'],cost_bps=cost,status='reused',folder=str(folder)))
            else:
                if (folder/'BYTE_MISMATCH_STOP.json').exists():raise ValueError('byte mismatch stop requires investigation and explicit --force')
                if folder.exists() and any(folder.iterdir()):
                    target=output.parent/(output.name+'_partial_archive_'+str(time.time_ns()))/p['id']/f'cost_{cost}bps'
                    if not target.resolve().is_relative_to(root.resolve()):raise ValueError('unsafe incomplete archive')
                    target.parent.mkdir(parents=True,exist_ok=True);os.replace(folder,target)
                jobs.append((p,cost,str(folder),job_artifacts,legacy,verify_level))
    reuse_hash_seconds=time.perf_counter()-hash_start
    # Jobs run grouped by window length, so a worker keeps one window table; the runs list is written in registry order.
    registry_order={(p['id'],c):i*2+j for i,p in enumerate(points) for j,c in enumerate([5,10])}
    jobs.sort(key=lambda job:job[0]['L'])
    cache_load_seconds=0.;hot_cache_load_seconds=None;cache_hit=True;results=[]
    sampler=_MemorySampler();start_available=available_physical()
    if jobs:
        sampler.start()
        timer=Timer();timer.instrument()
        t=time.perf_counter()
        cache_root=root/paths.COLD_CACHE/f'{tag}_{time.time_ns()}' if cold_cache else None
        with timer.section('shared_cache_prepare'):ctx,cache_hit=load_cache(root,doc,cache_root=cache_root)
        cache_load_seconds=time.perf_counter()-t;preparation_stages=timer.result()
        if cache_hit:hot_cache_load_seconds=cache_load_seconds
        else:
            # A cold build is followed by a real hot load of the same immutable cache, the path every later run takes.
            ctx=None;gc.collect()
            t=time.perf_counter();ctx,_=load_cache(root,doc,cache_root=cache_root);hot_cache_load_seconds=time.perf_counter()-t
        actual_cache_dir=ctx.cache_folder
        if workers==1:
            global _CTX,_TIMER
            _CTX=ctx;_TIMER=timer
            for i,job in enumerate(jobs,1):
                r=one_job(job);results.append(r)
                dump(output/'progress.json',dict(completed=i,total=len(jobs),last=r['configuration_id']))
                print(json.dumps(dict(completed=i,total=len(jobs),last=r['configuration_id'])),flush=True)
        else:
            # Workers deserialize their own immutable cache. Keeping the full
            # parent copy wastes memory and can force paging on 16 GB machines.
            cache_path=str(ctx.cache_folder/'prepared.pkl');cache_key=ctx.cache_key
            del ctx
            gc.collect()
            spawn=multiprocessing.get_context('spawn')
            with spawn.Pool(workers,initializer=initialize,initargs=(cache_path,cache_key)) as pool:
                for i,r in enumerate(pool.imap_unordered(one_job,jobs),1):
                    results.append(r);dump(output/'progress.json',dict(completed=i,total=len(jobs),last=r['configuration_id']))
                    print(json.dumps(dict(completed=i,total=len(jobs),last=r['configuration_id'])),flush=True)
        sampler.stop()
    else:
        # No unpickle, no price/score/ledger calculation in the all-reused path.
        previous_report=json.loads((output/'measurement.json').read_text(encoding='utf-8'))
        cache_dir=Path(previous_report.get('cache_folder',str(root/paths.CACHE/doc['key'])));info=json.loads((cache_dir/'cache.json').read_text(encoding='utf-8'))
        if info['identity']!=doc:raise ValueError('reuse cache identity mismatch')
        # All actual shared references were verified above; prepared.pkl is not
        # read or needed when every independently accepted result is reusable.
        actual_cache_dir=cache_dir
        preparation_stages={}
    results.sort(key=lambda r:registry_order[(r['configuration_id'],r['cost_bps'])])
    stages={}
    for r in results:
        for name,value in r.get('stage_seconds',{}).items():stages[name]=stages.get(name,0)+value
    end_hash_start=time.perf_counter()
    if identity(root,'legacy' if legacy else 'uniform')['key']!=doc['key']:
        raise ValueError('input/code changed during execution; results not certified, rebuild under the new key')
    final_identity_hash_seconds=time.perf_counter()-end_hash_start
    actual_bytes=sum(p.stat().st_size for p in output.rglob('*') if p.is_file())
    cache_bytes=sum(p.stat().st_size for p in actual_cache_dir.rglob('*') if p.is_file())
    worker_peaks={}
    for r in results:
        if r.get('peak_working_set_bytes') is not None:
            pid=str(r['worker_pid']);worker_peaks[pid]=max(worker_peaks.get(pid,0),r['peak_working_set_bytes'])
    prepared_path=actual_cache_dir/'prepared.pkl'
    report=dict(status='PASS' if all(r['status']=='completed' for r in results) else 'INCOMPLETE',tag=tag,
        cache_key=doc['key'],data_scope='legacy' if legacy else 'uniform',workers=workers,artifacts=job_artifacts,verification_level=verify_level,
        cache_folder=str(actual_cache_dir),
        configuration_count=len(points),cost_runs=len(points)*2,executed=len(results),reused=len(reused),cache_hit=cache_hit,
        identity_hash_seconds=identity_seconds,reuse_hash_seconds=reuse_hash_seconds,cache_load_or_build_seconds=cache_load_seconds,
        hot_cache_load_seconds=hot_cache_load_seconds,prepared_pkl_bytes=prepared_path.stat().st_size if prepared_path.exists() else None,
        parent_pid=os.getpid(),parent_peak_working_set_bytes=peak_working_set(),parent_current_working_set_bytes=working_sets()[0],
        start_available_physical_bytes=start_available,min_available_physical_bytes=sampler.low,workers_auto=auto_record,
        worker_peak_working_set_bytes=worker_peaks,
        final_identity_hash_seconds=final_identity_hash_seconds,
        preparation_stages=preparation_stages,
        stage_seconds_sum=stages,wall_seconds=time.perf_counter()-start,output_bytes=actual_bytes,shared_cache_bytes=cache_bytes,
        cache_included_bytes=actual_bytes+cache_bytes,no_holdout_computed=True,runs=results,reuse_records=reused)
    dump(output/'measurement.json',report)
    measurement=root/paths.MEASUREMENTS/f'{tag}_{time.time_ns()}.json';dump(measurement,report)
    print(json.dumps({k:v for k,v in report.items() if k not in {'runs','reuse_records'}},ensure_ascii=False,indent=2))
    return report

def compare_reference(root,folder,reference):
    """Every CSV round-tripped; entire summary bytes, not selected numeric fields."""
    folder=Path(folder);reference=Path(reference);spec=json.loads((reference/'reference.json').read_text(encoding='utf-8'))
    results={}
    for name,h in spec['logical_sha256'].items():
        if name.endswith('.csv'):
            raw=expanded_bytes(folder/name);results[name]=hashlib.sha256(raw).hexdigest()==h
        elif name=='summary.json':results[name]=sha(folder/name)==h
    reference_a=reference/'acceptance.json'
    if reference_a.exists():
        original=json.loads(reference_a.read_text(encoding='utf-8'));actual=json.loads((folder/'acceptance.json').read_text(encoding='utf-8'))
        results['independent_acceptance_all_fields']=original==actual
    result=dict(folder=str(folder),reference=str(reference),status='PASS' if all(results.values()) else 'FAIL',files=results)
    if result['status']!='PASS':
        dump(folder/'BYTE_MISMATCH_STOP.json',result);raise ValueError('BYTE MISMATCH: stop and investigate, baseline may not be replaced: '+str(folder))
    return result

def benchmark(root,mode,workers=1,artifacts='lean',force=False,output=None,cold_cache=False,verify_level='daily'):
    if mode=='synthetic':
        from .__main__ import main
        out=safe_output(root,output or Path(root)/paths.RUNS/'bench'/'synthetic')
        with contextlib.redirect_stdout(io.StringIO()):
            code=main(['demo','--config',str(Path(root)/'config/v05_bootstrap.json'),'--output',str(out),'--project-root',str(root),'--run-id','perf-synthetic'])
        if code:return code
        if verify_level=='regression':
            result=compare_reference(root,out,Path(root)/paths.BASELINE/'synthetic');dump(out/'byte_comparisons.json',result)
        manifest_path=out/'run_manifest.json';doc=json.loads(manifest_path.read_text(encoding='utf-8'));doc['verification_level']=verify_level;dump(manifest_path,doc)
        return 0
    legacy=mode=='legacy'
    small=not mode.startswith('grid')
    out=Path(output) if output else Path(root)/paths.RUNS/'bench'/mode
    if not out.is_absolute():out=Path(root)/out
    ids=['L020_N10_b2_R1'] if legacy else SMALL_IDS if small else None
    report=run_grid(root,output=out,workers=workers,artifacts=artifacts,force=force,ids=ids,tag=mode,verify_level=verify_level,legacy=legacy,cold_cache=cold_cache)
    if report['status']!='PASS':return 2
    if report['executed']==0 or verify_level in {'daily','explore'}:return 0
    comparisons=[]
    for p in verify(root)['configurations']:
        if ids and p['id'] not in ids:continue
        for cost in [5,10]:
            ref=Path(root)/paths.BASELINE/'stage3'/f'cost_{cost}bps' if legacy else Path(root)/paths.BASELINE/'runs'/p['id']/f'cost_{cost}bps'
            proof=out/p['id']/f'cost_{cost}bps/byte_comparison.json'
            comparisons.append(json.loads(proof.read_text(encoding='utf-8')) if proof.exists() else compare_reference(root,proof.parent,ref))
    dump(out/'byte_comparisons.json',comparisons)
    return 0

def run_single_legacy(root,config_path,output,stress=False,end_override=None):
    from .stage3 import guard_development
    root=Path(root);raw=json.loads(Path(config_path).read_text(encoding='utf-8'))
    reg=json.loads((root/paths.STAGE2_REGISTRY).read_text(encoding='utf-8'))
    if stress:raw['slippage_bps']=10
    guard_development(raw,reg,end_override)  # keep the reviewed boundary before cache/data access
    if Path(config_path).resolve()!=(root/'config/v05_stage3.json').resolve() or end_override:
        raise ValueError('cached lean run-dev requires canonical full development config; use --artifacts full for other diagnostics')
    folder=safe_output(root,output);doc=identity(root,'legacy');ctx,_=load_cache(root,doc)
    global _CTX,_TIMER
    _CTX=ctx;_TIMER=Timer();_TIMER.instrument()
    p=next(p for p in ctx.registry['configurations'] if p['id']=='L020_N10_b2_R1')
    result=one_job((p,raw['slippage_bps'],str(folder),'lean',True,'daily'))
    return 0 if result['status']=='completed' else 2
