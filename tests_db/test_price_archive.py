"""Price safety checks: wrong-company series, new issuer start, NONE-only, publication time."""
import hashlib,json
from pathlib import Path
import pytest
from leadtrend_db import price_data as P


def test_publication_after_nyse_open_starts_next_session():
    cal=['2020-03-06','2020-03-09','2020-03-10']
    assert P.active_start('2020-03-06T14:29:59Z',cal)=='2020-03-06'
    assert P.active_start('2020-03-06T14:30:00Z',cal)=='2020-03-09'
    # DST starts March 8: open is now 13:30 UTC.
    assert P.active_start('2020-03-09T13:29:59Z',cal)=='2020-03-09'
    assert P.active_start('2020-03-09T13:30:00Z',cal)=='2020-03-10'


def _toy(tmp_path,monkeypatch,close=10,autype='None',start=None):
    blobs={}
    def add(key,value):blobs[key]=json.dumps(value).encode();return key
    calendar=add('cal',dict(data=[dict(time=d,trade_date_type='WHOLE') for d in ['2013-12-30','2013-12-31','2014-01-02']]))
    bars=add('bars',dict(ret=0,params=dict(autype=autype),data=[dict(time_key=d+' 00:00:00',open=close,high=close,low=close,close=close,volume=1,turnover=close) for d in ['2013-12-30','2013-12-31','2014-01-02']]))
    index=add('index',dict(calendar=[calendar],entries=dict(TEST=dict(klines=[bars]))))
    config=tmp_path/'prices.json';config.write_text(json.dumps(dict(index_url=index,start='2012-12-01',end='2014-01-02')))
    monkeypatch.setattr(P,'CONFIG',config)
    monkeypatch.setattr(P,'CALENDAR_CONFIG',tmp_path/'no-calendar.json')
    monkeypatch.setattr(P,'load_overrides',lambda _: [dict(override_kind='issuer_start',key='0000000099',value=start)] if start else [])
    class Store:
        records={k:dict(sha256=hashlib.sha256(v).hexdigest(),path=k) for k,v in blobs.items()}
        def _read(self,key):return blobs[key]
    t=dict(issuer_links=[],securities=[dict(security_id='S',issuer_id='CIK:0000000099',class_code='COM')],
       strategy_securities=[dict(strategy_eligible=1,current_ticker='TEST',security_id='S',current_security_id='S',version_id='V')],
       holdings=[dict(security_id='S',issuer_id='CIK:0000000099',shares=100,value_usd=1000,snapshot_id='V')],
       pool_versions=[dict(version_id='V',report_period='2013-12-31',available_at_utc='2014-01-01T12:00:00Z')],review_items=[])
    return P.assemble(t,Store(),None),t


def test_wrong_company_price_anchor_quarantines_segment(tmp_path,monkeypatch):
    rows,t=_toy(tmp_path,monkeypatch,close=100)
    assert rows['prices_daily']==[]
    assert rows['price_lineage'][0]['status']=='quarantined'
    assert any(r['review_type']=='price_identity' for r in t['review_items'])


def test_fund_anchor_accepts_original_none_bars(tmp_path,monkeypatch):
    rows,_=_toy(tmp_path,monkeypatch)
    assert len(rows['prices_daily'])==3
    assert all(r['adj_close'] is None for r in rows['prices_daily'])


def test_new_merged_entity_never_inherits_pre_start_bars(tmp_path,monkeypatch):
    rows,_=_toy(tmp_path,monkeypatch,start='2013-12-31')
    assert [r['session_date'] for r in rows['prices_daily']]==['2013-12-31','2014-01-02']


def test_vendor_adjusted_input_is_rejected(tmp_path,monkeypatch):
    with pytest.raises(ValueError,match='non-NONE'):_toy(tmp_path,monkeypatch,autype='QFQ')
