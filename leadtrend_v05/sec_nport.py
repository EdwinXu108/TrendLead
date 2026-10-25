"""SEC N-PORT download and parse for Vanguard 500 Index Fund (Series S000002839).

Does NOT use the old leadtrend package. Official source only, rate-limited,
User-Agent carries the contact email required by SEC.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

class MissingSecUserAgent(RuntimeError):
    """Investor must fill config.v05_stage2.json sec_user_agent before any SEC download."""


def require_sec_user_agent(ua: str | None) -> str:
    if not ua or not str(ua).strip():
        raise MissingSecUserAgent(
            "config.sec_user_agent is empty. Fill an identifiable User-Agent with "
            "contact info (SEC requirement) in config/v05_stage2.json before "
            "running download commands."
        )
    return str(ua).strip()


def parse_acceptance_datetime_et_to_utc(text_or_ts: str) -> datetime:
    """Parse SEC ACCEPTANCE-DATETIME (Eastern local, e.g. 20260226161530) → UTC.

    The submissions JSON acceptanceDateTime is NOT used: it is consistently
    ~5 hours later than the filing header / index page "Accepted" time.
    """
    from .timeutil import NY

    s = text_or_ts.strip()
    if "<ACCEPTANCE-DATETIME>" in s:
        m = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", s)
        if not m:
            raise ValueError(f"no ACCEPTANCE-DATETIME in text: {s[:80]!r}")
        s = m.group(1)
    digits = re.sub(r"\D", "", s)
    if len(digits) != 14:
        raise ValueError(f"acceptance timestamp must be YYYYMMDDHHMMSS: {text_or_ts!r}")
    local = datetime(
        int(digits[0:4]), int(digits[4:6]), int(digits[6:8]),
        int(digits[8:10]), int(digits[10:12]), int(digits[12:14]),
        tzinfo=NY,
    )
    return local.astimezone(timezone.utc)


def fetch_filing_acceptance(
    accession: str,
    user_agent: str,
    raw_dir: Path,
) -> datetime:
    """Read ACCEPTANCE-DATETIME from the complete submission header (cached)."""
    acc_n = accession.replace("-", "")
    cache = raw_dir / f"header_{accession}.txt"
    if cache.is_file() and cache.stat().st_size > 0:
        text = cache.read_text(encoding="utf-8", errors="replace")[:20000]
    else:
        url = f"https://www.sec.gov/Archives/edgar/data/36405/{acc_n}/{accession}.txt"
        raw = _http_get(url, user_agent)
        text = raw[:20000].decode("utf-8", errors="replace")
        raw_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    return parse_acceptance_datetime_et_to_utc(text)

TARGET_SERIES_ID = "S000002839"
TARGET_CLASS_ID = "C000092055"  # VOO ETF shares
TARGET_REG_CIK = "0000036405"

NS = {
    "nport": "http://www.sec.gov/edgar/nport",
    "com": "http://www.sec.gov/edgar/common",
    "ncom": "http://www.sec.gov/edgar/nportcommon",
}


@dataclass
class Holding:
    name: str
    cusip: str
    isin: str
    lei: str
    title: str
    balance: float
    units: str
    currency: str
    val_usd: float
    pct_val: float
    asset_cat: str
    issuer_cat: str
    inv_country: str
    is_restricted: str
    payoff_profile: str


@dataclass
class NPortSnapshot:
    accession: str
    series_id: str
    series_name: str
    class_ids: list[str]
    holdings_as_of: date
    available_at: datetime  # SEC acceptance (public)
    filing_date: date
    retrieved_at: datetime
    source_url: str
    source_sha256: str
    local_path: str
    total_assets: float | None
    net_assets: float | None
    holdings: list[Holding] = field(default_factory=list)
    non_equity_count: int = 0
    parse_notes: list[str] = field(default_factory=list)

    @property
    def has_voo_class(self) -> bool:
        return TARGET_CLASS_ID in self.class_ids


def _http_get(url: str, user_agent: str, timeout: int = 60, max_retries: int = 3) -> bytes:
    import gzip
    import zlib

    ua = require_sec_user_agent(user_agent)
    headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            time.sleep(0.2)  # <= 5 req/s
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                elif enc == "deflate":
                    raw = zlib.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in {403, 429}:
                # backoff, then retry up to max_retries; do not loop forever
                time.sleep(2.0 * (attempt + 1))
                continue
            time.sleep(1.0 + attempt)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"SEC fetch failed for {url}: {last_err}")


def fetch_submissions(cik: str = TARGET_REG_CIK, user_agent: str = "") -> dict:
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    return json.loads(_http_get(url, user_agent).decode("utf-8"))


def find_voo_nport_accessions(
    submissions: dict,
    report_dates: list[str] | None = None,
) -> list[dict]:
    """Return NPORT-P filings whose primary_doc is Series S000002839.

    We identify by downloading primary_doc and checking seriesId (cannot assume
    all historical files share one CIK layout — we verify each one).
    """
    recent = submissions["filings"]["recent"]
    out = []
    for i, form in enumerate(recent["form"]):
        if form != "NPORT-P":
            continue
        rd = recent["reportDate"][i]
        if report_dates and rd not in report_dates:
            continue
        acc = recent["accessionNumber"][i]
        out.append(
            {
                "accession": acc,
                "report_date": rd,
                "filing_date": recent["filingDate"][i],
                "acceptance_datetime": recent["acceptanceDateTime"][i],
            }
        )
    return out


def parse_nport_xml(raw: bytes, meta: dict, local_path: Path) -> NPortSnapshot:
    text = raw.decode("utf-8", errors="replace")
    # namespace-agnostic parse
    root = ET.fromstring(raw)

    def findtext(*tags: str) -> str:
        for t in tags:
            el = root.find(f".//{{{NS['nport']}}}{t}")
            if el is None:
                el = root.find(f".//{t}")
            if el is not None and el.text:
                return el.text.strip()
        return ""

    series_id = findtext("seriesId")
    series_name = findtext("seriesName")
    rep_pd = findtext("repPdDate", "repPdEnd")
    class_ids = [
        (el.text or "").strip()
        for el in root.findall(f".//{{{NS['nport']}}}classId")
    ]
    if not class_ids:
        class_ids = [(el.text or "").strip() for el in root.findall(".//classId")]

    if series_id != TARGET_SERIES_ID:
        raise ValueError(f"not target series: {series_id!r} != {TARGET_SERIES_ID}")

    holdings: list[Holding] = []
    non_equity = 0
    notes: list[str] = []
    for inv in root.findall(f".//{{{NS['nport']}}}invstOrSec"):
        def g(tag: str) -> str:
            el = inv.find(f"{{{NS['nport']}}}{tag}")
            if el is None:
                el = inv.find(tag)
            return (el.text or "").strip() if el is not None else ""

        def gfloat(tag: str) -> float:
            s = g(tag)
            try:
                return float(s) if s else 0.0
            except ValueError:
                return 0.0

        isin = ""
        for ident in inv.findall(f".//{{{NS['nport']}}}isin"):
            isin = ident.get("value", "") or isin
        if not isin:
            for ident in inv.findall(".//isin"):
                isin = ident.get("value", "") or isin

        asset_cat = g("assetCat")
        h = Holding(
            name=g("name"),
            cusip=g("cusip"),
            isin=isin,
            lei=g("lei"),
            title=g("title"),
            balance=gfloat("balance"),
            units=g("units"),
            currency=g("curCd") or "USD",
            val_usd=gfloat("valUSD"),
            pct_val=gfloat("pctVal"),
            asset_cat=asset_cat,
            issuer_cat=g("issuerCat"),
            inv_country=g("invCountry"),
            is_restricted=g("isRestrictedSec"),
            payoff_profile=g("payoffProfile"),
        )
        # Equity-common only for the stock universe (EC). Keep others as non-equity.
        if asset_cat == "EC" and h.cusip:
            holdings.append(h)
        else:
            non_equity += 1

    acc = meta.get("accession", "")
    # Prefer explicit available_at_utc (from filing header ACCEPTANCE-DATETIME).
    # Fall back to submissions JSON only if header unavailable — and record it.
    available_at = meta.get("available_at_utc")
    if available_at is None:
        acc_dt_raw = meta.get("acceptance_datetime", "")
        acc_dt = datetime.fromisoformat(acc_dt_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        notes.append("available_at from submissions JSON (may be ~5h late vs filing header)")
        available_at = acc_dt
    elif isinstance(available_at, str):
        available_at = datetime.fromisoformat(available_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    filing_day = date.fromisoformat(meta.get("filing_date", rep_pd))

    total_assets = None
    net_assets = None
    ta = findtext("totAssets")
    na = findtext("netAssets")
    try:
        total_assets = float(ta) if ta else None
    except ValueError:
        notes.append(f"totAssets unparsed: {ta!r}")
    try:
        net_assets = float(na) if na else None
    except ValueError:
        notes.append(f"netAssets unparsed: {na!r}")

    return NPortSnapshot(
        accession=acc,
        series_id=series_id,
        series_name=series_name,
        class_ids=class_ids,
        holdings_as_of=date.fromisoformat(rep_pd),
        available_at=available_at,
        filing_date=filing_day,
        retrieved_at=datetime.now(timezone.utc),
        source_url=f"https://www.sec.gov/Archives/edgar/data/36405/{acc.replace('-', '')}/primary_doc.xml",
        source_sha256=hashlib.sha256(raw).hexdigest(),
        local_path=str(local_path),
        total_assets=total_assets,
        net_assets=net_assets,
        holdings=holdings,
        non_equity_count=non_equity,
        parse_notes=notes,
    )


def download_and_parse(
    accession: str,
    report_date: str,
    filing_date: str,
    acceptance_datetime: str,
    raw_dir: Path,
    user_agent: str = "",
) -> NPortSnapshot:
    acc_n = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/36405/{acc_n}/primary_doc.xml"
    local = raw_dir / f"nport_{TARGET_SERIES_ID}_{report_date}_{accession}.xml"
    from_cache = local.is_file() and local.stat().st_size > 0
    if from_cache:
        raw = local.read_bytes()
    else:
        raw = _http_get(url, user_agent)
        raw_dir.mkdir(parents=True, exist_ok=True)
        local.write_bytes(raw)

    # Authoritative acceptance time from filing header (ET → UTC); never silently
    # substitute the different submissions-JSON timestamp.
    try:
        acc_utc = fetch_filing_acceptance(accession, user_agent, raw_dir)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"filing header acceptance unavailable for {accession}: {exc}") from exc

    meta = {
        "accession": accession,
        "report_date": report_date,
        "filing_date": filing_date,
        "acceptance_datetime": acceptance_datetime,
        "available_at_utc": acc_utc.isoformat().replace("+00:00", "Z") if acc_utc else None,
    }
    snap = parse_nport_xml(raw, meta, local)
    if from_cache:
        snap.retrieved_at = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
    if not snap.has_voo_class:
        snap.parse_notes.append(f"VOO class {TARGET_CLASS_ID} not listed in classIds")
    return snap
