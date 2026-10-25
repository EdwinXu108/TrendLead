"""Claude's score-only events and explicit two-part data acceptance policy."""
import csv
from datetime import date
from pathlib import Path
import pytest
from leadtrend_v05.dataio import EventRow, PriceRow, load_inputs
from leadtrend_v05.scoring import build_tri, momentum_score
from leadtrend_v05.stage2_enrich import reconcile_cash, classify_jump
from leadtrend_v05.stage2_validate import run_validate
from conftest import make_config
from leadtrend_v05 import paths


def event(kind, day, ratio=None, cash=None, value=None, method=""):
    return EventRow("E"+kind, "P", kind, day, ratio, cash, "per_share_at_entitlement", None,
                    "test", value, method)


def prices(values):
    days = [date(2025,1,d) for d in (2,3,6)][:len(values)]
    return days, [PriceRow("P",d,v,v,100,1000,"USD") for d,v in zip(days,values)]


def test_spinoff_child_value_reinvestment_hand_calculation():
    days, rows = prices([100,80,88])
    # Two child shares worth $10 each per old parent share.
    result = build_tri(rows,[event("spinoff",days[1],value=2*10,method="child_raw_close")],days)
    assert result.invalid_from is None
    assert [result.tri[d] for d in days] == pytest.approx([100,100,110])
    assert momentum_score(result.tri,days[1],1,days)[0] == pytest.approx(0)


def test_compound_reverse_split_and_child_value_use_old_shares():
    days, rows = prices([100,60])
    events = [event("reverse_split",days[1],ratio=1/3),
              event("spinoff",days[1],value=80,method="child_raw_close")]
    result = build_tri(rows,events,days)
    assert result.tri[days[1]] == pytest.approx(100)


def test_unvalued_spinoff_is_still_explicitly_unscorable():
    days,rows=prices([100,80])
    result=build_tri(rows,[event("spinoff",days[1])],days)
    assert result.invalid_from == days[1]
    assert days[1] not in result.tri


def test_score_cash_does_not_depend_on_payment_date():
    days,rows=prices([100,99])
    result=build_tri(rows,[event("cash_dividend",days[1],cash=1)],days)
    assert result.tri[days[1]] == pytest.approx(100)


@pytest.mark.parametrize("amounts,adjustment,status,keep",[
    ([.1,.08],.18,"sum",[0,1]), ([.075,.15],.15,"one",[1]),
    ([.609,.331],1.271,"unresolved",[]),([.25,.235],.735,"unresolved",[])])
def test_cash_components_compare_to_same_event_adjustment(amounts,adjustment,status,keep):
    actual,expected,indices=reconcile_cash(amounts,{"backward_adj_factorB":adjustment})
    assert (actual,indices)==(status,keep)
    assert expected==adjustment


def test_qfq_detects_mechanical_split_versus_real_crash():
    assert classify_jump(50/100-1,25/50-1)=="real_price_move_qfq_confirmed"
    assert classify_jump(50/100-1,50/50-1)=="missing_corporate_action"
    assert classify_jump(-.5,None)=="unresolved"


def test_missing_pay_date_loader_is_score_only_exception(tmp_path):
    from test_stage2_data_quality import _copy_fixture
    _copy_fixture(tmp_path)
    path=tmp_path/"events.csv"
    with path.open(encoding="utf-8",newline="") as f:
        rows=list(csv.DictReader(f));fields=list(rows[0])
    row=next(r for r in rows if r["event_type"]=="cash_dividend")
    row["pay_date"]=""
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    cfg=make_config(input_dir=str(tmp_path))
    assert any("missing pay_date" in r["message"] for r in load_inputs(cfg,Path('.')).data_issues)
    assert not any("missing pay_date" in r["message"] for r in load_inputs(cfg,Path('.'),score_only=True).data_issues)


def test_historical_gap_is_reported_without_removing_denominator(tmp_path):
    from test_stage2_data_quality import test_validate_coverage_denominator_includes_unpriced_member
    test_validate_coverage_denominator_includes_unpriced_member(tmp_path)
    work=tmp_path/paths.PREP;work.mkdir(parents=True,exist_ok=True)
    with (work/"stage2_unpriced_members.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["security_id","classification"]);w.writeheader()
        w.writerow({"security_id":"B","classification":"historical_member_no_price"})
    assert run_validate(tmp_path,tmp_path/"config.json",tmp_path/"after")==0
    with (tmp_path/"after/weekly_coverage.csv").open(encoding="utf-8",newline="") as f:
        rows=[r for r in csv.DictReader(f) if int(r["n_eligible"])]
    assert rows
    assert all(r["n_eligible"]=="2" and r["n_scored"]=="1" for r in rows)
    assert all(float(r["score_ratio"])==.5 and float(r["historical_no_price_ratio"])==.5 for r in rows)
    assert all(float(r["remediable_gap_ratio"])==0 and r["passes_gate"]=="1" for r in rows)
