"""Investor-authorized data supplement; immutable original registration and universe."""
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from .stage4_registry import digest
from .stage2_enrich import read_csv
from .dataio import SecurityRow, PriceRow
from . import paths

def verify_supplement_seal(path):
    seal=path.with_suffix(path.suffix+'.sha256')
    if seal.exists() and digest(path)!=seal.read_text(encoding='ascii').strip():
        raise ValueError('supplemental manifest hash mismatch: '+path.name)

def scoring_input_supplement(root,inputs,raw_hashes):
    root=Path(root);path=root/paths.SCORE_OVERLAY
    if not path.exists():return inputs
    verify_supplement_seal(path)
    doc=json.loads(path.read_text(encoding='utf-8'))
    for name,h in doc['files'].items():
        if digest(root/name)!=h:raise ValueError('score-event supplemental source changed')
    updates={r['event_id']:r for r in doc['events']}
    events=[]
    for e in inputs.events:
        if e.event_id in updates:
            row=updates[e.event_id]
            e=replace(e,spinoff_valuation='child_raw_close',spinoff_value_per_old_share=row['value_per_old_share'],source=e.source+';verified_auxiliary_child_close='+row['source'])
        events.append(e)
    raw_hashes[path.relative_to(root).as_posix()]=digest(path)
    return replace(inputs,events=events)

def supplement(root, securities, prices, children, raw_hashes, end):
    root=Path(root); manifest=root/paths.DATA_VERSION
    if not manifest.exists():return {}
    verify_supplement_seal(manifest)
    version=json.loads(manifest.read_text(encoding='utf-8'))
    for name,h in version['files'].items():
        if digest(root/name)!=h:raise ValueError('supplement changed: '+name)
    raw_hashes[str(manifest.relative_to(root))]=digest(manifest)
    sid='CHILD:VSCO'
    securities[sid]=SecurityRow(sid,sid,'VSCO',None,None,'common_stock','US_NYSE','UNKNOWN:'+sid,'auxiliary only; no universe membership','USD')
    for p in read_csv(root/version['auxiliary_prices']):
        d=date.fromisoformat(p['session_date'])
        if d<=end:prices[sid,d]=PriceRow(sid,d,float(p['open']),float(p['close']),float(p['volume']),float(p['turnover']),'USD')
    for c in version['children']:
        if date.fromisoformat(c['session_date'])<=end:
            children[c['event_id']].append(dict(c,security_id=sid))
    policy=dict(version['fractional_settlements'])
    extension=root/paths.VSCO_EXTENSION
    if extension.exists():
        verify_supplement_seal(extension)
        extra=json.loads(extension.read_text(encoding='utf-8'))
        if extra['base_sha256']!=digest(manifest):raise ValueError('VSCO extension base changed')
        for name,h in extra['files'].items():
            if digest(root/name)!=h:raise ValueError('VSCO authorization changed')
        policy.update(extra['fractional_settlements'])
        raw_hashes[str(extension.relative_to(root))]=digest(extension)
    return policy

HOLDOUT_AUTHORIZATION='authorization.json'

def holdout_authorized(root):
    """The investor authorizes the one-shot holdout by creating research/v05/holdout/authorization.json."""
    return (Path(root)/paths.HOLDOUT/HOLDOUT_AUTHORIZATION).is_file()

def ensure_no_holdout(root):
    if not holdout_authorized(root):
        raise ValueError('holdout refused: requires fresh Claude review and investor authorization ('+paths.HOLDOUT+'/'+HOLDOUT_AUTHORIZATION+')')
