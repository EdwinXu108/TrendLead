"""OpenD read-only market data with explicit raw prices and traceable actions."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# OpenD sometimes encodes a distribution adjustment in its *split* fields.
# These are independently confirmed events, not ratio-based guesses.
VERIFIED_SPINOFFS = {
    ("IBM", "2021-11-04"): "https://newsroom.ibm.com/2021-11-03-IBM-Completes-the-Separation-of-Kyndryl",
    ("MRK", "2021-06-03"): "https://www.merck.com/news/merck-announces-completion-of-organon-co-spinoff/",
    ("DELL", "2021-11-02"): "https://www.sec.gov/Archives/edgar/data/1571996/000119312521315488/d146470d8k.htm",
    ("BFH", "2021-11-08"): "https://newsroom.breadfinancial.com/alliance-data-completes-spinoff-loyaltyone-segment",
    ("RTX", "2020-04-03"): "https://www.sec.gov/Archives/edgar/data/101829/000114036120008397/nc10010681x2_8k.htm",
    ("O", "2021-11-15"): "https://www.realtyincome.com/investors/press-releases/realty-income-completes-spin-orion-office-reit",
}

VERIFIED_DIVIDEND_TOTALS = {
    ("BX", "2022-04-29", "2022-05-09"): (1.32, "https://ir.blackstone.com/tax-info-dividends/default.aspx"),
    ("BX", "2022-07-29", "2022-08-08"): (1.27, "https://ir.blackstone.com/tax-info-dividends/default.aspx"),
    ("BX", "2022-10-28", "2022-11-07"): (0.90, "https://ir.blackstone.com/tax-info-dividends/default.aspx"),
    ("FCX", "2023-04-13", "2023-05-01"): (0.15, "https://investors.fcx.com/investors/news-releases/news-release-details/2023/Freeport-McMoRan-Declares-Quarterly-Cash-Dividends-on-Common-Stock/default.aspx"),
    ("FCX", "2023-07-13", "2023-08-01"): (0.15, "https://investors.fcx.com/investors/news-releases/news-release-details/2023/Freeport-McMoRan-Declares-Quarterly-Cash-Dividends-on-Common-Stock-7d3d6c239/default.aspx"),
    ("FCX", "2024-01-11", "2024-02-01"): (0.15, "https://investors.fcx.com/investors/news-releases/news-release-details/2023/Freeport-McMoRan-Declares-Quarterly-Cash-Dividends-on-Common-Stock-99dc84154/default.aspx"),
}


@dataclass
class PriceBar:
    session_date: date
    open: float
    close: float
    high: float
    low: float
    volume: float | None
    turnover: float | None


@dataclass
class FetchOutcome:
    ticker: str
    security_id: str
    ok: bool
    n_bars: int
    error: str
    from_cache: bool
    gap_category: str = ""


def finite_float(value: Any) -> float | None:
    """Return a finite float, or None for blank/NaN/inf/unparseable values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if str(value).strip() == "":
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _positive_pair(record: dict, base: str, ert: str) -> tuple[float, float] | None:
    b, e = finite_float(record.get(base)), finite_float(record.get(ert))
    if b is None or e is None or b <= 0 or e <= 0:
        return None
    return b, e


def optional_market_number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    result = finite_float(value)
    if result is None or result < 0:
        raise ValueError("invalid non-finite/negative volume or turnover")
    return result


