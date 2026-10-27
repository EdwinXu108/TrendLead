"""Checks for database-bound public entry, cache integrity and scoring safeguards."""
import json
import os
import sqlite3
import pytest
from leadtrend_bt import cache
from leadtrend_bt.engine import Backtest, DataGapError, HoldoutError
from leadtrend_db import api
from tests_bt.test_engine import base_cfg


def database(path, content):
    c = sqlite3.connect(path)
    c.execute("create table meta(key text primary key,value text)")
    c.executemany("insert into meta values (?,?)", [("content_sha256",content),
        ("economic_data_version","development_release_v1")])
    c.commit(); c.close()


def test_same_path_replacement_changes_database_identity_and_connection(tmp_path):
    db=tmp_path/'database.sqlite'; other=tmp_path/'next.sqlite'
    database(db,'old'); database(other,'new')
    first=api.database_identity(db)
    api.close(db)  # Windows cannot replace an open SQLite file
    os.replace(other,db)
    second=api.database_identity(db)
    assert first != second and second[-1]=='new'
    api.close(db)


def test_cache_checks_all_outputs_and_rejects_changed_inputs(tmp_path,monkeypatch):
    db=tmp_path/'database.sqlite'; database(db,'v1')
    cfg=base_cfg(protocol='research_v1',run_id='x')
    calls=[]
    def stub(cfg,out_root,db_path):
        calls.append(1); out=out_root/cfg['run_id']; out.mkdir(parents=True,exist_ok=True)
        for name in cache.OUTPUTS: (out/name).write_text(json.dumps({'value':len(calls)}),encoding='utf8')
        return {'summary':{'value':len(calls)}}
    monkeypatch.setattr(cache,'run',stub)
    out=tmp_path/'runs'
    assert cache.cached_run(cfg,db,out)['status']=='computed'
    assert cache.cached_run(cfg,db,out)['status']=='reused'
    (out/'x'/'positions.csv').write_text('corrupt',encoding='utf8')
    assert cache.cached_run(cfg,db,out)['status']=='computed'
    new=tmp_path/'new.sqlite'; database(new,'v2'); api.close(db); os.replace(new,db)
    assert cache.cached_run(cfg,db,out)['status']=='computed'
    assert len(calls)==3
    api.close(db)


def test_unresolved_action_in_score_window_is_not_silently_ranked():
    bt=Backtest(base_cfg(protocol='research_v1',L=1))
    bt.full_calendar=['2024-01-02','2024-01-03']
    bt.tri={'A':{'2024-01-02':1,'2024-01-03':1.1}}
    bt.actions={'A':[{'action_date':'2024-01-03','needs_review':1}]}
    pool={'securities':[{'security_id':'A','issuer_id':'I','amount_cents':1}]}
    with pytest.raises(DataGapError,match='unresolved action'):
        bt._scored('2024-01-03',pool)


def test_default_protocol_and_reserved_window():
    assert Backtest(base_cfg()).protocol in ('research_v1','legacy_v1') # fixture explicitly selects legacy
    cfg=base_cfg(); cfg.pop('protocol',None)
    assert Backtest(cfg).protocol == 'research_v1'
    with pytest.raises(HoldoutError): Backtest(dict(cfg,end='2024-07-05'))


def test_cached_top_ten_market_does_not_skip_full_pool_gap(monkeypatch):
    from leadtrend_bt import engine
    day, nxt = '2024-01-02', '2024-01-03'
    amounts = {'A': {'issuer_id':'IA','amount_cents':100}, 'B': {'issuer_id':'IB','amount_cents':1}}
    snapshot = {'calendar':[day,nxt], 'full_calendar':[day,nxt], 'signal_days':[day],
        'pools':{day:{'amounts':amounts,'company_amounts':{'IA':100,'IB':1}}},
        'pool_days':{day:'version'}, 'tri':{}, 'opens':{'A':{day:1}}, 'closes':{},
        'issuer':{'A':'IA','B':'IB'},'amounts':{},'actions':{},'action_sids':[],
        'spin_value':{},'continuity_links':[],'economic_data_version':'development_release_v1'}
    monkeypatch.setattr(Backtest,'_cache_key',lambda self:'fixture')
    monkeypatch.setitem(engine._MARKET_CACHE,'fixture',snapshot)
    top=Backtest(base_cfg(protocol='research_v1',mode='pool',top_n=1))
    top.load()
    whole=Backtest(base_cfg(protocol='research_v1',mode='pool'))
    with pytest.raises(DataGapError,match='B 2024-01-02'):
        whole.load()
