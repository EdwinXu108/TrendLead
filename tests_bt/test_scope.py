import pytest
from leadtrend_db import api
from leadtrend_bt.ma_walk import stock_list

V1='data/db/leadtrend_db_v1.sqlite'
V2='data/db/leadtrend_db_v2.sqlite'

def test_scoped_pool_subset():
    for day in ('2014-02-28','2016-01-08','2024-06-28'):
        full=api.pool_at(day,V2); scoped=api.pool_at(day,V2,api.SCOPE_TOP50)
        assert 48 <= len(scoped['companies']) <= 50
        assert {x['issuer_id'] for x in scoped['companies']} <= {x['issuer_id'] for x in full['companies']}

def test_old_database_rejects_scope():
    with pytest.raises(ValueError): api.pool_at('2014-02-28',V1,api.SCOPE_TOP50)

def test_old_stock_list_unchanged():
    assert len(stock_list(V1)) == 288


def test_current_id_merges_and_legacy_union_stays_unchanged():
    from leadtrend_bt.ma_lab import resolve_security
    rows=stock_list(V2,scope=api.SCOPE_TOP50)
    assert len(rows)==83
    assert {'GOOGL','GOOG','AVGO','CMCSA','MDT','LIN'} <= {x['ticker'] for x in rows}
    assert len(stock_list(V2,legacy_union=True))==318
    assert resolve_security('GOOGL',V2)=='CUSIP:02079K305'
    assert resolve_security('AVGO',V2)=='CUSIP:11135F101'
