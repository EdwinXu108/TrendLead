"""Load CSV/JSON research inputs and compute source hashes."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import BootstrapConfig
from .timeutil import parse_date, parse_datetime, parse_optional_bool


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class CalendarRow:
    session_date: date
    open_time_utc: datetime
    close_time_utc: datetime
    is_early_close: bool
    note: str


@dataclass
class SecurityRow:
    security_id: str
    issuer_id: str
    ticker: str
    ticker_valid_from: date | None
    ticker_valid_to: date | None
    security_type: str
    exchange: str
    industry: str
    industry_source_version: str
    currency: str


@dataclass
class SnapshotMeta:
    snapshot_id: str
    holdings_as_of: date
    available_at: datetime
    available_at_raw: str
    availability_precision: str
    source: str
    source_hash: str
    retrieved_at: datetime
    revision: str


@dataclass
class SnapshotMember:
    snapshot_id: str
    security_id: str
    original_name: str
    weight: float | None


@dataclass
class PriceRow:
    security_id: str
    session_date: date
    open: float
    close: float
    volume: float | None
    turnover: float | None
    currency: str


@dataclass
class EventRow:
    event_id: str
    security_id: str
    event_type: str
    effective_session: date
    shares_new_per_old: float | None
    cash_amount_per_share: float | None
    cash_basis: str
    pay_date: date | None
    source: str
    spinoff_value_per_old_share: float | None = None
    spinoff_valuation: str = ""


@dataclass
class Inputs:
    root: Path
    config: BootstrapConfig
    calendar: list[CalendarRow]
    securities: list[SecurityRow]
    snapshots: list[SnapshotMeta]
    members: list[SnapshotMember]
    prices: list[PriceRow]
    events: list[EventRow]
    file_hashes: dict[str, str] = field(default_factory=dict)
    data_issues: list[dict[str, Any]] = field(default_factory=list)

    def sessions_as_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "session_date": r.session_date,
                "open_time_utc": r.open_time_utc,
                "close_time_utc": r.close_time_utc,
                "is_early_close": r.is_early_close,
                "note": r.note,
            }
            for r in self.calendar
        ]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _opt_float(value: str | None) -> float | None:
    if value is None:
        return None
    s = value.strip()
    if s == "":
        return None
    result = float(s)
    if not math.isfinite(result):
        raise ValueError(f"non-finite numeric value: {s!r}")
    return result


def _opt_date(value: str | None) -> date | None:
    if value is None:
        return None
    s = value.strip()
    if s == "":
        return None
    return parse_date(s)


def load_inputs(config: BootstrapConfig, project_root: Path, *, score_only: bool = False) -> Inputs:
    input_dir = (project_root / config.input_dir).resolve()
    if not input_dir.is_dir():
        raise FileNotFoundError(f"input_dir not found: {input_dir}")

    data_issues: list[dict[str, Any]] = []
    file_hashes: dict[str, str] = {}

    def track(name: str) -> Path:
        p = input_dir / name
        if not p.is_file():
            raise FileNotFoundError(f"missing input file: {p}")
        file_hashes[name] = sha256_file(p)
        return p

    cal_path = track("calendar.csv")
    sec_path = track("securities.csv")
    snap_path = track("snapshots.csv")
    mem_path = track("snapshot_members.csv")
    px_path = track("prices.csv")
    ev_path = track("events.csv")

    calendar: list[CalendarRow] = []
    seen_sessions: set[date] = set()
    for i, row in enumerate(_read_csv(cal_path), start=2):
        sd = parse_date(row["session_date"])
        if sd in seen_sessions:
            data_issues.append(
                {
                    "category": "calendar",
                    "severity": "error",
                    "security_id": "",
                    "snapshot_id": "",
                    "event_id": "",
                    "session_date": sd.isoformat(),
                    "message": "duplicate session_date",
                    "context": f"calendar.csv:{i}",
                }
            )
            continue
        seen_sessions.add(sd)
        calendar.append(
            CalendarRow(
                session_date=sd,
                open_time_utc=parse_datetime(row["open_time_utc"]),
                close_time_utc=parse_datetime(row["close_time_utc"]),
                is_early_close=parse_optional_bool(row.get("is_early_close")),
                note=(row.get("note") or "").strip(),
            )
        )
    calendar.sort(key=lambda r: r.session_date)
    if not calendar:
        raise ValueError("calendar.csv is empty")

    securities: list[SecurityRow] = []
    sec_ids: set[str] = set()
    for i, row in enumerate(_read_csv(sec_path), start=2):
        sid = row["security_id"].strip()
        if sid in sec_ids:
            data_issues.append(
                {
                    "category": "identity",
                    "severity": "error",
                    "security_id": sid,
                    "snapshot_id": "",
                    "event_id": "",
                    "session_date": "",
                    "message": "duplicate security_id",
                    "context": f"securities.csv:{i}",
                }
            )
            continue
        sec_ids.add(sid)
        securities.append(
            SecurityRow(
                security_id=sid,
                issuer_id=row["issuer_id"].strip(),
                ticker=row["ticker"].strip(),
                ticker_valid_from=_opt_date(row.get("ticker_valid_from")),
                ticker_valid_to=_opt_date(row.get("ticker_valid_to")),
                security_type=row["security_type"].strip(),
                exchange=row["exchange"].strip(),
                industry=row["industry"].strip(),
                industry_source_version=row["industry_source_version"].strip(),
                currency=row.get("currency", "USD").strip() or "USD",
            )
        )

    snapshots: list[SnapshotMeta] = []
    snap_ids: set[str] = set()
    for i, row in enumerate(_read_csv(snap_path), start=2):
        sid = row["snapshot_id"].strip()
        if sid in snap_ids:
            data_issues.append(
                {
                    "category": "universe",
                    "severity": "error",
                    "security_id": "",
                    "snapshot_id": sid,
                    "event_id": "",
                    "session_date": "",
                    "message": "duplicate snapshot_id",
                    "context": f"snapshots.csv:{i}",
                }
            )
            continue
        snap_ids.add(sid)
        precision = (row.get("availability_precision") or "timestamp").strip().lower()
        raw_available = row["available_at"].strip()
        if precision == "date":
            d = parse_date(raw_available)
            sess = [
                {
                    "session_date": c.session_date,
                    "open_time_utc": c.open_time_utc,
                }
                for c in calendar
            ]
            from .timeutil import date_precision_available_at

            inst = date_precision_available_at(d, sess)
            if inst is None:
                # Date is known but no later session exists in this calendar.
                # Keep the snapshot (never usable inside this run) instead of dropping it.
                from datetime import datetime as _dt, time as _time, timedelta as _td
                from datetime import timezone as _tz

                inst = _dt.combine(d, _time(0, 0), tzinfo=_tz.utc) + _td(days=1)
                data_issues.append(
                    {
                        "category": "universe",
                        "severity": "info",
                        "security_id": "",
                        "snapshot_id": sid,
                        "event_id": "",
                        "session_date": d.isoformat(),
                        "message": "date-precision available_at has no later session in calendar; snapshot kept but not usable in this run",
                        "context": f"snapshots.csv:{i}",
                    }
                )
            available_at = inst
        elif precision == "timestamp":
            available_at = parse_datetime(raw_available)
        else:
            data_issues.append(
                {
                    "category": "universe",
                    "severity": "error",
                    "security_id": "",
                    "snapshot_id": sid,
                    "event_id": "",
                    "session_date": "",
                    "message": f"unknown availability_precision={precision!r}; unknown public time cannot enter reproducible flow",
                    "context": f"snapshots.csv:{i}",
                }
            )
            continue

        snapshots.append(
            SnapshotMeta(
                snapshot_id=sid,
                holdings_as_of=parse_date(row["holdings_as_of"]),
                available_at=available_at,
                available_at_raw=raw_available,
                availability_precision=precision,
                source=row.get("source", "").strip(),
                source_hash=row.get("source_hash", "").strip(),
                retrieved_at=parse_datetime(row["retrieved_at"]),
                revision=row.get("revision", "").strip(),
            )
        )

    members: list[SnapshotMember] = []
    known_snap = {s.snapshot_id for s in snapshots}
    for i, row in enumerate(_read_csv(mem_path), start=2):
        sid = row["snapshot_id"].strip()
        if sid not in known_snap:
            data_issues.append(
                {
                    "category": "universe",
                    "severity": "error",
                    "security_id": row["security_id"].strip(),
                    "snapshot_id": sid,
                    "event_id": "",
                    "session_date": "",
                    "message": "snapshot_members references unknown snapshot_id",
                    "context": f"snapshot_members.csv:{i}",
                }
            )
            continue
        members.append(
            SnapshotMember(
                snapshot_id=sid,
                security_id=row["security_id"].strip(),
                original_name=row.get("original_name", "").strip(),
                weight=_opt_float(row.get("weight")),
            )
        )

    prices: list[PriceRow] = []
    price_keys: set[tuple[str, date]] = set()
    for i, row in enumerate(_read_csv(px_path), start=2):
        sid = row["security_id"].strip()
        sd = parse_date(row["session_date"])
        key = (sid, sd)
        if key in price_keys:
            data_issues.append(
                {
                    "category": "prices",
                    "severity": "error",
                    "security_id": sid,
                    "snapshot_id": "",
                    "event_id": "",
                    "session_date": sd.isoformat(),
                    "message": "duplicate price key",
                    "context": f"prices.csv:{i}",
                }
            )
            continue
        price_keys.add(key)
        try:
            open_value = float(row["open"])
            close_value = float(row["close"])
            if not math.isfinite(open_value) or not math.isfinite(close_value):
                raise ValueError("non-finite open/close")
            if open_value <= 0 or close_value <= 0:
                raise ValueError("nonpositive open/close")
            volume = _opt_float(row.get("volume"))
            turnover = _opt_float(row.get("turnover"))
        except (TypeError, ValueError) as exc:
            data_issues.append(
                {
                    "category": "prices", "severity": "error", "security_id": sid,
                    "snapshot_id": "", "event_id": "", "session_date": sd.isoformat(),
                    "message": f"invalid numeric price: {exc}", "context": f"prices.csv:{i}",
                }
            )
            continue
        prices.append(
            PriceRow(
                security_id=sid,
                session_date=sd,
                open=open_value,
                close=close_value,
                volume=volume,
                turnover=turnover,
                currency=row.get("currency", "USD").strip() or "USD",
            )
        )

    events: list[EventRow] = []
    event_ids: set[str] = set()
    dividend_keys: set[tuple[str, date, float, date]] = set()
    for i, row in enumerate(_read_csv(ev_path), start=2):
        eid = row["event_id"].strip()
        if eid in event_ids:
            data_issues.append(
                {
                    "category": "events",
                    "severity": "error",
                    "security_id": row["security_id"].strip(),
                    "snapshot_id": "",
                    "event_id": eid,
                    "session_date": "",
                    "message": "duplicate event_id",
                    "context": f"events.csv:{i}",
                }
            )
            continue
        event_ids.add(eid)
        etype = row["event_type"].strip()
        sid = row["security_id"].strip()
        effective = parse_date(row["effective_session"])
        try:
            pay_date = _opt_date(row.get("pay_date"))
            shares_new = _opt_float(row.get("shares_new_per_old"))
            cash_amount = _opt_float(row.get("cash_amount_per_share"))
            spinoff_value = _opt_float(row.get("spinoff_value_per_old_share"))
            if spinoff_value is not None and spinoff_value < 0:
                raise ValueError("spinoff value must be nonnegative")
            if etype in {"split", "reverse_split"} and (shares_new is None or shares_new <= 0):
                raise ValueError("split/reverse_split requires positive finite shares_new_per_old")
            if etype == "cash_dividend" and (cash_amount is None or cash_amount <= 0):
                raise ValueError("cash_dividend requires positive finite cash_amount_per_share")
        except (TypeError, ValueError) as exc:
            data_issues.append(
                {
                    "category": "events", "severity": "error", "security_id": sid,
                    "snapshot_id": "", "event_id": eid, "session_date": effective.isoformat(),
                    "message": f"invalid numeric event: {exc}", "context": f"events.csv:{i}",
                }
            )
            continue
        if etype == "cash_dividend" and pay_date is None and not score_only:
            data_issues.append(
                {
                    "category": "events", "severity": "error", "security_id": sid,
                    "snapshot_id": "", "event_id": eid, "session_date": effective.isoformat(),
                    "message": "cash_dividend missing pay_date", "context": f"events.csv:{i}",
                }
            )
            continue
        if etype == "cash_dividend":
            dividend_key = (sid, effective, float(cash_amount), pay_date)
            if dividend_key in dividend_keys:
                data_issues.append(
                    {
                        "category": "events", "severity": "error", "security_id": sid,
                        "snapshot_id": "", "event_id": eid, "session_date": effective.isoformat(),
                        "message": "duplicate cash dividend semantic key", "context": f"events.csv:{i}",
                    }
                )
                continue
            dividend_keys.add(dividend_key)
        events.append(
            EventRow(
                event_id=eid,
                security_id=sid,
                event_type=etype,
                effective_session=effective,
                shares_new_per_old=shares_new,
                cash_amount_per_share=cash_amount,
                cash_basis=(row.get("cash_basis") or "per_share_at_entitlement").strip(),
                pay_date=pay_date,
                source=row.get("source", "").strip(),
                spinoff_value_per_old_share=spinoff_value,
                spinoff_valuation=(row.get("spinoff_valuation") or "").strip(),
            )
        )

    return Inputs(
        root=input_dir,
        config=config,
        calendar=calendar,
        securities=securities,
        snapshots=snapshots,
        members=members,
        prices=prices,
        events=events,
        file_hashes=file_hashes,
        data_issues=data_issues,
    )


def manifest_inputs_hash(file_hashes: dict[str, str]) -> str:
    payload = json.dumps(file_hashes, sort_keys=True, separators=(",", ":"))
    return sha256_text(payload)
