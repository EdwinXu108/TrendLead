"""Stage-2 real-data preparation. No portfolio backtest or return calculation."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from .industry_map import assign_industry, fetch_missing_industries, load_industry_membership
from .nyse_calendar import session_times_utc
from .opend_prices import OpenDPriceFetcher, holding_key, security_id_from_key
from .sec_nport import (TARGET_CLASS_ID, TARGET_SERIES_ID, download_and_parse,
                        fetch_submissions, require_sec_user_agent)
from . import paths
from .ticker_map import (is_rejected_instrument, load_sec_company_tickers,
                         map_holdings_to_tickers, prefetch_openfigi, prefetch_isin_fallback, resolve_shareclass_links)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def _fetch_sic(cik: int, user_agent: str, cache_dir: Path) -> str:
    cache = cache_dir / f"sic_{cik}.txt"
    if cache.is_file():
        value = cache.read_text(encoding="utf-8").strip()
        if value:
            return value
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
            sic = str(json.loads(raw.decode("utf-8")).get("sic") or "")
            if sic:
                cache.write_text(sic, encoding="utf-8")
            return sic
        except urllib.error.HTTPError as exc:
            if exc.code not in {403, 429} or attempt == 2:
                break
            time.sleep(2.0 * (attempt + 1))
        except Exception:
            if attempt == 2:
                break
            time.sleep(1.0 + attempt)
        finally:
            time.sleep(0.2)
    return ""


def _void_old_registry(work_dir: Path) -> None:
    path = work_dir / "stage2_experiment_registry.json"
    if not path.is_file():
        return
    raw = path.read_bytes()
    old = json.loads(raw.decode("utf-8"))
    digest = hashlib.sha256(raw).hexdigest()[:12]
    archive = work_dir / f"stage2_experiment_registry_void_{digest}.json"
    if not archive.exists():
        archive.write_bytes(raw)
    old["status"] = "VOID_defective_stage2_company_actions_prices_and_coverage"
    old["voided_reason"] = "superseded: NaN pseudo-splits, adjusted prices, wrong coverage denominator, identity/industry gaps"
    old["archived_as"] = archive.name
    path.write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_submissions(raw_dir: Path, sec_ua: str) -> dict:
    path = raw_dir / "submissions_CIK0000036405.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    data = fetch_submissions(user_agent=sec_ua)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def _split_price_checks(event_rows: list[dict], prices_by_sid: dict[str, list],
                        compound_events: list[dict]) -> list[dict]:
    checks: list[dict] = []
    compound_by = {(sid, c["effective_session"]): c for c in compound_events
                   for sid in c["parent_security_ids"]}
    for event in event_rows:
        if event["event_type"] not in {"split", "reverse_split"}:
            continue
        sid = event["security_id"]
        event_date = date.fromisoformat(event["effective_session"])
        ratio = float(event["shares_new_per_old"])
        series = prices_by_sid.get(sid, [])
        current = next((b for b in series if b.session_date == event_date), None)
        previous = max((b for b in series if b.session_date < event_date),
                       key=lambda b: b.session_date, default=None)
        observed = previous.close / current.close if previous and current and current.close > 0 else None
        deviation = abs(observed - ratio) / ratio if observed is not None and ratio > 0 else None
        method = "raw_parent_price_ratio"
        compound = compound_by.get((sid, event["effective_session"]))
        child_close = combined_value = combined_deviation = None
        if compound is not None:
            method = "compound_parent_plus_child_value"
            child_series = prices_by_sid.get(compound["child_security_id"], [])
            child = next((b for b in child_series if b.session_date == event_date), None)
            if previous and current and child:
                child_close = child.close
                combined_value = (float(compound["parent_shares_new_per_old"]) * current.close
                                  + float(compound["child_shares_per_old"]) * child.close)
                combined_deviation = abs(previous.close - combined_value) / previous.close
                passed = math.isfinite(combined_deviation) and combined_deviation <= 0.20
                deviation = combined_deviation
            else:
                passed = False
        else:
            passed = deviation is not None and math.isfinite(deviation) and deviation <= 0.20
        checks.append({
            "event_id": event["event_id"], "security_id": sid, "event_type": event["event_type"],
            "effective_session": event["effective_session"], "shares_new_per_old": f"{ratio:.10g}",
            "previous_session": previous.session_date.isoformat() if previous else "",
            "previous_raw_close": f"{previous.close:.8f}" if previous else "",
            "event_raw_close": f"{current.close:.8f}" if current else "",
            "observed_old_over_new": f"{observed:.10g}" if observed is not None else "",
            "child_raw_close": f"{child_close:.8f}" if child_close is not None else "",
            "combined_value_per_old_share": f"{combined_value:.8f}" if combined_value is not None else "",
            "relative_deviation": f"{deviation:.8f}" if deviation is not None else "",
            "check_method": method, "tolerance": "0.20", "passes": "1" if passed else "0",
            "source": event["source"] + (";" + "|".join(compound["sources"]) if compound else ""),
        })
    return checks


def run_stage2_prepare(project_root: Path, stage2_config_path: Path | None = None,
                       max_securities: int | None = None) -> dict:
    root = Path(project_root)
    cfg_path = stage2_config_path or root / "config" / "v05_stage2.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    sec_ua = require_sec_user_agent(cfg.get("sec_user_agent"))
    raw_dir = root / cfg.get("raw_dir", "data/v05/raw")
    norm_dir = root / paths.NORMALIZED
    work_dir = root / paths.PREP
    for directory in (raw_dir, norm_dir, work_dir):
        directory.mkdir(parents=True, exist_ok=True)
    price_start = cfg.get("price_start", "2019-05-01")
    price_end = cfg.get("price_end", "2026-12-31")
    gate = float(cfg.get("score_coverage_gate", 0.95))
    started = time.perf_counter()
    identity_issues: list[dict] = []
    price_gaps: list[dict] = []
    action_gaps: list[dict] = []
    industry_gaps: list[dict] = []
    opend_log: list[dict] = []
    _void_old_registry(root / paths.REGISTRY)

    submissions = _load_submissions(raw_dir, sec_ua)
    recent = submissions["filings"]["recent"]
    candidates = []
    for index, form in enumerate(recent["form"]):
        if form == "NPORT-P":
            candidates.append({"accession": recent["accessionNumber"][index],
                               "report_date": recent["reportDate"][index],
                               "filing_date": recent["filingDate"][index],
                               "acceptance_datetime": recent["acceptanceDateTime"][index]})
    candidates.sort(key=lambda item: (item["report_date"], item["accession"]))
    snapshots = []
    available_diffs = []
    for meta in candidates:
        try:
            snapshot = download_and_parse(meta["accession"], meta["report_date"], meta["filing_date"],
                                          meta["acceptance_datetime"], raw_dir, user_agent=sec_ua)
        except ValueError:
            continue
        except Exception as exc:
            identity_issues.append({"category": "universe", "severity": "error", "security_id": "",
                                    "message": "nport_failed", "detail": f"{meta['accession']} {exc}"})
            continue
        if snapshot.series_id != TARGET_SERIES_ID or not snapshot.has_voo_class:
            identity_issues.append({"category": "universe", "severity": "error", "security_id": "",
                                    "message": "series_or_voo_class_mismatch", "detail": snapshot.accession})
            continue
        if snapshot.parse_notes:
            identity_issues.append({"category": "universe", "severity": "error", "security_id": "",
                                    "message": "nport_parse_notes", "detail": f"{snapshot.accession}:{snapshot.parse_notes}"})
            continue
        snapshots.append(snapshot)
        old_dt = datetime.fromisoformat(meta["acceptance_datetime"].replace("Z", "+00:00")).astimezone(timezone.utc)
        available_diffs.append({"accession": snapshot.accession, "holdings_as_of": snapshot.holdings_as_of.isoformat(),
                                "available_at_old_json": old_dt.isoformat().replace("+00:00", "Z"),
                                "available_at_new_header": snapshot.available_at.isoformat().replace("+00:00", "Z"),
                                "delta_hours": round((old_dt - snapshot.available_at).total_seconds() / 3600, 3)})
    snapshots.sort(key=lambda s: (s.holdings_as_of, s.available_at, s.accession))
    if not snapshots:
        raise RuntimeError("no usable target VOO N-PORT snapshots")
    _write_csv(work_dir / "stage2_available_at_diff.csv",
               ["accession", "holdings_as_of", "available_at_old_json", "available_at_new_header", "delta_hours"],
               available_diffs)

    lag_rows = []
    manifest_rows = []
    previous = None
    for snapshot in snapshots:
        snapshot_id = f"NPORT_{snapshot.holdings_as_of.isoformat()}_{snapshot.accession}"
        current = {holding_key(h) for h in snapshot.holdings}
        prior = {holding_key(h) for h in previous.holdings} if previous else set()
        lag_rows.append({"snapshot_id": snapshot_id, "accession": snapshot.accession,
                         "holdings_as_of": snapshot.holdings_as_of.isoformat(),
                         "available_at": snapshot.available_at.isoformat().replace("+00:00", "Z"),
                         "lag_days": (snapshot.available_at.date() - snapshot.holdings_as_of).days,
                         "n_equity": len(snapshot.holdings), "non_equity_count": snapshot.non_equity_count,
                         "added_vs_prev": len(current - prior) if previous else "",
                         "removed_vs_prev": len(prior - current) if previous else ""})
        for holding in snapshot.holdings:
            key = holding_key(holding)
            manifest_rows.append({"snapshot_accession": snapshot.accession, "snapshot_id": snapshot_id,
                                  "series_id": snapshot.series_id, "class_ids": "|".join(snapshot.class_ids),
                                  "voo_class_present": "1", "holdings_as_of": snapshot.holdings_as_of.isoformat(),
                                  "available_at": snapshot.available_at.isoformat().replace("+00:00", "Z"),
                                  "source_sha256": snapshot.source_sha256, "security_id": security_id_from_key(key),
                                  "cusip": holding.cusip, "isin": holding.isin, "lei": holding.lei,
                                  "name": holding.name, "title": holding.title, "balance": holding.balance,
                                  "val_usd": holding.val_usd, "pct_val": holding.pct_val,
                                  "asset_cat": holding.asset_cat, "issuer_cat": holding.issuer_cat})
        previous = snapshot
    _write_csv(work_dir / "stage2_snapshot_lag.csv", list(lag_rows[0].keys()), lag_rows)
    _write_csv(work_dir / "stage2_holdings_manifest.csv", list(manifest_rows[0].keys()), manifest_rows)

    all_holdings = [h for snapshot in snapshots for h in snapshot.holdings]
    if max_securities:
        representatives = {}
        for holding in sorted(all_holdings, key=lambda h: -h.pct_val):
            representatives.setdefault(holding_key(holding), holding)
        allowed_keys = set(list(representatives)[:max_securities])
        all_holdings = [h for h in all_holdings if holding_key(h) in allowed_keys]
        snapshots_for_output = []
        for snapshot in snapshots:
            snapshot.holdings = [h for h in snapshot.holdings if holding_key(h) in allowed_keys]
            snapshots_for_output.append(snapshot)
        snapshots = snapshots_for_output
    representatives = {}
    membership_dates: dict[str, list[date]] = defaultdict(list)
    for holding in all_holdings:
        key = holding_key(holding)
        representatives.setdefault(key, holding)
    for snapshot in snapshots:
        for holding in snapshot.holdings:
            membership_dates[holding_key(holding)].append(snapshot.holdings_as_of)
    keys = sorted(representatives)

    ticker_rows = load_sec_company_tickers(raw_dir / "company_tickers.json", user_agent=sec_ua)
    # Mutable lookup index is derived state; raw responses remain append-only.
    figi_cache = work_dir / "stage2_openfigi_cache_v3.json"
    if cfg.get("use_openfigi", True):
        prefetch_openfigi(all_holdings, figi_cache, raw_dir / "openfigi_responses")
        # FIGI now excludes delisted equities by default. Retry only unresolved
        # historical identifiers; this is discovery, not OTC trading eligibility.
        prefetch_openfigi(all_holdings, figi_cache, raw_dir / "openfigi_responses", include_unlisted=True)
        prefetch_isin_fallback(all_holdings, figi_cache, raw_dir / "openfigi_responses")
        resolve_shareclass_links(figi_cache, raw_dir / "openfigi_responses")
    mapping, map_issues = map_holdings_to_tickers(all_holdings, ticker_rows, figi_cache=figi_cache,
                                                  use_openfigi=bool(cfg.get("use_openfigi", True)))
    identity_issues.extend(map_issues)

    # Reviewed identifier-specific corrections/lifetimes, never name prefixes.
    override_path = work_dir / "stage2_identity_overrides.json"
    overrides = json.loads(override_path.read_text(encoding="utf-8")) if override_path.is_file() else {}
    for key, override in overrides.items():
        if key not in mapping:
            continue
        entry = mapping[key]
        if override.get("ticker"):
            entry.ticker = override["ticker"]
            entry.cik = override.get("cik", entry.cik)
            entry.match_method = "reviewed_identifier_crosswalk"
        entry.evidence += ";" + override["reason"] + ";" + "|".join(override["sources"])

    historical_sec_cik = {
        "264411505": (783280, "https://www.sec.gov/Archives/edgar/data/783280/000078328021000059/0000783280-21-000059.txt"),
        "03750L109": (1820877, "https://www.sec.gov/Archives/edgar/data/926660/000119312521237070/0001193125-21-237070.txt"),
    }
    for key, (cik, url) in historical_sec_cik.items():
        if key in mapping:
            mapping[key].cik = mapping[key].cik or cik
            mapping[key].evidence += f";historical_sec_cik={cik};source={url}"

    reit_rows = []
    reit_keys: set[str] = set()
    for key in keys:
        holding = representatives[key]
        entry = mapping.get(key)
        sic = _fetch_sic(entry.cik, sec_ua, raw_dir) if entry and entry.cik else ""
        figi_reit = bool(entry and re.search(r";type=REIT(?:;|$)", entry.evidence))
        reviewed_reit = bool(overrides.get(key, {}).get("force_reit"))
        is_reit = sic == "6798" or figi_reit or reviewed_reit
        suspected = any(word in (holding.name or "").upper() for word in ("REIT", "REALTY", "REAL ESTATE", "PROPERTY TRUST"))
        if is_reit or suspected:
            decision = ("exclude_reit_sic_6798" if sic == "6798" else "exclude_reit_reviewed_issuer" if reviewed_reit else "exclude_reit_figi_security_type") if is_reit else "review_not_excluded_without_corroboration"
            evidence = f"sic={sic or 'unknown'}" + (";" + entry.evidence if figi_reit or reviewed_reit else "")
            reit_rows.append({"security_id": security_id_from_key(key), "cusip": holding.cusip,
                              "name": holding.name, "ticker": entry.ticker if entry else "",
                              "cik": entry.cik if entry else "", "sic": sic, "decision": decision, "evidence": evidence})
        if is_reit:
            reit_keys.add(key)
    # A REIT issuer remains a REIT across its common-share CUSIP changes.
    # LEI joins are used only for issuer-level classification, not stock mapping.
    reit_by_lei = {representatives[k].lei: k for k in reit_keys
                   if representatives[k].lei and representatives[k].lei.upper() != "N/A"}
    for key in keys:
        holding = representatives[key]
        linked = reit_by_lei.get(holding.lei)
        if key in reit_keys or not linked:
            continue
        reit_keys.add(key)
        reit_rows = [r for r in reit_rows if r["security_id"] != security_id_from_key(key)]
        reit_rows.append({"security_id": security_id_from_key(key), "cusip": holding.cusip,
                          "name": holding.name, "ticker": mapping[key].ticker,
                          "cik": mapping[linked].cik or "", "sic": "6798",
                          "decision": "exclude_reit_same_lei_sic_6798",
                          "evidence": f"same issuer LEI={holding.lei};confirmed SIC 6798 for key={linked}"})
    _write_csv(work_dir / "stage2_reit_review.csv",
               ["security_id", "cusip", "name", "ticker", "cik", "sic", "decision", "evidence"], reit_rows)

    ind_map = load_industry_membership(
        root / paths.REFERENCE / "universe_verification_2026-10-24/exchange_candidate_industry_memberships.csv",
        root / paths.REFERENCE / "universe_verification_2026-10-24/industry_members",
        root / paths.REFERENCE / "opend_probe_data/current_us_industry_list.csv",
    )
    fetcher = OpenDPriceFetcher(cache_dir=raw_dir / "opend_kline_none_v1",
                                host=cfg.get("opend_host", "127.0.0.1"), port=int(cfg.get("opend_port", 11111)))
    quota_before = fetcher.query_kline_quota()
    opend_log.append({"step": "quota_before", **{k: quota_before.get(k) for k in ("used", "remain", "raw")}})
    missing_industry = sorted({mapping[k].ticker for k in keys if k not in reit_keys and mapping.get(k)
                               and mapping[k].ticker and mapping[k].ticker.upper() not in ind_map})
    def industry_pace():
        fetcher._pace()
        fetcher.request_count += 1

    ind_map.update(fetch_missing_industries(fetcher._get_ctx(), missing_industry,
                                            raw_dir / "opend_industry", industry_pace, industry_gaps))

    mapped = [(key, mapping[key].ticker) for key in keys
              if key not in reit_keys and mapping.get(key) and mapping[key].ticker]
    mapped.append(("VOO_ETF", "VOO"))
    unique_downloads: dict[str, str] = {}
    for key, ticker in mapped:
        unique_downloads.setdefault(ticker, key)
    remain = quota_before.get("remain")
    uncached_tickers = [ticker for ticker in unique_downloads
                        if fetcher.load_cache(ticker, price_start, price_end) is None]
    if isinstance(remain, int) and remain >= 0 and remain < len(uncached_tickers):
        identity_issues.append({"category": "opend", "severity": "error", "security_id": "",
                                "message": "insufficient_kline_quota",
                                "detail": f"need_uncached_unique={len(uncached_tickers)} remain={remain}; no partial cohort downloaded"})
        unique_downloads = {}
        mapped = []
    outcomes = {}
    for index, (ticker, key) in enumerate(unique_downloads.items(), 1):
        outcome = fetcher.fetch_one(ticker, key, price_start, price_end)
        outcomes[ticker] = outcome
        if index % 25 == 0 or index == len(unique_downloads):
            print(f"[prices] {index}/{len(unique_downloads)} requests={fetcher.request_count}", flush=True)
    for key, ticker in mapped:
        outcome = outcomes.get(ticker)
        sid = "VOO_ETF" if key == "VOO_ETF" else security_id_from_key(key)
        if outcome is None or not outcome.ok or outcome.n_bars == 0:
            category = "insufficient_kline_quota" if outcome is None else (outcome.gap_category or "request_failed_retryable")
            price_gaps.append({"security_id": sid, "ticker": ticker, "gap_category": category,
                               "detail": "not downloaded" if outcome is None else outcome.error or "kline empty in range",
                               "n_bars": 0})
    for key in keys:
        entry = mapping.get(key)
        if key not in reit_keys and (not entry or not entry.ticker):
            price_gaps.append({"security_id": security_id_from_key(key), "ticker": "",
                               "gap_category": entry.match_method if entry and entry.match_method != "unmatched" else "unmapped_identity",
                               "detail": f"name={representatives[key].name};evidence={entry.evidence if entry else ''}",
                               "n_bars": 0})

    ca_mapped = []
    for key, ticker in mapped:
        sid = "VOO_ETF" if key == "VOO_ETF" else security_id_from_key(key)
        if fetcher.load_cache(ticker, price_start, price_end):
            ca_mapped.append((key, ticker))
        else:
            action_gaps.append({"category": "corporate_actions", "severity": "warning", "security_id": sid,
                                "message": "corporate_actions_unavailable_without_price_history",
                                "detail": f"code=US.{ticker};no normalized price history"})
    event_rows = fetcher.fetch_corporate_actions_for(ca_mapped, price_start, price_end, action_gaps,
                                                      cache_dir=raw_dir / "opend_corporate_actions")
    lifetimes = {security_id_from_key(k): v["history_from"] for k, v in overrides.items() if v.get("history_from")}
    # Supplier history can include another issuer formerly using the same ticker,
    # or the cancelled equity before emergence from bankruptcy.
    retained_events = []
    for event in event_rows:
        start = lifetimes.get(event["security_id"])
        if start and event["effective_session"] < start:
            action_gaps.append({"category": "identity", "severity": "info", "security_id": event["security_id"],
                                "message": "supplier_event_outside_security_lifetime",
                                "detail": f"{event['event_id']};verified_history_from={start}"})
        else:
            retained_events.append(event)
    event_rows = retained_events
    compound_path = work_dir / "stage2_verified_compound_events.json"
    compound_events = json.loads(compound_path.read_text(encoding="utf-8"))["compound_events"] if compound_path.is_file() else []
    existing_events = {(e["security_id"], e["event_type"], e["effective_session"]) for e in event_rows}
    for compound in compound_events:
        if not compound.get("add_spinoff_event"):
            continue
        for sid in compound["parent_security_ids"]:
            key = (sid, "spinoff", compound["effective_session"])
            if key in existing_events:
                continue
            event_rows.append({"event_id": f"CA_SPINOFF_{sid}_{compound['effective_session']}_{compound['event_id_suffix']}",
                               "security_id": sid, "event_type": "spinoff",
                               "effective_session": compound["effective_session"], "shares_new_per_old": "",
                               "cash_amount_per_share": "", "cash_basis": "", "pay_date": "",
                               "source": compound["distribution_note"] + ";" + "|".join(compound["sources"])})
            existing_events.add(key)
    quota_after = fetcher.query_kline_quota()
    opend_log.append({"step": "quota_after", **{k: quota_after.get(k) for k in ("used", "remain", "raw")}})

    prices_by_sid: dict[str, list] = {}
    for key, ticker in mapped:
        bars = fetcher.load_cache(ticker, price_start, price_end) or []
        sid = "VOO_ETF" if key == "VOO_ETF" else security_id_from_key(key)
        if sid in lifetimes:
            bars = [b for b in bars if b.session_date.isoformat() >= lifetimes[sid]]
        prices_by_sid[sid] = bars
    voo_bars = prices_by_sid.get("VOO_ETF", [])
    if not voo_bars:
        fetcher.close()
        raise RuntimeError("VOO benchmark raw bars missing")
    last_observed_session = max(b.session_date for b in voo_bars).isoformat()
    future_events = [e for e in event_rows if e["effective_session"] > last_observed_session]
    event_rows = [e for e in event_rows if e["effective_session"] <= last_observed_session]
    sessions = []
    for bar in voo_bars:
        open_utc, close_utc, early, note = session_times_utc(bar.session_date)
        sessions.append({"session_date": bar.session_date.isoformat(), "open_time_utc": open_utc,
                         "close_time_utc": close_utc, "is_early_close": "1" if early else "0",
                         "note": f"{note};source=nyse.com/markets/hours-calendars;checked=2026-10-25"})

    sec_rows = []
    industry_hits = 0
    for key in keys:
        holding = representatives[key]
        entry = mapping.get(key)
        ticker = entry.ticker if entry else ""
        lei = str(holding.lei or "").strip()
        issuer = f"LEI:{lei}" if lei and lei.upper() != "N/A" else f"CUSIP:{holding.cusip[:6]}" if holding.cusip else f"ISSUER:{key}"
        security_type = "reit" if key in reit_keys else "common_stock"
        industry, industry_source, industry_evidence = assign_industry(ticker, issuer, ind_map)
        if not industry.startswith("UNKNOWN"):
            industry_hits += 1
        sec_rows.append({"security_id": security_id_from_key(key), "issuer_id": issuer, "ticker": ticker,
                         "ticker_valid_from": "", "ticker_valid_to": "", "security_type": security_type,
                         "exchange": "US_LISTED" if ticker else "UNKNOWN", "industry": industry,
                         "industry_source_version": industry_source, "currency": holding.currency or "USD",
                         "source_evidence": f"cusip={holding.cusip};isin={holding.isin};lei={holding.lei};map={entry.match_method if entry else 'unmatched'};{entry.evidence if entry else ''};ind={industry_evidence}"})
    sec_rows.append({"security_id": "VOO_ETF", "issuer_id": "ISS_VOO", "ticker": "VOO",
                     "ticker_valid_from": "", "ticker_valid_to": "", "security_type": "etf", "exchange": "ARCX",
                     "industry": "BENCH", "industry_source_version": "stage2", "currency": "USD",
                     "source_evidence": "benchmark only"})
    snap_rows, member_rows = [], []
    for snapshot in snapshots:
        snapshot_id = f"NPORT_{snapshot.holdings_as_of.isoformat()}_{snapshot.accession}"
        snap_rows.append({"snapshot_id": snapshot_id, "holdings_as_of": snapshot.holdings_as_of.isoformat(),
                          "available_at": snapshot.available_at.isoformat().replace("+00:00", "Z"),
                          "availability_precision": "timestamp", "source": snapshot.source_url,
                          "source_hash": snapshot.source_sha256,
                          "retrieved_at": snapshot.retrieved_at.isoformat().replace("+00:00", "Z"),
                          "revision": f"nport_{snapshot.accession}"})
        for holding in snapshot.holdings:
            member_rows.append({"snapshot_id": snapshot_id, "security_id": security_id_from_key(holding_key(holding)),
                                "original_name": holding.name, "weight": f"{holding.pct_val:.10g}"})

    price_rows = []
    for sid, bars in prices_by_sid.items():
        for bar in bars:
            price_rows.append({"security_id": sid, "session_date": bar.session_date.isoformat(),
                               "open": f"{bar.open:.8f}", "close": f"{bar.close:.8f}",
                               "volume": "" if bar.volume is None else f"{bar.volume:.4f}",
                               "turnover": "" if bar.turnover is None else f"{bar.turnover:.4f}", "currency": "USD"})

    split_checks = _split_price_checks(event_rows, prices_by_sid, compound_events)
    for check in split_checks:
        if check["passes"] != "1":
            action_gaps.append({"category": "split_price_check", "severity": "error",
                                "security_id": check["security_id"], "message": "split_price_check_failed",
                                "detail": json.dumps(check, ensure_ascii=False)})
    events_by_date = {(e["security_id"], e["effective_session"]) for e in event_rows}
    for sid, series in prices_by_sid.items():
        ordered = sorted(series, key=lambda b: b.session_date)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.close > 0 and abs(current.close / previous.close - 1) >= 0.40:
                if (sid, current.session_date.isoformat()) not in events_by_date:
                    action_gaps.append({"category": "price_jump_unexplained", "severity": "warning", "security_id": sid,
                                        "message": "abs_close_change_ge_40pct_without_event",
                                        "effective_session": current.session_date.isoformat(),
                                        "previous_session": previous.session_date.isoformat(),
                                        "previous_close": f"{previous.close:.8f}", "close": f"{current.close:.8f}",
                                        "detail": f"{previous.session_date}->{current.session_date} {previous.close:.8f}->{current.close:.8f}",
                                        "conclusion": "unresolved", "review_note": "requires event/news source review"})

    jump_override_path = work_dir / "stage2_jump_review_overrides.csv"
    if jump_override_path.is_file():
        with jump_override_path.open("r", encoding="utf-8-sig", newline="") as handle:
            overrides = {(r["security_id"], r["effective_session"]): r for r in csv.DictReader(handle)}
        for gap in action_gaps:
            if gap.get("message") != "abs_close_change_ge_40pct_without_event":
                continue
            override = overrides.get((gap["security_id"], gap.get("effective_session", "")))
            if override:
                gap["conclusion"] = override.get("conclusion", "unresolved")
                gap["review_note"] = override.get("review_note", "")

    _write_csv(norm_dir / "calendar.csv", ["session_date", "open_time_utc", "close_time_utc", "is_early_close", "note"], sessions)
    _write_csv(norm_dir / "securities.csv", list(sec_rows[0].keys()), sec_rows)
    _write_csv(norm_dir / "snapshots.csv", list(snap_rows[0].keys()), snap_rows)
    _write_csv(norm_dir / "snapshot_members.csv", list(member_rows[0].keys()), member_rows)
    _write_csv(norm_dir / "prices.csv", list(price_rows[0].keys()), price_rows)
    _write_csv(norm_dir / "events.csv", ["event_id", "security_id", "event_type", "effective_session",
                                          "shares_new_per_old", "cash_amount_per_share", "cash_basis", "pay_date", "source"], event_rows)

    unpriced_rows = []
    latest_asof = max(s.holdings_as_of for s in snapshots)
    for key in keys:
        sid = security_id_from_key(key)
        if prices_by_sid.get(sid):
            continue
        entry = mapping.get(key)
        gap = next((g for g in reversed(price_gaps) if g["security_id"] == sid), {})
        first, last = min(membership_dates[key]), max(membership_dates[key])
        if key in reit_keys:
            classification = "excluded_reit"
        elif gap.get("gap_category") == "request_failed_retryable":
            classification = "request_failed_retryable"
        elif not entry or not entry.ticker:
            # Leaving the fund does not prove delisting, acquisition, or that
            # free quotes are unavailable. Unmapped historical names need work too.
            classification = "mapping_unresolved_historical" if last < latest_asof else "mapping_remediable"
        elif last < latest_asof and gap.get("gap_category") in {"unknown_security", "no_data", "unsupported_venue"}:
            classification = "historical_member_no_price"
        else:
            classification = "no_data_current_member"
        unpriced_rows.append({"security_id": sid, "cusip": representatives[key].cusip,
                              "isin": representatives[key].isin, "lei": representatives[key].lei,
                              "declared_name": representatives[key].name, "ticker": entry.ticker if entry else "",
                              "first_holdings_as_of": first.isoformat(), "last_holdings_as_of": last.isoformat(),
                              "snapshot_count": len(membership_dates[key]),
                              "mapping_status": entry.match_method if entry else "unmatched",
                              "failure_reason": gap.get("gap_category", "no_price"),
                              "failure_detail": gap.get("detail", ""), "classification": classification})
    _write_csv(work_dir / "stage2_unpriced_members.csv", list(unpriced_rows[0].keys()) if unpriced_rows else
               ["security_id", "cusip", "isin", "lei", "declared_name", "ticker", "first_holdings_as_of",
                "last_holdings_as_of", "snapshot_count", "mapping_status", "failure_reason", "failure_detail", "classification"],
               unpriced_rows)

    full = partial = none = 0
    session_count = len(sessions)
    for key in keys:
        if key in reit_keys:
            continue
        bars = prices_by_sid.get(security_id_from_key(key), [])
        if not bars:
            none += 1
            continue
        dates = [b.session_date for b in bars]
        expected = sum(1 for b in voo_bars if dates[0] <= b.session_date <= dates[-1])
        internal = len(bars) / expected if expected else 0.0
        if internal >= 0.95:
            full += 1
        else:
            partial += 1
    event_counts = Counter(e["event_type"] for e in event_rows)
    figi_records = json.loads(figi_cache.read_text(encoding="utf-8")) if figi_cache.is_file() else {}
    figi_status_counts = Counter(record.get("status", "unknown") for record in figi_records.values())
    gap_categories = Counter(g.get("gap_category", "unknown") for g in price_gaps)
    unpriced_categories = Counter(r["classification"] for r in unpriced_rows)
    duplicate_divs_removed = sum(1 for g in action_gaps if g.get("message") == "duplicate_cash_dividend_removed")
    overlap_removed = sum(int(g["detail"].rsplit("removed=", 1)[1]) for g in action_gaps
                          if g.get("message") == "dividend_component_overlap_removed")
    wrong_issuer_removed = sum(1 for g in action_gaps if g.get("message") == "wrong_issuer_dividend_quarantined"
                               and (m := re.search(r"ex=(\d{4}-\d{2}-\d{2})", g["detail"]))
                               and m.group(1) <= last_observed_session)
    coverage = {
        "dataset_kind": "real_partial", "stage": 2,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "series_id": TARGET_SERIES_ID, "voo_class_id": TARGET_CLASS_ID,
        "nport_parsed_series": len(snapshots),
        "date_range_holdings_as_of": {"start": snapshots[0].holdings_as_of.isoformat(), "end": snapshots[-1].holdings_as_of.isoformat()},
        "counts_source": "security_id=CUSIP when valid, otherwise ISIN/fallback; N/A CUSIPs never merged",
        "unique_security_ids": len(sec_rows), "unique_holdings_excluding_voo": len(keys),
        "mapped_tickers": sum(1 for key in keys if mapping.get(key) and mapping[key].ticker),
        "unmapped_holdings": sum(1 for key in keys if not mapping.get(key) or not mapping[key].ticker),
        "openfigi_status_counts": dict(figi_status_counts),
        "reit_excluded": len(reit_keys), "reit_review_rows": len(reit_rows),
        "reit_unresolved": sum(r["decision"] == "review_not_excluded_without_corroboration" for r in reit_rows),
        "price_window": {"start": price_start, "end": price_end, "autype": "NONE"},
        "session_count": session_count,
        "last_observed_session": last_observed_session,
        "future_announced_events_retained_in_raw_only": len(future_events),
        "kline_unique_tickers": len(unique_downloads), "kline_uncached_tickers_before_download": len(uncached_tickers),
        "price_coverage": {"full": full, "partial": partial, "none": none},
        "price_coverage_note": "full/partial uses internal coverage between first and last available bar; none includes unmapped",
        "price_gap_categories": dict(gap_categories), "unpriced_categories": dict(unpriced_categories),
        "corporate_action_events": {"split": event_counts.get("split", 0), "reverse_split": event_counts.get("reverse_split", 0),
                                    "spinoff": event_counts.get("spinoff", 0), "cash_dividend": event_counts.get("cash_dividend", 0),
                                    "total": len(event_rows)},
        "cash_dividends_before_dedup": event_counts.get("cash_dividend", 0) + duplicate_divs_removed + overlap_removed + wrong_issuer_removed,
        "duplicate_cash_dividends_removed": duplicate_divs_removed,
        "dividend_component_overlap_removed": overlap_removed,
        "wrong_issuer_cash_records_removed_in_observed_window": wrong_issuer_removed,
        "split_price_checks": {"total": len(split_checks), "passed": sum(c["passes"] == "1" for c in split_checks),
                               "failed": sum(c["passes"] != "1" for c in split_checks)},
        "corporate_action_gaps": len(action_gaps),
        "price_jump_unexplained": sum(1 for g in action_gaps if g.get("message") == "abs_close_change_ge_40pct_without_event"),
        "industry_hits": industry_hits, "industry_coverage": round(industry_hits / max(len(keys), 1), 6),
        "industry_note": "current classification, not historical; full 145 files plus individual get_owner_plate retry",
        "industry_gaps": len(industry_gaps),
        "opend_quota_before": quota_before, "opend_quota_after": quota_after,
        "opend_requests": fetcher.request_count, "score_coverage_gate": gate,
        "gate_note": "engineering gate, unvalidated", "elapsed_seconds": time.perf_counter() - started,
        "limitations": ["current industry classification, not historical", "spinoffs identified but not accounted in Stage 2",
                        "historical delisted members without free quotes remain in coverage denominator",
                        "no strategy backtest or return comparison"],
    }
    _write_csv(work_dir / "stage2_identity_issues.csv", ["category", "severity", "security_id", "message", "detail"], identity_issues)
    _write_csv(work_dir / "stage2_price_gaps.csv", ["security_id", "ticker", "gap_category", "detail", "n_bars"], price_gaps)
    _write_csv(work_dir / "stage2_corporate_action_gaps.csv",
               ["category", "severity", "security_id", "message", "detail", "effective_session", "previous_session",
                "previous_close", "close", "conclusion", "review_note"], action_gaps)
    _write_csv(work_dir / "stage2_industry_gaps.csv", ["category", "severity", "security_id", "message", "detail"], industry_gaps)
    _write_csv(work_dir / "stage2_opend_request_log.csv", ["step", "used", "remain", "raw"], opend_log)
    _write_csv(work_dir / "stage2_split_price_checks.csv", list(split_checks[0].keys()) if split_checks else
               ["event_id", "security_id", "event_type", "effective_session", "shares_new_per_old", "previous_session",
                "previous_raw_close", "event_raw_close", "observed_old_over_new", "relative_deviation", "tolerance", "passes", "source"],
               split_checks)
    (work_dir / "stage2_coverage_report.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fetcher.close()
    return coverage
