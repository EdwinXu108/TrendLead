"""Current Futu industry evidence; never presented as historical classification."""

from __future__ import annotations

import csv
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path


def load_industry_membership(memberships_csv: Path, industry_members_dir: Path | None = None,
                             industry_list_csv: Path | None = None) -> dict[str, dict]:
    """Load all 145 industry files first, then use the narrower table as fallback."""
    names: dict[str, str] = {}
    if industry_list_csv and industry_list_csv.is_file():
        with industry_list_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                names[str(row.get("code") or "").strip()] = str(row.get("plate_name") or "").strip()
    out: dict[str, dict] = {}
    conflicts: set[str] = set()
    if industry_members_dir and industry_members_dir.is_dir():
        for path in sorted(industry_members_dir.glob("US_LIST*.csv")):
            code = path.stem
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    ticker = str(row.get("code") or "").strip().split(".")[-1].upper()
                    if not ticker:
                        continue
                    rec = {
                        "industry_code": code,
                        "industry_name": names.get(code, code),
                        "source": str(path),
                        "stock_id": str(row.get("stock_id") or ""),
                        "retrieved_at": "2026-10-24",
                        "vintage": "current_not_historical",
                    }
                    if ticker in out and out[ticker]["industry_code"] != code:
                        conflicts.add(ticker)
                    else:
                        out[ticker] = rec
    for ticker in conflicts:
        out.pop(ticker, None)
    if memberships_csv.is_file():
        with memberships_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ticker = str(row.get("code") or "").strip().split(".")[-1].upper()
                if not ticker or ticker in out or ticker in conflicts:
                    continue
                out[ticker] = {
                    "industry_code": str(row.get("industry_code") or "").strip(),
                    "industry_name": str(row.get("industry_name") or "").strip(),
                    "source": str(memberships_csv),
                    "stock_id": str(row.get("member_stock_id") or ""),
                    "retrieved_at": "2026-10-24",
                    "vintage": "current_not_historical",
                }
    return out


def assign_industry(ticker: str, issuer_id: str, industry_map: dict[str, dict]) -> tuple[str, str, str]:
    rec = industry_map.get(str(ticker or "").upper())
    if rec and (rec.get("industry_name") or rec.get("industry_code")):
        name = rec["industry_name"] or rec["industry_code"]
        return (name, f"futu_industry_{rec['retrieved_at']}_current_not_historical",
                f"code={rec['industry_code']};stock_id={rec.get('stock_id', '')};source={rec['source']}")
    return (f"UNKNOWN:{issuer_id}", "current_industry_unavailable_after_batch_and_single_retry",
            "not resolved in full Futu industry cache or get_owner_plate retry")


def fetch_missing_industries(ctx, tickers: list[str], cache_dir: Path, pace,
                             gaps_out: list[dict]) -> dict[str, dict]:
    """Query misses individually; retry every miss/failure once and cache raw evidence."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    resolved: dict[str, dict] = {}
    for index, ticker in enumerate(sorted(set(tickers)), 1):
        code = ticker if ticker.startswith("US.") else f"US.{ticker}"
        safe = code.replace(".", "_")
        path = cache_dir / f"owner_industry_{safe}.json"
        payload = None
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            last_error = ""
            for attempt in range(2):
                try:
                    pace()
                    ret, frame = ctx.get_owner_plate([code])
                    if ret == 0 and frame is not None:
                        records = frame.to_dict("records")
                        industries = [r for r in records if str(r.get("plate_type") or "").upper() == "INDUSTRY"]
                        if industries:
                            payload = {"code": code, "records": industries, "attempt": attempt + 1}
                            break
                        last_error = "no INDUSTRY row"
                    else:
                        last_error = str(frame)
                except Exception as exc:
                    last_error = str(exc)
                if attempt == 0:
                    time.sleep(1.0)
            if payload is None:
                gaps_out.append({"category": "industry", "severity": "warning", "security_id": "",
                                 "message": "industry_unresolved_after_single_retry",
                                 "detail": f"code={code};{last_error}"})
                continue
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
            path.with_suffix(".meta.json").write_text(json.dumps({
                "interface": "get_owner_plate", "code": code,
                "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }, indent=2) + "\n", encoding="utf-8")
        records = payload.get("records") or []
        industry_codes = {str(r.get("plate_code") or "") for r in records}
        if len(industry_codes) == 1:
            row = records[0]
            resolved[ticker.upper()] = {
                "industry_code": str(row.get("plate_code") or ""),
                "industry_name": str(row.get("plate_name") or ""),
                "source": f"opend_get_owner_plate:{code};attempt={payload.get('attempt', 'cached')}",
                "stock_id": "",
                "retrieved_at": "2026-10-25",
                "vintage": "current_not_historical",
            }
        else:
            gaps_out.append({"category": "industry", "severity": "warning", "security_id": "",
                             "message": "industry_conflicting_owner_plates",
                             "detail": f"code={code};plate_codes={sorted(industry_codes)}"})
        if index % 25 == 0:
            print(f"[industry] {index}/{len(tickers)}", flush=True)
    return resolved
