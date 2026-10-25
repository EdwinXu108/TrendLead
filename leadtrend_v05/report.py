"""Write stage-1 run artifacts (CSV/JSON) under the output directory."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pipeline import PipelineResult


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


UNIVERSE_FIELDS = [
    "signal_session_date",
    "snapshot_id",
    "holdings_as_of",
    "available_at",
    "security_id",
    "issuer_id",
    "in_snapshot",
    "eligible",
    "eligibility_reason",
    "class_selected",
]

SCORE_FIELDS = [
    "signal_session_date",
    "security_id",
    "issuer_id",
    "industry",
    "L",
    "window_sessions",
    "tri_start",
    "tri_end",
    "score",
    "rank",
    "unscorable_reason",
]

DECISION_FIELDS = [
    "signal_session_date",
    "action",
    "security_id",
    "issuer_id",
    "industry",
    "score",
    "rank",
    "reason",
    "buffer_score",
    "buffer_security_id",
    "in_feasible_order",
    "feasible_index",
    "target_count",
]

TRADE_FIELDS = [
    "session_date",
    "signal_session_date",
    "security_id",
    "side",
    "shares",
    "reference_price",
    "fill_price",
    "fee",
    "cash_delta",
    "reason",
]

EQUITY_FIELDS = [
    "session_date",
    "stock_value",
    "cash",
    "receivable_cash",
    "total_equity",
    "n_positions",
]

BENCH_FIELDS = [
    "session_date",
    "voo_tri",
    "benchmark_value",
]

ISSUE_FIELDS = [
    "category",
    "severity",
    "security_id",
    "snapshot_id",
    "event_id",
    "session_date",
    "message",
    "context",
]


def write_outputs(result: PipelineResult, output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "run_id": result.run_id,
        "dataset_kind": result.dataset_kind,
        "status": result.status,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "config": result.config,
        "file_hashes": result.file_hashes,
        "inputs_hash": result.inputs_hash,
        "supported_scope": {
            "events": result.config.get("supported_events", []),
            "rebalance_policy": result.config.get("rebalance_policy"),
            "benchmark_is_independent": True,
            "dataset_kind": result.dataset_kind,
        },
        "unsupported": result.config.get("unsupported_event_types", []),
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    summary = dict(result.summary)
    summary["run_id"] = result.run_id
    if result.failure_context:
        summary["failure_context"] = result.failure_context
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    _write_csv(output_dir / "universe.csv", result.universe_rows, UNIVERSE_FIELDS)
    _write_csv(output_dir / "scores.csv", result.score_rows, SCORE_FIELDS)
    _write_csv(output_dir / "decisions.csv", result.decision_rows, DECISION_FIELDS)
    _write_csv(output_dir / "trades.csv", result.trade_rows, TRADE_FIELDS)
    _write_csv(output_dir / "daily_equity.csv", result.equity_rows, EQUITY_FIELDS)
    _write_csv(output_dir / "benchmark.csv", result.benchmark_rows, BENCH_FIELDS)
    _write_csv(output_dir / "data_issues.csv", result.data_issues, ISSUE_FIELDS)

    if result.class_notes:
        _write_csv(
            output_dir / "class_selection_notes.csv",
            result.class_notes,
            ["issuer_id", "security_id", "method", "reason", "as_of"],
        )

    return {
        "run_manifest.json": str(output_dir / "run_manifest.json"),
        "summary.json": str(output_dir / "summary.json"),
    }
