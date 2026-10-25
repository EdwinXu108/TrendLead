"""Traceable CUSIP/ISIN mapping; no name-prefix or instrument-type guessing."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .opend_prices import holding_key, security_id_from_key, valid_cusip
from .sec_nport import require_sec_user_agent


@dataclass
class TickerMapEntry:
    cusip: str
    name: str
    ticker: str
    cik: int | None
    matched_title: str
    match_method: str
    evidence: str = ""
    rejected_reason: str = ""
    key: str = ""


_REJECT_TICKER = re.compile(r"(-WT|-WS|\.WS|-U$|-R$|-P[A-Z]|-PR[A-Z]?|^[A-Z]{4,}P$|-WD)", re.I)
_REJECT_NAME = re.compile(r"\b(WARRANT|WARRANTS|PREFERRED|PFD|DEPOSITARY SH PFD|UNITS?|NOTES?|BONDS?)\b", re.I)
_US_MIC = {"XNYS", "XNAS", "XNCM", "XNGS", "XNMS", "ARCX", "BATS", "IEXG", "XASE", "XCHI", "XPHL", "XBOS"}


def _norm(s: str) -> str:
    text = re.sub(r"[^A-Z0-9 ]", " ", str(s).upper())
    text = re.sub(r"\b(INC|CORP|CORPORATION|CO|COMPANY|LTD|LLC|LP|PLC|HOLDINGS?|GROUP|THE|CL [A-Z]|CLASS [A-Z]|NEW|DEL|ORD|SHS|COMMON)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_rejected_instrument(ticker: str, name: str) -> str:
    if _REJECT_TICKER.search(ticker or ""):
        return f"ticker_pattern:{ticker}"
    if _REJECT_NAME.search(name or ""):
        return f"name_pattern:{name}"
    return ""


def load_sec_company_tickers(cache_path: Path, user_agent: str = "") -> list[dict]:
    if cache_path.is_file():
        raw = cache_path.read_bytes()
    else:
        ua = require_sec_user_agent(user_agent)
        req = urllib.request.Request("https://www.sec.gov/files/company_tickers.json",
                                     headers={"User-Agent": ua, "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(raw)
    return list(json.loads(raw.decode("utf-8")).values())


def _openfigi_job(key: str) -> dict | None:
    if valid_cusip(key):
        return {"idType": "ID_CUSIP", "idValue": key}
    if key.startswith("ISIN:"):
        return {"idType": "ID_ISIN", "idValue": key[5:]}
    return None


def _select_openfigi_candidate(data: list[dict]) -> tuple[str, str]:
    candidates: dict[str, list[str]] = {}
    for row in data:
        ticker = str(row.get("ticker") or "").strip().upper()
        market_sector = str(row.get("marketSector") or "")
        exch = str(row.get("exchCode") or "")
        mic = str(row.get("micCode") or "").upper()
        sec_type = str(row.get("securityType") or "")
        name = str(row.get("name") or "")
        if not ticker or (market_sector and market_sector != "Equity"):
            continue
        if is_rejected_instrument(ticker, f"{sec_type} {name}"):
            continue
        if mic:
            if mic not in _US_MIC:
                continue
        elif exch != "US":
            continue
        candidates.setdefault(ticker, []).append(f"{name};exch={exch};mic={mic};type={sec_type}")
    if len(candidates) != 1:
        return "", f"openfigi_ambiguous_candidates:{json.dumps(candidates, ensure_ascii=False)}" if candidates else "openfigi_no_eligible_us_equity"
    ticker = next(iter(candidates))
    return ticker, "openfigi:" + candidates[ticker][0]


def prefetch_openfigi(holdings: list, cache_path: Path, raw_dir: Path, *, include_unlisted: bool = False) -> dict:
    """Batch CUSIP/ISIN jobs, <=25 requests/min, 429 backoff max 3."""
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    keys = sorted({holding_key(h) for h in holdings if _openfigi_job(holding_key(h))})
    pending = [k for k in keys if cache.get(k, {}).get("status") != "resolved"
               and (cache.get(k, {}).get("status") != "no_hit"
                    or (include_unlisted and not cache.get(k, {}).get("include_unlisted")))]
    raw_dir.mkdir(parents=True, exist_ok=True)
    for offset in range(0, len(pending), 10):
        batch = pending[offset:offset + 10]
        jobs = [_openfigi_job(k) for k in batch]
        if include_unlisted:
            jobs = [dict(job, includeUnlistedEquities=True) for job in jobs]
        body = json.dumps(jobs).encode("utf-8")
        payload = None
        last_error = ""
        for attempt in range(3):
            req = urllib.request.Request("https://api.openfigi.com/v3/mapping", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                if exc.code != 429 or attempt == 2:
                    break
                time.sleep(15.0 * (attempt + 1))
            except Exception as exc:
                last_error = str(exc)
                if attempt < 2:
                    time.sleep(5.0 * (attempt + 1))
        if payload is None:
            for key in batch:
                cache[key] = {"ticker": "", "status": "retryable", "evidence": f"openfigi_error:{last_error}"}
        else:
            digest = hashlib.sha256(body).hexdigest()[:16]
            raw_path = raw_dir / f"openfigi_batch_{digest}.json"
            if not raw_path.exists():
                raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                raw_path.with_suffix(".meta.json").write_text(json.dumps({
                    "url": "https://api.openfigi.com/v3/mapping", "jobs": body.decode("utf-8"),
                    "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                }, indent=2) + "\n", encoding="utf-8")
            for key, item in zip(batch, payload):
                data = item.get("data") or []
                if not data:
                    error = str(item.get("error", ""))
                    # A malformed request/server error does not prove no security exists.
                    no_hit = "No identifier found" in error or "No identifier found" in str(item.get("warning", ""))
                    cache[key] = {"ticker": "", "status": "no_hit" if no_hit else "retryable",
                                  "evidence": f"openfigi_response:{item}"}
                    continue
                ticker, evidence = _select_openfigi_candidate(data)
                cache[key] = {"ticker": ticker, "status": "resolved" if ticker else "no_hit",
                              "evidence": evidence}
        for key in batch:
            cache[key]["include_unlisted"] = include_unlisted
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[openfigi] {min(offset + 10, len(pending))}/{len(pending)}", flush=True)
        if offset + 10 < len(pending):
            time.sleep(2.6)
    return cache


def map_cusip_openfigi(key: str, cache_path: Path) -> tuple[str, str]:
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    rec = cache.get(key, {})
    return rec.get("ticker", ""), rec.get("evidence", "openfigi_not_queried")


def prefetch_isin_fallback(holdings: list, cache_path: Path, raw_dir: Path) -> None:
    """CINS/retired CUSIP misses can still resolve through the reported ISIN.

    Keep the original security key; this is identifier evidence, not a rename
    inference from company names.
    """
    from types import SimpleNamespace
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    by_key: dict[str, set[str]] = {}
    for h in holdings:
        key = holding_key(h)
        isin = str(getattr(h, "isin", "") or "").strip().upper()
        if cache.get(key, {}).get("status") != "resolved" and re.fullmatch(r"[A-Z]{2}[0-9A-Z]{10}", isin):
            by_key.setdefault(key, set()).add(isin)
    probes = [SimpleNamespace(cusip="N/A", isin=isin, lei="", name="ISIN cross-check")
              for isin in sorted({i for values in by_key.values() for i in values})]
    prefetch_openfigi(probes, cache_path, raw_dir, include_unlisted=True)
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    for key, isins in by_key.items():
        hits = [cache["ISIN:" + i] for i in sorted(isins) if cache.get("ISIN:" + i, {}).get("status") == "resolved"]
        tickers = {h["ticker"] for h in hits}
        if len(tickers) == 1:
            cache[key] = {"ticker": next(iter(tickers)), "status": "resolved", "include_unlisted": True,
                          "evidence": "N-PORT ISIN cross-check:" + "|".join(sorted(isins)) + ";" + hits[0]["evidence"]}
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_shareclass_links(cache_path: Path, raw_dir: Path) -> None:
    """Join retired identifiers through FIGI's stable shareClassFIGI evidence.

    Require one US ticker, never a company-level FIGI/name match. This also
    preserves the distinction between two voting classes of one issuer.
    """
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    classes_by_key: dict[str, set[str]] = {}
    us_by_class: dict[str, list[dict]] = {}
    evidence_by_key: dict[str, list[str]] = {}
    for meta_path in sorted(raw_dir.glob("*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        jobs = json.loads(meta["jobs"])
        payload = json.loads(meta_path.with_name(meta_path.name.replace(".meta.json", ".json")).read_text(encoding="utf-8"))
        for job, result in zip(jobs, payload):
            if job["idType"] not in {"ID_CUSIP", "ID_ISIN"}:
                continue
            key = ("ISIN:" if job["idType"] == "ID_ISIN" else "") + job["idValue"]
            for rec in result.get("data", []):
                shareclass = rec.get("shareClassFIGI")
                if not shareclass or rec.get("marketSector") != "Equity":
                    continue
                classes_by_key.setdefault(key, set()).add(shareclass)
                evidence_by_key.setdefault(key, []).append(meta_path.name)
                if rec.get("exchCode") == "US" and rec.get("securityType") == "Common Stock":
                    us_by_class.setdefault(shareclass, []).append(rec)
    for key, classes in classes_by_key.items():
        if cache.get(key, {}).get("status") == "resolved" or len(classes) != 1:
            continue
        shareclass = next(iter(classes))
        ticker, evidence = _select_openfigi_candidate(us_by_class.get(shareclass, []))
        if ticker:
            cache[key] = {"ticker": ticker, "status": "resolved", "include_unlisted": True,
                          "evidence": f"same shareClassFIGI={shareclass};{evidence};raw_meta=" + "|".join(sorted(set(evidence_by_key[key])))}
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def map_holdings_to_tickers(holdings: list, ticker_rows: list[dict], figi_cache: Path | None = None,
                            use_openfigi: bool = False) -> tuple[dict[str, TickerMapEntry], list[dict]]:
    """Use all historical name variants and unique exact matches; never prefixes."""
    groups: dict[str, list] = {}
    for holding in holdings:
        groups.setdefault(holding_key(holding), []).append(holding)
    exact: dict[str, list[dict]] = {}
    for row in ticker_rows:
        exact.setdefault(_norm(str(row.get("title", ""))), []).append(row)
    mapping: dict[str, TickerMapEntry] = {}
    issues: list[dict] = []

    for key, members in groups.items():
        first = members[0]
        ticker, evidence, method, matched_title, cik = "", "", "unmatched", "", None
        if use_openfigi and figi_cache is not None:
            ticker, evidence = map_cusip_openfigi(key, figi_cache)
            if ticker:
                method = "openfigi_cusip" if valid_cusip(key) else "openfigi_isin"
        mapping[key] = TickerMapEntry(cusip=first.cusip, name=first.name, ticker=ticker, cik=cik,
                                      matched_title=matched_title, match_method=method,
                                      evidence=evidence, key=key)

    name_group_sizes: dict[str, int] = {}
    for members in groups.values():
        normalized = _norm(members[0].name or "")
        name_group_sizes[normalized] = name_group_sizes.get(normalized, 0) + 1

    by_isin: dict[str, set[str]] = {}
    for key, members in groups.items():
        isin = str(getattr(members[0], "isin", "") or "").upper()
        if isin and mapping[key].ticker:
            by_isin.setdefault(isin, set()).add(mapping[key].ticker)
    for key, members in groups.items():
        entry = mapping[key]
        if entry.ticker:
            continue
        isin = str(getattr(members[0], "isin", "") or "").upper()
        if isin and len(by_isin.get(isin, set())) == 1:
            entry.ticker = next(iter(by_isin[isin]))
            entry.match_method = "same_isin_crosswalk"
            entry.evidence = f"same ISIN {isin}; unique mapped ticker; no conflicting class"
            continue
        candidates: dict[tuple[str, int | None], dict] = {}
        for holding in members:
            for row in exact.get(_norm(holding.name or ""), []):
                ticker = str(row.get("ticker") or "").strip().upper()
                cik = int(row["cik_str"]) if row.get("cik_str") is not None else None
                if ticker and not is_rejected_instrument(ticker, holding.name or ""):
                    candidates[(ticker, cik)] = row
        if candidates and name_group_sizes.get(_norm(members[0].name or ""), 0) > 1:
            entry.match_method = "ambiguous_same_name_security_group"
            entry.evidence = "multiple securities share the N-PORT name; exact company name cannot prove share class"
            continue
        if len(candidates) == 1:
            (ticker, cik), row = next(iter(candidates.items()))
            entry.ticker, entry.cik = ticker, cik
            entry.matched_title = str(row.get("title") or "")
            entry.match_method = "exact_name"
            entry.evidence = f"unique SEC exact-name match across {len(members)} N-PORT variants:title={entry.matched_title}"
        elif len(candidates) > 1:
            entry.match_method = "ambiguous_exact_name"
            entry.evidence = f"multiple SEC exact-name candidates:{sorted(candidates)}"

    for key, entry in mapping.items():
        if entry.ticker:
            reject = is_rejected_instrument(entry.ticker, entry.name)
            if reject:
                entry.ticker = ""
                entry.match_method = "rejected_type"
                entry.rejected_reason = reject
                entry.evidence = reject
            elif re.search(r"[/-][A-Z]$", entry.ticker):
                original = entry.ticker
                entry.ticker = original[:-2] + "." + original[-1]
                entry.evidence += f";futu_ticker={entry.ticker} (from {original})"
            # FIGI supplies the security identity; SEC ticker corroboration
            # supplies CIK for systematic SIC/REIT checks, never a share class guess.
            matches = [r for r in ticker_rows if str(r.get("ticker", "")).upper().replace("-", ".") == entry.ticker]
            ciks = {int(r["cik_str"]) for r in matches if r.get("cik_str")}
            if len(ciks) == 1:
                entry.cik = next(iter(ciks))
                entry.evidence += f";SEC ticker corroboration CIK={entry.cik}"
        if not entry.ticker:
            issues.append({"category": "identity", "severity": "warning", "security_id": security_id_from_key(key),
                           "message": "ticker_rejected_instrument" if entry.match_method == "rejected_type" else "ticker_unmapped",
                           "detail": f"key={key};name={entry.name};method={entry.match_method};{entry.evidence}"})
    return mapping, issues
