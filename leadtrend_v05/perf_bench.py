"""Engineering measurements only. Frozen reference uses the pre-optimization engine."""
import contextlib,cProfile,csv,gzip,hashlib,io,json,os,pstats,shutil,subprocess,sys,time
from collections import defaultdict
from pathlib import Path
from . import paths

SMALL_IDS=['L010_N08_b2_R1','L010_N08_b2_R2','L020_N10_b2_R1','L020_N10_b2_R2',
 'L040_N08_b2_R1','L040_N10_b3_R2','L060_N12_b2_R1','L060_N10_b3_R2',
 'L090_N08_b2_R1','L090_N08_b2_R2','L126_N08_b2_R1','L126_N08_b2_R2']

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def dump(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')

def working_sets():
    """(current, peak) working set of this process in bytes (Windows GetProcessMemoryInfo); (None, None) elsewhere."""
    if sys.platform!='win32':return None,None
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),('PeakWorkingSetSize',ctypes.c_size_t),
            ('WorkingSetSize',ctypes.c_size_t),('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),
            ('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),
            ('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]
    kernel32=ctypes.WinDLL('kernel32',use_last_error=True);psapi=ctypes.WinDLL('psapi',use_last_error=True)
    kernel32.GetCurrentProcess.restype=wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype=wintypes.BOOL
    counters=Counters();counters.cb=ctypes.sizeof(Counters)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
        raise OSError(ctypes.get_last_error(),'GetProcessMemoryInfo failed')
    return int(counters.WorkingSetSize),int(counters.PeakWorkingSetSize)

def peak_working_set():return working_sets()[1]

def available_physical():
    """Available physical memory of the machine in bytes (Windows GlobalMemoryStatusEx); None elsewhere."""
    if sys.platform!='win32':return None
    import ctypes
    class MemoryStatus(ctypes.Structure):
        _fields_=[('dwLength',ctypes.c_ulong),('dwMemoryLoad',ctypes.c_ulong),('ullTotalPhys',ctypes.c_ulonglong),
            ('ullAvailPhys',ctypes.c_ulonglong),('ullTotalPageFile',ctypes.c_ulonglong),('ullAvailPageFile',ctypes.c_ulonglong),
            ('ullTotalVirtual',ctypes.c_ulonglong),('ullAvailVirtual',ctypes.c_ulonglong),('ullAvailExtendedVirtual',ctypes.c_ulonglong)]
    status=MemoryStatus();status.dwLength=ctypes.sizeof(MemoryStatus)
    if not ctypes.WinDLL('kernel32',use_last_error=True).GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError(ctypes.get_last_error(),'GlobalMemoryStatusEx failed')
    return int(status.ullAvailPhys)

class Timer:
    def __init__(self):self.seconds=defaultdict(float);self.calls=defaultdict(int);self.stack=[]
    @contextlib.contextmanager
    def section(self,name):
        record=[name,time.perf_counter(),0.];self.stack.append(record)
        try:yield
        finally:
            elapsed=time.perf_counter()-record[1];self.stack.pop()
            self.seconds[name]+=elapsed-record[2];self.calls[name]+=1
            if self.stack:self.stack[-1][2]+=elapsed
    def wrap(self,obj,name,group):
        old=getattr(obj,name)
        old=getattr(old,'_perf_original',old)
        def measured(*args,**kwargs):
            with self.section(group):return old(*args,**kwargs)
        measured._perf_original=old
        setattr(obj,name,measured)
    def instrument(self):
        from . import stage3 as s,stage3_scoring as sc,stage4_runner as g,stage4_registry as reg,stage4_acceptance as ac
        from . import perf_artifacts as artifacts,perf_cache as cache,dataio
        for obj,name,group in [(g,'load_inputs','data_loading'),(s,'load_inputs','data_loading'),(g,'load_children','data_loading'),
          (s,'load_children','data_loading'),(s,'choose_snapshot','eligibility_and_class_selection'),
          (s,'eligibility_index','eligibility_and_class_selection'),(s,'build_price_index','eligibility_and_class_selection'),
          (s.ClassSelector,'select_for_issuer','eligibility_and_class_selection'),(sc.ScoreEngine,'__init__','scoring'),
          (sc.ScoreEngine,'score','scoring'),(s,'select_portfolio','selection'),(s,'write_artifacts','artifact_writing'),
          (s,'sha256_file','hashing'),(reg,'digest','hashing'),(g,'digest','hashing'),
          (ac,'independent_quotes','acceptance_source_loading'),(artifacts,'write_gzip_csv','artifact_writing'),
          (cache,'sha','hashing'),(artifacts,'sha','hashing'),(dataio,'sha256_file','hashing')]:self.wrap(obj,name,group)
    def result(self):return dict(seconds=dict(self.seconds),calls=dict(self.calls))

def freeze(root):
    root=Path(root);base=root/paths.BASELINE;target=base/'source/leadtrend_v05'
    if target.exists():raise ValueError('reference source already frozen; never replace a baseline')
    target.mkdir(parents=True)
    for p in (root/'leadtrend_v05').glob('*.py'):shutil.copy2(p,target/p.name)
    files={p.relative_to(root).as_posix():sha(p) for p in (root/'leadtrend_v05').glob('*.py')}
    protected={}
    for prefix in [paths.NORMALIZED,paths.RAW]:
        for p in (root/prefix).rglob('*'):
            if p.is_file():protected[p.relative_to(root).as_posix()]=dict(size=p.stat().st_size,mtime_ns=p.stat().st_mtime_ns)
    for p in (root/paths.REGISTRY).glob('*registry*'):
        if p.is_file():protected[p.relative_to(root).as_posix()]=dict(sha256=sha(p))
    dump(base/'freeze.json',dict(code_sha256=files,protected=protected,small_ids=SMALL_IDS,
        hash_seed=0,scope='development only; same current supplemented data for reference and optimized runs',
        purpose='before-optimization engineering reference; not a new parameter experiment'))
    return base

def baseline_entry(root):
    # This executes in a fresh interpreter whose import path contains only the
    # frozen reference package ahead of the live package; edits cannot alter it.
    from .stage4_runner import PreparedGrid,run_point
    from . import stage4_runner
    from .stage4_acceptance import audit
    from .stage3 import run_dev
    from .__main__ import main
    root=Path(root);base=root/paths.BASELINE;timer=Timer();timer.instrument()
    stage4_runner.journal=lambda root,record:None  # never append the historical journal
    start=time.perf_counter()
    with timer.section('data_loading'):ctx=PreparedGrid(root)
    points={p['id']:p for p in ctx.registry['configurations']}
    for cost in [5,10]:
        folder=base/'stage3'/f'cost_{cost}bps';folder.mkdir(parents=True,exist_ok=True)
        with (folder/'command.log').open('w',encoding='utf-8') as log,contextlib.redirect_stdout(log):
            with timer.section('execution_ledger_and_other'):
                code=run_dev(root,root/'config/v05_stage3.json',folder,stress=cost==10)
        if code:raise ValueError('reference Stage3 failed')
        archive(folder,timer)
    with contextlib.redirect_stdout(io.StringIO()):
        code=main(['demo','--config',str(root/'config/v05_bootstrap.json'),'--output',str(base/'synthetic'),'--project-root',str(root),'--run-id','perf-synthetic'])
    if code:raise ValueError('reference synthetic failed')
    archive(base/'synthetic',timer)
    # Profile one run separately; do not contaminate the unprofiled grid timing.
    p=points['L020_N10_b2_R1'];prof=cProfile.Profile();folder=base/'profile_single'
    prof.enable();run_point(ctx,p,5,folder);prof.disable();prof.dump_stats(str(base/'single.prof'))
    with (base/'profile_top.txt').open('w',encoding='utf-8') as f:pstats.Stats(prof,stream=f).strip_dirs().sort_stats('cumulative').print_stats(55)
    with timer.section('acceptance'):a=audit(folder,root,ctx=ctx)
    if a['status']!='PASS':raise ValueError(a)
    archive(folder,timer)
    pre=timer.result();grid_start=time.perf_counter();records=[]
    ordered=[points[c] for c in SMALL_IDS]+[p for p in ctx.registry['configurations'] if p['id'] not in SMALL_IDS]
    small_done=None
    for i,p in enumerate(ordered):
        for cost in [5,10]:
            folder=base/'runs'/p['id']/f'cost_{cost}bps';t=time.perf_counter()
            with timer.section('execution_ledger_and_other'):code=run_point(ctx,p,cost,folder)
            if code:raise ValueError('reference run failed '+p['id'])
            with timer.section('acceptance'):a=audit(folder,root,ctx=ctx)
            if a['status']!='PASS':raise ValueError(a)
            dump(folder/'acceptance.json',a)
            archive(folder,timer)
            records.append(dict(configuration_id=p['id'],cost_bps=cost,elapsed_seconds=time.perf_counter()-t))
        if i==11:small_done=dict(wall_seconds=time.perf_counter()-grid_start,stages=timer.result())
        dump(base/'progress.json',dict(configurations=i+1,cost_runs=len(records),last=p['id'],wall_seconds=time.perf_counter()-grid_start))
        print(json.dumps(dict(configurations=i+1,cost_runs=len(records),last=p['id'])),flush=True)
    end=time.perf_counter();sizes=sum(p.stat().st_size for p in (base/'runs').rglob('*') if p.is_file())
    logical=sum(json.loads(p.read_text(encoding='utf-8'))['logical_bytes'] for p in (base/'runs').glob('*/cost_*/reference.json'))
    dump(base/'measurement.json',dict(status='PASS',hash_seed=0,total_wall_seconds=end-start,grid_wall_seconds=end-grid_start,
        before_grid=pre,small_grid=small_done,stages=timer.result(),runs=records,logical_full_output_bytes=logical,archived_physical_bytes=sizes))
    print('Frozen baseline complete',flush=True)

def archive(folder,timer):
    folder=Path(folder);hashes={};logical=0
    with timer.section('hashing'):
        for p in folder.iterdir():
            if p.is_file():hashes[p.name]=sha(p);logical+=p.stat().st_size
    with timer.section('reference_archival_compression'):
        for p in list(folder.glob('*.csv')):
            out=p.with_suffix('.csv.gz')
            with p.open('rb') as src,out.open('wb') as dst,gzip.GzipFile(fileobj=dst,filename='',mode='wb',compresslevel=1,mtime=0) as z:shutil.copyfileobj(src,z)
            h=hashlib.sha256()
            with gzip.open(out,'rb') as f:
                for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
            if h.hexdigest()!=hashes[p.name]:raise ValueError('reference compression roundtrip failed')
            p.unlink()  # only this newly created, losslessly archived reference copy
    dump(folder/'reference.json',dict(logical_bytes=logical,logical_sha256=hashes))

def baseline(root):
    root=Path(root);base=freeze(root)
    script=base/'execute_frozen.py'
    script.write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).parent/'source'))\nfrom leadtrend_v05.perf_bench import baseline_entry\nif __name__=='__main__':baseline_entry(Path.cwd())\n",encoding='utf-8')
    env=dict(os.environ,PYTHONHASHSEED='0')
    return subprocess.call([sys.executable,'-X','utf8',str(script)],cwd=root,env=env)

def bench(root,mode='baseline',**options):
    if mode=='baseline':return baseline(root)
    from .perf_runner import benchmark
    return benchmark(Path(root),mode=mode,**options)