def _event_id(prefix: str, sid: str, ex: str, semantic: str) -> str:
    digest = hashlib.sha256(f"{sid}|{ex}|{semantic}".encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{sid}_{ex}_{digest}"


def normalize_rehab_rows(records: Iterable[dict], sid: str, code: str, start_d: date,
                         end_d: date, gaps_out: list[dict]) -> list[dict]:
    """Create only evidenced split/reverse-split/spinoff events.

    Dividend-only rehab rows have blank split/join fields and produce no split.
    Spin-off adjustment factors are evidence, not parent-share conversion ratios.
    """
    rows: list[dict] = []
    for record in records:
        ex = str(record.get("ex_div_date") or "")[:10]
        try:
            ex_d = date.fromisoformat(ex)
        except ValueError:
            continue
        if ex_d < start_d or ex_d > end_d:
            continue
        split_pair = _positive_pair(record, "split_base", "split_ert")
        join_pair = _positive_pair(record, "join_base", "join_ert")
        spin_pair = _positive_pair(record, "spin_off_base", "spin_off_ert")
        spin_ratio = finite_float(record.get("spin_off_ratio"))
        verified_spin_source = VERIFIED_SPINOFFS.get((code.removeprefix("US."), ex))
        if verified_spin_source and split_pair is not None:
            # Preserve the vendor factor as evidence, never as parent shares.
            spin_pair, split_pair = split_pair, None
        has_spin_field = spin_pair is not None or (spin_ratio is not None and spin_ratio > 0)
        if split_pair is not None:
            sb, se = split_pair
            ratio = se / sb
            if math.isfinite(ratio) and ratio > 0:
                rows.append({
                    "event_id": _event_id("CA_SPLIT", sid, ex, f"split:{sb}:{se}"),
                    "security_id": sid, "event_type": "split", "effective_session": ex,
                    "shares_new_per_old": f"{ratio:.10g}", "cash_amount_per_share": "",
                    "cash_basis": "", "pay_date": "",
                    "source": f"opend_get_rehab:{code};split_base={sb};split_ert={se}",
                })
        if join_pair is not None:
            jb, je = join_pair
            ratio = je / jb
            if math.isfinite(ratio) and ratio > 0:
                rows.append({
                    "event_id": _event_id("CA_REVERSE", sid, ex, f"join:{jb}:{je}"),
                    "security_id": sid, "event_type": "reverse_split", "effective_session": ex,
                    "shares_new_per_old": f"{ratio:.10g}", "cash_amount_per_share": "",
                    "cash_basis": "", "pay_date": "",
                    "source": f"opend_get_rehab:{code};join_base={jb};join_ert={je}",
                })
        if has_spin_field:
            evidence = f"opend_get_rehab:{code}"
            if spin_pair is not None:
                sb, se = spin_pair
                evidence += f";spin_off_base={sb};spin_off_ert={se};price_factor={se / sb:.10g}"
            if spin_ratio is not None:
                evidence += f";spin_off_ratio={spin_ratio}"
            if verified_spin_source:
                evidence += f";independently_verified_spinoff={verified_spin_source}"
            rows.append({
                "event_id": _event_id("CA_SPINOFF", sid, ex, evidence),
                "security_id": sid, "event_type": "spinoff", "effective_session": ex,
                "shares_new_per_old": "", "cash_amount_per_share": "", "cash_basis": "",
                "pay_date": "", "source": evidence + ";identified_only_no_stage2_accounting",
            })
    return rows


def parse_dividend_statement(statement: str) -> float | None:
    """Parse a USD statement into cash per entitled share."""
    text = str(statement or "").strip()
    patterns = [
        r"(?P<shares>\d+(?:\.\d+)?)\s*(?:股|shares?)\s*(?:派息|dividend)?\s*(?P<amount>\d+(?:\.\d+)?)\s*USD",
        r"(?:派息|dividend)\s*(?P<amount>\d+(?:\.\d+)?)\s*USD",
        r"(?P<amount>\d+(?:\.\d+)?)\s*USD\s*(?:per|/)\s*(?:share|股)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        amount = finite_float(match.group("amount"))
        shares = finite_float(match.groupdict().get("shares")) or 1.0
        if amount is not None and amount > 0 and shares > 0:
            result = amount / shares
            return result if math.isfinite(result) else None
    return None


def normalize_dividend_rows(items: Iterable[dict], sid: str, code: str, start_d: date,
                            end_d: date, gaps_out: list[dict]) -> list[dict]:
    """Normalize and semantically de-duplicate cash dividends."""
    unique: dict[tuple[str, str, str], dict] = {}
    for item in items:
        ex = str(item.get("ex_date") or "").replace("/", "-")[:10]
        pay = str(item.get("dividend_payable_date") or "").replace("/", "-")[:10]
        statement = str(item.get("statement") or "")
        amount = parse_dividend_statement(statement)
        try:
            ex_d = date.fromisoformat(ex)
        except ValueError:
            ex_d = None
        if ex_d is not None and (ex_d < start_d or ex_d > end_d):
            continue
        if ex_d is None or amount is None or not pay:
            reason = "dividend_missing_pay_date" if ex_d is not None and amount is not None and not pay else "dividend_unparsed"
            gaps_out.append({"category": "corporate_actions", "severity": "error", "security_id": sid,
                             "message": reason, "detail": str(item)[:500]})
            continue
        try:
            date.fromisoformat(pay)
        except ValueError:
            gaps_out.append({"category": "corporate_actions", "severity": "error", "security_id": sid,
                             "message": "dividend_invalid_pay_date", "detail": str(item)[:500]})
            continue
        amount_text = f"{amount:.10g}"
        key = (ex, pay, amount_text)
        if key in unique:
            gaps_out.append({"category": "corporate_actions", "severity": "info", "security_id": sid,
                             "message": "duplicate_cash_dividend_removed",
                             "detail": f"ex={ex};pay={pay};amount={amount_text};statement={statement}"})
            continue
        unique[key] = {
            "event_id": _event_id("CA_DIV", sid, ex, f"{pay}:{amount_text}"),
            "security_id": sid, "event_type": "cash_dividend", "effective_session": ex,
            "shares_new_per_old": "", "cash_amount_per_share": amount_text,
            "cash_basis": "per_share_at_entitlement", "pay_date": pay,
            "source": f"opend_dividends:{code};statement={statement};record_date={item.get('record_date', '')}",
        }
    by_date: dict[tuple[str, str], list[dict]] = {}
    for row in unique.values():
        by_date.setdefault((row["effective_session"], row["pay_date"]), []).append(row)
    result = []
    for (ex, pay), group in by_date.items():
        verified = VERIFIED_DIVIDEND_TOTALS.get((code.removeprefix("US."), ex, pay))
        if len(group) > 1 and verified:
            total, source = verified
            matches = [r for r in group if abs(float(r["cash_amount_per_share"]) - total) < 1e-9]
            if len(matches) == 1:
                matches[0]["source"] += ";issuer_verified_total=" + source
                result.extend(matches)
                gaps_out.append({"category": "corporate_actions", "severity": "info", "security_id": sid,
                                 "message": "dividend_component_overlap_removed",
                                 "detail": f"ex={ex};pay={pay};issuer_total={total};source={source};removed={len(group)-1}"})
                continue
        if len(group) > 1:
            gaps_out.append({"category": "corporate_actions", "severity": "warning", "security_id": sid,
                             "message": "distinct_cash_dividends_require_source_review",
                             "detail": f"ex={ex};pay={pay};amounts=" + "|".join(r["cash_amount_per_share"] for r in group)})
        result.extend(group)
    return result


def verify_bny_dividend_identity(rows: list[dict], rehab_records: list[dict], sid: str,
                                 start_d: date, end_d: date, gaps: list[dict]) -> list[dict]:
    """BNY was a closed-end-fund ticker before the bank took it in May 2026.

    The dividend endpoint currently mixes both histories, while bank rehab and
    price history are distinct. Keep only endpoint cash records corroborated
    by the bank rehab dates/amounts. Rehab never supplies cash or pay dates.
    """
    expected = {}
    for r in rehab_records:
        ex = str(r.get("ex_div_date") or "")[:10]
        amount = finite_float(r.get("per_cash_div"))
        if start_d.isoformat() <= ex <= end_d.isoformat() and amount is not None and amount > 0:
            expected[ex] = amount
    kept = []
    for row in rows:
        ex, amount = row["effective_session"], float(row["cash_amount_per_share"])
        if ex in expected and abs(expected[ex] - amount) < 1e-7:
            kept.append(row)
        else:
            gaps.append({"category": "identity", "severity": "info", "security_id": sid,
                         "message": "wrong_issuer_dividend_quarantined",
                         "detail": f"BNY former closed-end fund feed contamination: ex={ex};cash={amount};"
                                   "bank rehab does not corroborate;source=https://www.bny.com/corporate/global/en/about-us/newsroom/press-release/bny-announces-planned-change-of-stock-ticker-symbol-to-bny-130465.html"})
    covered = {r["effective_session"] for r in kept}
    for ex in sorted(set(expected) - covered):
        gaps.append({"category": "corporate_actions", "severity": "error", "security_id": sid,
                     "message": "bank_dividend_missing_in_truncated_mixed_feed",
                     "detail": f"ex={ex};bank rehab indicates cash event;dividend API lacks verified amount/pay-date record;not synthesized"})
    return kept


class OpenDPriceFetcher:
    """Rate limit: <=50 requests/30s; retry request failures up to 3 times."""

    def __init__(self, cache_dir: Path, host: str = "127.0.0.1", port: int = 11111, autype: str = "NONE"):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.host = host
        self.port = port
        if autype not in {"NONE", "QFQ"}:
            raise ValueError("unsupported price adjustment mode")
        self.autype = autype
        self.request_count = 0
        self.errors: list[dict] = []
        self._ctx = None
        self._req_times: list[float] = []

    def _pace(self) -> None:
        now = time.time()
        self._req_times = [t for t in self._req_times if now - t < 30.0]
        if len(self._req_times) >= 50:
            sleep_for = 30.0 - (now - self._req_times[0]) + 0.05
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.time()
            self._req_times = [t for t in self._req_times if now - t < 30.0]
        self._req_times.append(time.time())

    def _get_ctx(self):
        if self._ctx is None:
            from futu import OpenQuoteContext
            self._ctx = OpenQuoteContext(host=self.host, port=self.port)
        return self._ctx

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None

    def _cache_path(self, ticker: str, start: str, end: str) -> Path:
        safe = ticker.replace(".", "_")
        return self.cache_dir / f"{self.autype}_{safe}_{start}_{end}.csv"

    def load_cache(self, ticker: str, start: str, end: str) -> list[PriceBar] | None:
        path = self._cache_path(ticker, start, end)
        if not path.is_file():
            return None
        rows: list[PriceBar] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                values = {k: finite_float(row.get(k)) for k in ("open", "close", "high", "low")}
                if any(v is None or v <= 0 for v in values.values()):
                    raise ValueError(f"invalid raw OHLC in {path}: {row}")
                rows.append(PriceBar(
                    session_date=date.fromisoformat(row["session_date"]),
                    open=values["open"], close=values["close"], high=values["high"], low=values["low"],
                    volume=optional_market_number(row.get("volume")),
                    turnover=optional_market_number(row.get("turnover")),
                ))
        return rows

    def save_cache(self, ticker: str, start: str, end: str, bars: list[PriceBar]) -> None:
        path = self._cache_path(ticker, start, end)
        if path.exists():
            raise FileExistsError(f"raw cache is append-only: {path}")
        with path.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["session_date", "open", "close", "high", "low", "volume", "turnover"])
            writer.writeheader()
            for bar in bars:
                writer.writerow({
                    "session_date": bar.session_date.isoformat(),
                    "open": f"{bar.open:.8f}", "close": f"{bar.close:.8f}",
                    "high": f"{bar.high:.8f}", "low": f"{bar.low:.8f}",
                    "volume": "" if bar.volume is None else f"{bar.volume:.4f}",
                    "turnover": "" if bar.turnover is None else f"{bar.turnover:.4f}",
                })
        self._write_meta(path, "request_history_kline", ticker, {"start": start, "end": end, "autype": self.autype})

    @staticmethod
    def _write_meta(path: Path, interface: str, code: str, params: dict) -> None:
        meta = {"interface": interface, "code": code, "params": params,
                "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    def _classify_error(self, msg: str) -> str:
        lowered = msg.lower()
        if "otc" in lowered:
            return "unsupported_venue"
        if "30秒" in msg or "频率" in msg or "frequency" in lowered or "rate" in lowered:
            return "request_failed_retryable"
        if "未知" in msg or "unknown" in lowered or "无效" in msg or "invalid" in lowered:
            return "unknown_security"
        return "request_failed_retryable"

    def fetch_one(self, ticker: str, security_id: str, start: str, end: str, max_retries: int = 3) -> FetchOutcome:
        cached = self.load_cache(ticker, start, end)
        if cached is not None:
            return FetchOutcome(ticker, security_id, True, len(cached), "", True, "ok")
        code = ticker if ticker.startswith("US.") else f"US.{ticker}"
        try:
            from futu import AuType, RET_OK
        except ImportError as exc:
            return FetchOutcome(ticker, security_id, False, 0, f"futu_import:{exc}", False, "request_failed_retryable")
        last_err = ""
        for attempt in range(max_retries):
            try:
                ctx = self._get_ctx()
                bars: list[PriceBar] = []
                page_key = None
                ret = RET_OK
                for _page in range(20):
                    self._pace()
                    self.request_count += 1
                    kwargs = dict(start=start, end=end, ktype="K_DAY", autype=getattr(AuType, self.autype),
                                  extended_time=False, max_count=1000)
                    if page_key is not None:
                        kwargs["page_req_key"] = page_key
                    ret, frame, page_key = ctx.request_history_kline(code, **kwargs)
                    if ret != RET_OK:
                        last_err = str(frame)
                        break
                    if frame is None or len(frame) == 0:
                        break
                    for _, row in frame.iterrows():
                        values = {k: finite_float(row.get(k)) for k in ("open", "close", "high", "low")}
                        if any(v is None or v <= 0 for v in values.values()):
                            raise ValueError(f"non-finite/nonpositive OHLC for {code}: {row.to_dict()}")
                        bars.append(PriceBar(
                            session_date=date.fromisoformat(str(row["time_key"])[:10]),
                            open=values["open"], close=values["close"], high=values["high"], low=values["low"],
                            volume=optional_market_number(row.get("volume")),
                            turnover=optional_market_number(row.get("turnover")),
                        ))
                    if not page_key:
                        break
                if ret != RET_OK:
                    category = self._classify_error(last_err)
                    if category == "request_failed_retryable" and attempt < max_retries - 1:
                        time.sleep(5.0 * (attempt + 1))
                        continue
                    self.errors.append({"ticker": ticker, "security_id": security_id, "error": last_err, "category": category})
                    return FetchOutcome(ticker, security_id, False, 0, last_err, False, category)
                if not bars:
                    return FetchOutcome(ticker, security_id, True, 0, "", False, "no_data")
                unique = {bar.session_date: bar for bar in bars}
                bars = sorted(unique.values(), key=lambda bar: bar.session_date)
                self.save_cache(ticker, start, end, bars)
                return FetchOutcome(ticker, security_id, True, len(bars), "", False, "ok")
            except Exception as exc:
                last_err = str(exc)
                if attempt < max_retries - 1:
                    time.sleep(5.0 * (attempt + 1))
                    continue
                self.errors.append({"ticker": ticker, "security_id": security_id, "error": last_err, "category": "request_failed_retryable"})
                return FetchOutcome(ticker, security_id, False, 0, last_err, False, "request_failed_retryable")
        return FetchOutcome(ticker, security_id, False, 0, last_err, False, "request_failed_retryable")

    def query_kline_quota(self) -> dict:
        try:
            self._pace()
            ctx = self._get_ctx()
            fn = getattr(ctx, "get_history_kl_quota", None)
            if fn is None:
                return {"used": None, "remain": None, "note": "quota_api_unavailable"}
            self.request_count += 1
            ret, data = fn()
            if ret != 0:
                return {"used": None, "remain": None, "note": f"quota_ret={ret}"}
            if isinstance(data, (list, tuple)) and len(data) >= 2:
                try:
                    return {"used": int(data[0]), "remain": int(data[1]), "raw": str(data)[:200]}
                except Exception:
                    pass
            return {"used": None, "remain": None, "raw": str(data)[:200]}
        except Exception as exc:
            return {"used": None, "remain": None, "note": f"quota_error:{exc}"}

    def _ca_call(self, label: str, sid: str, invoke, gaps_out: list[dict], max_retries: int = 3):
        last = ""
        for attempt in range(max_retries):
            try:
                self._pace()
                self.request_count += 1
                ret, data = invoke()
                if ret == 0:
                    return data
                last = str(data)
            except Exception as exc:
                last = str(exc)
            if attempt < max_retries - 1:
                time.sleep(3.0 * (attempt + 1))
        gaps_out.append({"category": "corporate_actions", "severity": "error", "security_id": sid,
                         "message": f"{label}_request_failed", "detail": last[:1000]})
        return None

    def fetch_corporate_actions_for(self, pairs: list[tuple[str, str]], start: str, end: str,
                                    gaps_out: list[dict], cache_dir: Path | None = None) -> list[dict]:
        rows: list[dict] = []
        try:
            ctx = self._get_ctx()
        except Exception as exc:
            gaps_out.append({"category": "corporate_actions", "severity": "error", "security_id": "*",
                             "message": "opend_unavailable", "detail": str(exc)})
            return rows
        start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
        for idx, (key, ticker) in enumerate(pairs, 1):
            if idx % 20 == 0:
                print(f"[ca] {idx}/{len(pairs)}", flush=True)
            code = ticker if str(ticker).startswith("US.") else f"US.{ticker}"
            sid = "VOO_ETF" if key == "VOO_ETF" else security_id_from_key(key)
            safe = code.replace(".", "_")
            rehab_path = cache_dir / f"rehab_{safe}.csv" if cache_dir else None
            div_path = cache_dir / f"dividends_{safe}.json" if cache_dir else None
            rehab_records = None
            if rehab_path is not None and rehab_path.is_file():
                import pandas as pd
                rehab_records = pd.read_csv(rehab_path).to_dict("records")
            else:
                frame = self._ca_call("rehab", sid, lambda: ctx.get_rehab(code), gaps_out)
                if frame is not None:
                    rehab_records = frame.to_dict("records")
                    if rehab_path is not None:
                        frame.to_csv(rehab_path, index=False, encoding="utf-8-sig")
                        self._write_meta(rehab_path, "get_rehab", code, {})
            if rehab_records is not None:
                rows.extend(normalize_rehab_rows(rehab_records, sid, code, start_d, end_d, gaps_out))
            dividend_items = None
            if div_path is not None and div_path.is_file():
                payload = json.loads(div_path.read_text(encoding="utf-8"))
                dividend_items = payload.get("dividend_list") or []
            else:
                payload = self._ca_call("dividends", sid, lambda: ctx.get_corporate_actions_dividends(code), gaps_out)
                if isinstance(payload, dict):
                    dividend_items = payload.get("dividend_list") or []
                    if div_path is not None:
                        div_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
                        self._write_meta(div_path, "get_corporate_actions_dividends", code, {})
            if dividend_items is not None:
                dividend_rows = normalize_dividend_rows(dividend_items, sid, code, start_d, end_d, gaps_out)
                if code == "US.BNY":
                    dividend_rows = verify_bny_dividend_identity(dividend_rows, rehab_records or [], sid, start_d, end_d, gaps_out)
                    gaps_out[:] = [g for g in gaps_out if not (g.get("security_id") == sid
                                    and g.get("message") == "distinct_cash_dividends_require_source_review")]
                rows.extend(dividend_rows)
        return rows


def valid_cusip(value: str) -> bool:
    text = str(value or "").strip().upper()
    return bool(re.fullmatch(r"[0-9A-Z]{9}", text)) and text != "000000000"


def holding_key(holding: Any) -> str:
    """Stable key that never merges unrelated N/A-CUSIP holdings."""
    cusip = str(getattr(holding, "cusip", "") or "").strip().upper()
    if cusip and cusip not in {"N/A", "NA", "NONE", "000000000"}:
        return cusip
    isin = str(getattr(holding, "isin", "") or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}[0-9A-Z]{10}", isin):
        return f"ISIN:{isin}"
    lei = str(getattr(holding, "lei", "") or "").strip().upper()
    name = re.sub(r"\s+", " ", str(getattr(holding, "name", "") or "").strip().upper())
    digest = hashlib.sha256(f"{lei}|{name}".encode("utf-8")).hexdigest()[:12]
    return f"UNIDENTIFIED:{digest}"


def security_id_from_key(key: str) -> str:
    if key.startswith(("ISIN:", "UNIDENTIFIED:")):
        return key
    return f"CUSIP:{key}"
