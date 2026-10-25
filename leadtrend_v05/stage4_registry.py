"""Create-once registration and fail-closed Stage 4 authority."""
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from . import paths

L_VALUES=[10,20,40,60,90,126]
N_VALUES=[8,10,12]
B_VALUES=[2,3]

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def config_id(L,N,b,R):return f'L{L:03d}_N{N:02d}_b{b}_{R}'
def points():
    return [dict(id=config_id(L,N,b,R),L=L,N=N,k=max(1,math.floor(.3*N+.5)),b=b,rebalance=R)
            for R in ['R1','R2'] for b in B_VALUES for N in N_VALUES for L in L_VALUES]

def register(root):
    root=Path(root);path=root/paths.STAGE4_REGISTRY;seal=path.with_suffix('.sha256')
    if path.exists():return verify(root)
    stage2=root/paths.STAGE2_REGISTRY
    old=json.loads(stage2.read_text(encoding='utf-8'))
    baseline={}
    for cost,folder in [(5,'stage3_dev_run'),(10,'stage3_dev_run/stress_10bps')]:
        baseline[str(cost)]={name:digest(root/paths.RESULTS/folder/name) for name in ['trades.csv','daily_equity.csv','benchmark.csv','summary.json']}
    registry={
        'stage':4,'created_at_utc':datetime.now(timezone.utc).isoformat(),
        'results_seen_before_registration':['Stage3 L20 N10 k3 b2 R1 development at 5 and 10 bps only'],
        'new_parameter_results_seen':False,'all_attempts':72,'costs_are_not_additional_attempts':True,
        'configurations':points(),'costs_bps':[5,10],
        'source_stage2_registry_sha256':digest(stage2),'normalized_sha256':old['data_version'],
        'development':old['development'],'holdout':old['holdout'],
        'initial_capital':100000,'fee_per_trade':1,'tax_assumption':0,
        'rebalance_rules':{
            'R1':'On swap weeks, all tradable positions restored near equal weight using N minus frozen slots; no swap, no trades; same Stage3 execution.',
            'R2':'On swap weeks sell exits only; retained shares unchanged; distribute all available cash after exit fees and reserving one fee per new buy equally among new additions, using adverse fill prices and whole shares. With no additions, leave cash for a later weekly review; no swap means no trades. Initial entry is equal allocation among additions.'},
        'neighbors':'Same b and rebalance; adjacent index in L or N, Manhattan distance exactly one, no diagonals; k follows N.',
        'holdout_rule':{
            'a':'P development annualized return at 10bps strictly exceeds same-period VOO annualized return',
            'b':'At 5bps, strictly more than half of ALL geometric neighbors have annualized return above VOO; incomplete neighbor is not a win',
            'c':'At 10bps, strictly positive calendar-year return difference versus VOO in at least 3 of 2020,2021,2022,2023',
            'd':'P completes both cost scenarios and passes ledger acceptance; no unresolved held event',
            'selection':'Select exactly one candidate maximizing arithmetic mean of own and all neighbors 10bps annualized EXCESS return. No additional return/risk criteria.',
            'incomplete_neighbor_mean':'Any incomplete 10bps neighbor gives extended-real mean -infinity for selection, never inventing a numeric return. Equal means, including all -infinity, tie by ascending configuration ID.',
            'none':'开发段未发现满足事先条件的参数区域; do not run or compute holdout scores, trades, equity, benchmark, or returns',
            'positive':'Exactly one selected configuration, one holdout experiment with both 5bps/10bps; no subsequent parameter change',
            'holdout_account':'Fresh $100000 account at first eligible holdout signal 2024-07-05, next-session open execution. Historical prices may warm scores; no inherited development holdings, cash or tax state.',
            'execution_once':'Create exclusive holdout execution lock BEFORE any holdout calculation. Refuse a second invocation, including failed/incomplete runs; no automatic retries.'},
        'annualization':'CAGR = (ending equity / starting equity)^(365.25 / elapsed calendar days)-1; same interval for VOO; annualized excess = strategy CAGR minus VOO CAGR',
        'calendar_years':'2020-2023: previous year final session close to current year final session close, including all intervening costs/dividends; no fresh restart',
        'subperiods':[{'label':'2019-12_to_2021-12','start':'first actual execution open','end':'2021-12-31'},
                      {'label':'2022-01_to_2024-06','start':'2021-12-31 close (boundary for 2022-01)','end':'2024-06-28'}],
        'subperiod_note':'Slice the same continuous account/benchmark; do not restart or rerank at subperiod boundaries; pick last NYSE session at/before each endpoint.',
        'incomplete_reporting':'Count attempted configuration regardless of completion; retain last explainable state; no complete-period return imputation; not eligible for holdout',
        'engineering_replays':'Deterministic repair/replays of the SAME registered configuration are logged as replays, not hidden new strategy attempts; no parameter additions',
        'baseline_stage3_sha256':baseline,
        'supplemental_events':'Original normalized files never modified. Official new evidence and necessary event overlays must be append-only under stage4 paths with source, acquisition time/hash, and disclosure of results already seen. Unverifiable held events stop the configuration.',
        'limitations':['missing historical member quotes / survivor bias','current industry classification','72 multiple comparisons; no efficacy conclusion or investor parameter recommendation']}
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:f.write(json.dumps(registry,ensure_ascii=False,indent=2)+'\n')
    with seal.open('x',encoding='ascii') as f:f.write(digest(path)+'\n')
    return verify(root)

def verify(root):
    root=Path(root);p=root/paths.STAGE4_REGISTRY
    seal=p.with_suffix('.sha256')
    if not seal.exists() or digest(p)!=seal.read_text(encoding='ascii').strip():raise ValueError('Stage4 registry hash mismatch')
    r=json.loads(p.read_text(encoding='utf-8'))
    if r['configurations']!=points() or r['all_attempts']!=72 or r['costs_bps']!=[5,10]:raise ValueError('unregistered grid')
    if digest(root/paths.STAGE2_REGISTRY)!=r['source_stage2_registry_sha256']:raise ValueError('Stage2 registration changed')
    for name,h in r['normalized_sha256'].items():
        if digest(root/paths.NORMALIZED/name)!=h:raise ValueError('normalized data changed: '+name)
    return r
