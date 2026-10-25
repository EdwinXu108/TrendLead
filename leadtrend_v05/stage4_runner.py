"""Sealed-grid execution using the reviewed Stage3 ledger and selection code."""
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date,datetime,timezone
from functools import lru_cache
from pathlib import Path
from collections import defaultdict
import json
import time
from .config import load_config
from .dataio import load_inputs
from .calendar import Calendar
from .stage3 import run_dev,load_children
from .stage3_scoring import ScoreEngine
from .stage4_registry import verify,digest,config_id
from . import paths

class PreparedGrid:
    def __init__(self,root,*,holdout=False,decision=None,claim_token=None):
        self.root=Path(root);self.registry=verify(root);self.registry_hash=digest(self.root/paths.STAGE4_REGISTRY)
        self.scope='holdout_only' if holdout else 'development_only'
        if holdout:
            from .stage4_remediation import ensure_no_holdout
            ensure_no_holdout(root)
            from .stage4_summary import verify_holdout_claim
            verify_holdout_claim(self.root,decision,claim_token)
        self.segment=self.registry['holdout' if holdout else 'development']
        self.allowed_holdout=decision
        self.start=date.fromisoformat(self.segment['start']);self.end=date.fromisoformat(self.segment['end'])
        cfg=load_config(self.root/'config/v05_stage3.json')
        data=load_inputs(cfg,self.root,score_only=True)
        self.full=replace(data,calendar=[c for c in data.calendar if c.session_date<=self.end],
            prices=[p for p in data.prices if p.session_date<=self.end],events=[e for e in data.events if e.effective_session<=self.end],
            snapshots=[s for s in data.snapshots if s.available_at.date()<=self.end and s.holdings_as_of<=self.end])
        del data
        dates=[s.session_date for s in Calendar(self.full.calendar).sessions]
        sec,px,children,raw_hashes=load_children(self.root,self.full,self.end)
        from .stage4_remediation import supplement
        self.fractional_settlements=supplement(self.root,sec,px,children,raw_hashes,self.end)
        from .stage4_remediation import scoring_input_supplement
        self.full=scoring_input_supplement(self.root,self.full,raw_hashes)
        marks=defaultdict(dict)
        for (sid,d),p in px.items():marks[d][sid]=p.close
        engine=ScoreEngine(self.full,dates,self.root,self.end)
        engine.score=lru_cache(maxsize=None)(engine.score)
        self.prepared=(sec,px,children,raw_hashes,marks,engine)

    def authorize(self,raw,end_override):
        if digest(self.root/paths.STAGE4_REGISTRY)!=self.registry_hash:raise ValueError('registry changed during execution')
        match=next((p for p in self.registry['configurations'] if p['id']==raw.get('configuration_id')),None)
        if match is None or any(raw.get(k)!=match[k] for k in ('L','N','k','b','rebalance')):raise ValueError('configuration not in sealed grid')
        if raw.get('slippage_bps') not in self.registry['costs_bps']:raise ValueError('unregistered cost')
        if raw.get('initial_capital')!=100000 or raw.get('fee_per_trade')!=1:raise ValueError('unregistered account assumption')
        if raw['start_date']!=self.segment['start'] or raw['end_date']!=self.segment['end'] or end_override is not None:
            raise ValueError('unregistered segment / holdout boundary')
        if self.scope=='holdout_only' and match['id']!=self.allowed_holdout:raise ValueError('holdout configuration not selected')
        return self.start,self.end

def journal(root,record):
    record={'time_utc':datetime.now(timezone.utc).isoformat(),**record}
    path=Path(root)/paths.LOGS/'stage4_execution_journal.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')

def run_point(ctx,point,cost,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    raw=json.loads((ctx.root/'config/v05_stage3.json').read_text(encoding='utf-8'))
    raw.update({k:point[k] for k in ('L','N','k','b','rebalance')})
    raw.update(configuration_id=point['id'],slippage_bps=cost,start_date=ctx.segment['start'],end_date=ctx.segment['end'],
               authorization='Stage4 sealed grid / conditional one-shot holdout',engineering_defaults_unvalidated=True)
    cfg=folder/'config.json';cfg.write_text(json.dumps(raw,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    journal(ctx.root,dict(operation='start',configuration_id=point['id'],cost_bps=cost,scope=ctx.scope,registry_sha256=ctx.registry_hash))
    start=time.monotonic()
    try:
        with (folder/'command.log').open('w',encoding='utf-8') as f,redirect_stdout(f):
            code=run_dev(ctx.root,cfg,folder,_stage4_context=ctx)
    except Exception as exc:
        # Infrastructure faults remain visible, never converted into a valid return.
        code=3
        (folder/'infrastructure_error.json').write_text(json.dumps({'exception':type(exc).__name__,'message':str(exc)},indent=2)+'\n')
    journal(ctx.root,dict(operation='finish',configuration_id=point['id'],cost_bps=cost,scope=ctx.scope,exit_code=code,elapsed_seconds=time.monotonic()-start))
    return code

def run_grid(root,*,output=None,workers=1,artifacts='lean',force=False,verify_level='daily'):
    """New outputs only; old grids and the sealed registry remain untouched."""
    from .perf_runner import run_grid as optimized_grid
    return optimized_grid(root,output=output,workers=workers,artifacts=artifacts,force=force,verify_level=verify_level)
