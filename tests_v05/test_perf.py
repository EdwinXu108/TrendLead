"""Engineering invariants: content keys, reversible encoding and strict reuse."""
import csv,hashlib,io,json,os
from pathlib import Path
import pytest
from leadtrend_v05.perf_cache import identity
from leadtrend_v05.perf_bench import sha,dump
from leadtrend_v05.perf_artifacts import write_lean,expanded_bytes,write_gzip_csv,logical_sha,record,date_bounds
from leadtrend_v05.perf_runner import accepted,safe_output

def bytes_csv(rows):
    s=io.StringIO(newline='');w=csv.DictWriter(s,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows);return s.getvalue().encode('utf-8')

def fixture_root(root):
    d=root/'data/v05/normalized';d.mkdir(parents=True)
    (d/'spinoff_child_prices.csv').write_text('child_ticker\nCHILD\n')
    (d/'prices.csv').write_text('price\n1\n')
    c=root/'leadtrend_v05';c.mkdir();(c/'scoring.py').write_text('formula = 1\n')
    return d,c

def test_content_key_invalidates_data_even_same_size_same_mtime(tmp_path,monkeypatch):
    monkeypatch.setenv('PYTHONHASHSEED','0');d,c=fixture_root(tmp_path)
    a=identity(tmp_path);p=d/'prices.csv';stat=p.stat();p.write_text('price\n2\n');os.utime(p,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    b=identity(tmp_path);assert a['key']!=b['key']
    assert identity(tmp_path)['key']==b['key']

def test_content_key_invalidates_code_config_and_new_optional_source(tmp_path):
    d,c=fixture_root(tmp_path);a=identity(tmp_path)['key'];(c/'scoring.py').write_text('formula = 2\n');b=identity(tmp_path)['key'];assert a!=b
    p=tmp_path/'config';p.mkdir();(p/'v05_stage3.json').write_text('{"class_selection_turnover_days":21}')
    assert identity(tmp_path)['key']!=b
    before=identity(tmp_path)['key'];raw=tmp_path/'data/v05/raw/opend_kline_none_v1';raw.mkdir(parents=True)
    (raw/'NONE_CHILD_2019-05-01_2026-12-31.csv').write_text('close\n1\n');assert identity(tmp_path)['key']!=before

def decision(day,sid='a',score=1.23456789012345):
    return dict(signal_session_date=day,security_id=sid,issuer_id='issuer',industry='行业, "A"',score=score,rank=1,
        action='retain',reason='buffer',buffer_score=.123456789012345,buffer_security_id='b',industry_counts_after='{"行业, \\"A\\"": 1}',target_count=8,frozen_count=0,is_swap_week=0)

def test_lossless_gzip_and_decision_projection_roundtrip(tmp_path):
    rows=[decision('2020-01-03'),decision('2020-01-10'),decision('2020-01-17','child',None)]
    tables=dict(decisions=rows,trades=[dict(price=1.0,shares=3,cash=-4.123456789012345,note='换股, "x"')])
    write_lean(tmp_path,tables,{'return':.1},{},{},{})
    for name,rs in tables.items():assert expanded_bytes(tmp_path/(name+'.csv'))==bytes_csv(rs)

def test_shared_score_projection_handles_unscored_child_and_frozen_override(tmp_path):
    common=[dict(signal_session_date='2020-01-03',security_id='a',issuer_id='issuer',industry='行业, "A"',score=1.23456789012345,rank=1)]
    p=tmp_path/'scores.csv.gz';write_gzip_csv(p,common,list(common[0]));ref=dict(path=p,physical_sha256=sha(p),logical_sha256=logical_sha(p))
    scores={(x['signal_session_date'],x['security_id']):x for x in common}
    rows=[decision('2020-01-03'),decision('2020-01-03','child',None),decision('2020-01-10','a',0.)]
    # Different days have separate contexts. Child static metadata is an override.
    out=tmp_path/'out';write_lean(out,dict(scores=common,decisions=rows),{}, {},{},dict(scores=ref,score_lookup=scores))
    assert expanded_bytes(out/'decisions.csv')==bytes_csv(rows)
    assert expanded_bytes(out/'scores.csv')==bytes_csv(common)

def test_date_scan_preserves_each_violation_not_only_maximum(tmp_path):
    rows=[dict(session_date='2024-07-05',signal_session_date='2024-07-05'),dict(session_date='2024-07-08',signal_session_date='2024-07-05')]
    write_gzip_csv(tmp_path/'x.csv.gz',rows,list(rows[0]))
    latest,violations=date_bounds(tmp_path,'x.csv','2024-06-28');assert latest=='2024-07-08' and len(violations)==4

def test_incremental_reuse_requires_exact_key_acceptance_and_hash(tmp_path):
    p=tmp_path/'summary.json';p.write_text('{"value":1}')
    doc=dict(run_key='key',status='completed',acceptance={'status':'PASS'},physical_sha256={'summary.json':sha(p)})
    dump(tmp_path/'completion.json',doc);assert accepted(tmp_path,'key');assert not accepted(tmp_path,'different')
    p.write_text('{"value":2}');assert not accepted(tmp_path,'key')
    dump(tmp_path/'BYTE_MISMATCH_STOP.json',{});assert not accepted(tmp_path,'key')

def test_outputs_only_in_scratch_area(tmp_path):
    with pytest.raises(ValueError,match='protected'):safe_output(tmp_path,'research/v05/results/new')
    with pytest.raises(ValueError,match='protected'):safe_output(tmp_path,'data/v05/normalized')
    with pytest.raises(ValueError,match='new child'):safe_output(tmp_path,'work')
    with pytest.raises(ValueError,match='new child'):safe_output(tmp_path,'work/runs')
    assert safe_output(tmp_path,'work/runs/new')==tmp_path/'work/runs/new'


def test_verification_cli_defaults_and_explicit_regression():
    from leadtrend_v05.__main__ import build_parser
    parser=build_parser()
    for command in ['bench','run-grid']:
        assert parser.parse_args([command]).verify=='daily'
        assert parser.parse_args([command,'--verify','regression']).verify=='regression'
        with pytest.raises(SystemExit):parser.parse_args([command,'--verify','unchecked'])


def test_verification_levels_never_share_a_completion_key():
    from leadtrend_v05.perf_runner import run_key
    assert run_key('data',{'id':'p'},5,'lean',verify_level='daily')!=run_key('data',{'id':'p'},5,'lean',verify_level='regression')


def test_daily_audits_without_oracle_regression_runs_both(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from leadtrend_v05 import perf_runner as runner,stage3,stage4_acceptance
    from leadtrend_v05.perf_bench import Timer
    (tmp_path/'config').mkdir();dump(tmp_path/'config/v05_stage3.json',{})
    ctx=SimpleNamespace(root=tmp_path,segment={'start':'2019-11-29','end':'2024-06-28'},cache_key='key')
    monkeypatch.setattr(runner,'_CTX',ctx);monkeypatch.setattr(runner,'_TIMER',Timer())
    calls=[]
    def run(root,config,out,**kwargs):
        dump(out/'run_manifest.json',{});dump(out/'summary.json',{'total_return':.123456789012345})
        for name in ['trades','daily_equity','benchmark','positions','receivables']:(out/(name+'.csv')).write_bytes(b'value\r\n0.123456789012345\r\n')
        return 0
    def audit(*args,**kwargs):calls.append('audit');return {'status':'PASS'}
    def compare(*args):calls.append('oracle');return {'status':'PASS'}
    monkeypatch.setattr(stage3,'run_dev',run);monkeypatch.setattr(stage4_acceptance,'audit',audit);monkeypatch.setattr(runner,'compare_reference',compare)
    point=dict(id='test',L=20,N=10,k=3,b=2,rebalance='R1')
    for level in ['daily','regression']:
        folder=tmp_path/level;result=runner.one_job((point,5,str(folder),'lean',False,level))
        assert result['verification_level']==level
        assert json.loads((folder/'run_manifest.json').read_text())['verification_level']==level
    assert calls==['audit','audit','oracle']
    for name in ['trades.csv','daily_equity.csv','benchmark.csv','positions.csv','receivables.csv','summary.json']:
        assert (tmp_path/'daily'/name).read_bytes()==(tmp_path/'regression'/name).read_bytes()


def test_uniform_reviewed_matrix_is_protected(tmp_path):
    with pytest.raises(ValueError,match='protected'):safe_output(tmp_path,'research/v05/results/stage4_unified/runs')
