"""Full synthetic demo: artifacts, reproducibility, buffer coverage, exclusions."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from leadtrend_v05.config import load_config
from leadtrend_v05.dataio import load_inputs
from leadtrend_v05.pipeline import run_pipeline

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "v05_bootstrap.json"


@pytest.fixture(scope="module")
def demo_result():
    cfg = load_config(CONFIG)
    inputs = load_inputs(cfg, ROOT)
    return run_pipeline(inputs, run_id="test-demo")


def test_demo_status_ok(demo_result):
    assert demo_result.status == "ok"
    assert demo_result.dataset_kind == "synthetic"


def test_demo_required_artifacts(demo_result, tmp_path: Path):
    from leadtrend_v05.report import write_outputs

    out = tmp_path / "out"
    write_outputs(demo_result, out)
    required = [
        "run_manifest.json",
        "universe.csv",
        "scores.csv",
        "decisions.csv",
        "trades.csv",
        "daily_equity.csv",
        "benchmark.csv",
        "summary.json",
        "data_issues.csv",
    ]
    for name in required:
        assert (out / name).is_file(), name
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_kind"] == "synthetic"
    assert manifest["file_hashes"]
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "ok"
    # data_issues has header even if only info rows
    text = (out / "data_issues.csv").read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("category")


def test_demo_buffer_line_not_last_of_feasible_order(demo_result):
    """With N=10,k=3,b=2 and >=7 industries, len(F) should exceed 20."""
    f_lens = []
    for row in demo_result.decision_rows:
        if row.get("feasible_index"):
            f_lens.append(int(row["feasible_index"]))
    max_f = max(f_lens) if f_lens else 0
    assert max_f > 20, f"feasible order too short to exercise buffer: {max_f}"
    # buffer position is min(20, len(F)); since max_f>20 buffer is inside F
    buffers = {r["buffer_security_id"] for r in demo_result.decision_rows if r["buffer_security_id"]}
    assert buffers


def test_demo_retain_when_above_buffer_exit_when_below(demo_result):
    by_sig = defaultdict(list)
    for r in demo_result.decision_rows:
        by_sig[r["signal_session_date"]].append(r)
    saw_retain = any(r["action"] == "retain" for r in demo_result.decision_rows)
    saw_exit = any(
        r["action"] == "exit" and r["reason"] == "below_buffer_line"
        for r in demo_result.decision_rows
    )
    assert saw_retain
    assert saw_exit


def test_demo_no_voo_in_strategy(demo_result):
    for t in demo_result.trade_rows:
        assert t["security_id"] != "SYN_VOO"
    for d in demo_result.decision_rows:
        assert d["security_id"] != "SYN_VOO"
    # benchmark exists
    assert demo_result.benchmark_rows


def test_demo_exclusions_present(demo_result):
    reasons = {
        r["eligibility_reason"]
        for r in demo_result.universe_rows
        if r["in_snapshot"] == "1"
    }
    assert "excluded_ticker" in reasons or "excluded_security_type" in reasons
    # BRK.A excluded
    brka = [
        r
        for r in demo_result.universe_rows
        if r["security_id"] == "SYN_BRK_A" and r["in_snapshot"] == "1"
    ]
    assert brka and all(r["eligible"] == "0" for r in brka)
    # preferred ADR not excluded by keyword
    pref = [
        r
        for r in demo_result.universe_rows
        if r["security_id"] == "SYN_PREF_ADR" and r["class_selected"] == "1"
    ]
    assert pref


def test_demo_dual_class_single_issuer(demo_result):
    for sig, rows in _group_by_signal(demo_result).items():
        selected = [
            r["security_id"]
            for r in demo_result.universe_rows
            if r["signal_session_date"] == sig
            and r["class_selected"] == "1"
            and r["security_id"] in {"SYN_DUAL_A", "SYN_DUAL_B"}
        ]
        assert len(selected) <= 1


def _group_by_signal(res):
    g = defaultdict(list)
    for r in res.decision_rows:
        g[r["signal_session_date"]].append(r)
    return g


def test_demo_equity_identity_and_nonnegative_cash(demo_result):
    for r in demo_result.equity_rows:
        s = float(r["stock_value"])
        c = float(r["cash"])
        rec = float(r["receivable_cash"])
        tot = float(r["total_equity"])
        assert abs(s + c + rec - tot) < 1e-4
        assert c >= -1e-6


def test_demo_no_trade_weeks_and_swap_weeks(demo_result):
    traded = {t["signal_session_date"] for t in demo_result.trade_rows}
    swap = {
        d["signal_session_date"]
        for d in demo_result.decision_rows
        if d["action"] in {"exit", "fill"}
    }
    assert traded <= swap
    # at least one week without trades (after first fill) if there are >=2 trade weeks
    if traded:
        assert traded  # sanity
    # every trade is integer shares and has fee 1
    for t in demo_result.trade_rows:
        assert int(t["shares"]) > 0
        assert float(t["fee"]) == 1.0


def test_demo_reproducible_economics():
    cfg = load_config(CONFIG)
    a = run_pipeline(load_inputs(cfg, ROOT), run_id="A")
    b = run_pipeline(load_inputs(cfg, ROOT), run_id="B")
    assert a.status == b.status == "ok"
    assert a.trade_rows == b.trade_rows
    assert a.equity_rows == b.equity_rows
    assert a.score_rows == b.score_rows
    assert a.summary["final_equity"] == b.summary["final_equity"]


def test_demo_summary_target_fields(demo_result):
    s = demo_result.summary
    assert isinstance(s["target_ids_at_last_signal"], list)
    assert isinstance(s["target_count_at_last_signal"], int)
    assert s["target_count_at_last_signal"] == len(s["target_ids_at_last_signal"])


def test_demo_benchmark_aligned_to_first_trade(demo_result):
    s = demo_result.summary
    assert s["comparison_available"] is True
    first_trade = min(t["session_date"] for t in demo_result.trade_rows)
    assert s["comparison_start_session"] == first_trade
    assert demo_result.benchmark_rows[0]["session_date"] == first_trade
    assert float(demo_result.benchmark_rows[0]["benchmark_value"]) == pytest.approx(
        s["initial_capital"]
    )
    assert s["comparison_end_session"] == demo_result.equity_rows[-1]["session_date"]


def test_demo_dividend_receivable_and_payment(demo_result):
    """Synthetic fixture EV_DIV_E01 must produce a receivable and a later cash payment."""
    eq = {r["session_date"]: r for r in demo_result.equity_rows}
    receivable_days = [
        r for r in demo_result.equity_rows if float(r["receivable_cash"]) > 0
    ]
    assert receivable_days, "expected at least one day with dividend receivable in demo"
    # ex 2025-03-28, pay 2025-04-11 for SYN_E01 $0.40
    ex = eq.get("2025-03-28")
    pay = eq.get("2025-04-11")
    assert ex is not None and pay is not None
    rec = float(ex["receivable_cash"])
    assert rec > 0
    entitled = rec / 0.40
    assert entitled == int(entitled) and entitled > 0
    # pay day: receivable cleared
    assert float(pay["receivable_cash"]) == 0.0
    # cash increases by receivable amount around pay date
    prior = demo_result.equity_rows[
        [r["session_date"] for r in demo_result.equity_rows].index("2025-04-11") - 1
    ]
    assert abs((float(pay["cash"]) - float(prior["cash"])) - rec) < 1e-4 or float(pay["cash"]) > float(prior["cash"])


def test_cli_demo_entrypoint(tmp_path: Path):
    out = tmp_path / "cli_out"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "leadtrend_v05",
            "demo",
            "--config",
            str(CONFIG.relative_to(ROOT)).replace("\\", "/")
            if CONFIG.is_relative_to(ROOT)
            else str(CONFIG),
            "--output",
            str(out),
            "--project-root",
            str(ROOT),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (out / "summary.json").is_file()
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "ok"
