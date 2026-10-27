"""Small, integrity-checked cache for a single explicit research configuration."""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
from pathlib import Path
from leadtrend_db import api
from .runner import run

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ("leadtrend_bt/engine.py", "leadtrend_bt/metrics.py", "leadtrend_bt/runner.py",
          "leadtrend_db/api.py", "config/db_cash_overrides_v1.json", "config/db_corporate_events_v1.json",
          "config/pool_exclusions_v2.csv",
          "research/bt_v3/TERRA_R1/spec.json", "research/bt_v3/TERRA_R1/development_release_v1.json",
          "data/v05/raw/external/DTB3.csv")
OUTPUTS = ("config.json", "summary.json", "daily_equity.csv", "trades.csv", "positions.csv",
           "weekly_decisions.csv", "corporate_actions.csv", "score_counts.csv")

def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def fingerprint(cfg, db):
    inputs = {name: digest(ROOT / name) for name in INPUTS}
    identity = api.database_identity(db)
    # SQLite content hash describes economic content; file identity prevents same-path stale reuse.
    return {"database": identity, "config": cfg, "inputs": inputs}

def cached_run(cfg, db, out_root):
    cfg = dict(cfg)
    cfg.setdefault("protocol", "research_v1")
    if cfg["protocol"] != "research_v1":
        raise ValueError("cached research entry requires research_v1")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cfg.get("run_id", "")):
        raise ValueError("invalid run_id")
    if cfg.get("end", "") > "2024-06-28" or cfg.get("start", "") >= cfg.get("end", ""):
        raise ValueError("invalid or reserved-segment window")
    meta = dict(api.connect(db).execute("select key,value from meta"))
    if meta.get("economic_data_version") != "development_release_v1":
        raise ValueError("research_v1 requires development_release_v1 database")
    out = Path(out_root) / cfg["run_id"]
    fp = fingerprint(cfg, db)
    # JSON round trip also normalizes tuples, which is necessary across processes.
    fp = json.loads(json.dumps(fp, sort_keys=True))
    marker = out / "complete.json"
    if marker.exists():
        try:
            old = json.loads(marker.read_text(encoding="utf8"))
            if old["fingerprint"] == fp and all((out / n).is_file() and digest(out / n) == old["outputs"][n] for n in OUTPUTS):
                return {"status": "reused", "path": str(out), "summary": json.loads((out / "summary.json").read_text(encoding="utf8"))}
        except (OSError, KeyError, ValueError):
            pass
    out.mkdir(parents=True, exist_ok=True)
    marker.unlink(missing_ok=True)
    result = run(cfg, out_root=out_root, db_path=db)
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        head = "unavailable"
    record = {"fingerprint": fp, "head": head, "outputs": {name: digest(out / name) for name in OUTPUTS}}
    tmp = out / "complete.json.tmp"
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf8")
    tmp.replace(marker)
    return {"status": "computed", "path": str(out), "summary": result["summary"]}
