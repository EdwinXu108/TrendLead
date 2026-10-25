"""Deterministic synthetic fixture generator for LeadTrend v0.5 stage 1.

Seed and rules are documented in data/v05/fixtures/GENERATION.md.
Regenerate with: python -m leadtrend_v05 generate-fixtures --output data/v05/fixtures
"""

from __future__ import annotations

import csv
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SEED = 42
NY_OFFSET_WINTER = timezone(timedelta(hours=-5))  # 2025 Jan-Feb is EST
NY_OFFSET_SUMMER = timezone(timedelta(hours=-4))  # 2025 Mar-Nov is EDT


def _ny_offset(d: date) -> timezone:
    # US DST 2025: starts Mar 9, ends Nov 2
    if date(2025, 3, 9) <= d <= date(2025, 11, 2):
        return NY_OFFSET_SUMMER
    return NY_OFFSET_WINTER


def _session_dt(d: date, hour: int, minute: int) -> datetime:
    local = datetime(d.year, d.month, d.day, hour, minute, tzinfo=_ny_offset(d))
    return local.astimezone(timezone.utc)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def build_sessions() -> list[dict]:
    """~100 sessions: 2025-01-02 .. 2025-05-30, minus holidays, with one Friday holiday and one early close."""
    holidays = {
        date(2025, 1, 20),  # Monday holiday
        date(2025, 2, 17),  # Monday holiday
        date(2025, 4, 18),  # Friday holiday (Good Friday) — tests week-end on Thursday
        date(2025, 5, 26),  # Monday holiday
    }
    early_close = {
        date(2025, 4, 17): "early_close_day_before_friday_holiday",
        date(2025, 5, 23): "early_close_half_day",
    }
    rows: list[dict] = []
    d = date(2025, 1, 2)
    end = date(2025, 5, 30)
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            if d in early_close:
                open_t = _session_dt(d, 9, 30)
                close_t = _session_dt(d, 13, 0)
                is_early = "1"
                note = early_close[d]
            else:
                open_t = _session_dt(d, 9, 30)
                close_t = _session_dt(d, 16, 0)
                is_early = "0"
                note = ""
            rows.append(
                {
                    "session_date": d.isoformat(),
                    "open_time_utc": open_t.isoformat().replace("+00:00", "Z"),
                    "close_time_utc": close_t.isoformat().replace("+00:00", "Z"),
                    "is_early_close": is_early,
                    "note": note,
                }
            )
        d += timedelta(days=1)
    return rows


def mulberry32(seed: int):
    """Small deterministic PRNG (not Python's hash-randomized random)."""
    state = seed & 0xFFFFFFFF

    def rand() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = state
        t = (t ^ (t >> 15)) * (t | 1) & 0xFFFFFFFF
        t ^= t + ((t ^ (t >> 7)) * (t | 61) & 0xFFFFFFFF)
        t &= 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0

    return rand


def gauss(rand) -> float:
    # Box-Muller
    u1 = max(rand(), 1e-12)
    u2 = rand()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


INDUSTRY_SPECS = {
    "TECH": 5,
    "FIN": 5,
    "HLTH": 5,
    "ENRG": 5,
    "CONS": 5,
    "INDU": 5,
    "UTIL": 5,
    "MATR": 5,
}


def build_securities() -> list[dict]:
    rows: list[dict] = []
    # 8 industries x 5 = 40 core eligible names
    for ind, n in INDUSTRY_SPECS.items():
        for i in range(1, n + 1):
            sid = f"SYN_{ind[:1]}{i:02d}"
            rows.append(
                {
                    "security_id": sid,
                    "issuer_id": f"ISS_{ind[:1]}{i:02d}",
                    "ticker": f"SY{ind[:1]}{i:02d}",
                    "ticker_valid_from": "2025-01-02",
                    "ticker_valid_to": "",
                    "security_type": "common_stock",
                    "exchange": "NASDAQ" if i % 2 == 0 else "NYSE",
                    "industry": ind,
                    "industry_source_version": "synthetic_v1",
                    "currency": "USD",
                }
            )
    # dual-class issuer (two share classes, one issuer)
    rows.append(
        {
            "security_id": "SYN_DUAL_A",
            "issuer_id": "ISS_DUAL",
            "ticker": "SYDA",
            "ticker_valid_from": "2025-01-02",
            "ticker_valid_to": "",
            "security_type": "common_stock",
            "exchange": "NYSE",
            "industry": "TECH",
            "industry_source_version": "synthetic_v1",
            "currency": "USD",
        }
    )
    rows.append(
        {
            "security_id": "SYN_DUAL_B",
            "issuer_id": "ISS_DUAL",
            "ticker": "SYDB",
            "ticker_valid_from": "2025-01-02",
            "ticker_valid_to": "",
            "security_type": "common_stock",
            "exchange": "NASDAQ",
            "industry": "TECH",
            "industry_source_version": "synthetic_v1",
            "currency": "USD",
        }
    )
    # allowed: equity preferred ADR + BRK.B-like
    rows.append(
        {
            "security_id": "SYN_PREF_ADR",
            "issuer_id": "ISS_PREF",
            "ticker": "SYPFA",
            "ticker_valid_from": "2025-01-02",
            "ticker_valid_to": "",
            "security_type": "adr_preferred_equity",
            "exchange": "NYSE",
            "industry": "FIN",
            "industry_source_version": "synthetic_v1",
            "currency": "USD",
        }
    )
    rows.append(
        {
            "security_id": "SYN_BRK_B",
            "issuer_id": "ISS_BRK",
            "ticker": "BRK.B",
            "ticker_valid_from": "2025-01-02",
            "ticker_valid_to": "",
            "security_type": "common_stock",
            "exchange": "NYSE",
            "industry": "FIN",
            "industry_source_version": "synthetic_v1",
            "currency": "USD",
        }
    )
    # excluded examples
    rows.append(
        {
            "security_id": "SYN_BRK_A",
            "issuer_id": "ISS_BRK",
            "ticker": "BRK.A",
            "ticker_valid_from": "2025-01-02",
            "ticker_valid_to": "",
            "security_type": "common_stock",
            "exchange": "NYSE",
            "industry": "FIN",
            "industry_source_version": "synthetic_v1",
            "currency": "USD",
        }
    )
    rows.append(
        {
            "security_id": "SYN_REIT1",
            "issuer_id": "ISS_REIT",
            "ticker": "SYRT",
            "ticker_valid_from": "2025-01-02",
            "ticker_valid_to": "",
            "security_type": "reit",
            "exchange": "NYSE",
            "industry": "FIN",
            "industry_source_version": "synthetic_v1",
            "currency": "USD",
        }
    )
    rows.append(
        {
            "security_id": "SYN_SPAC1",
            "issuer_id": "ISS_SPAC",
            "ticker": "SYSC",
            "ticker_valid_from": "2025-01-02",
            "ticker_valid_to": "",
            "security_type": "spac",
            "exchange": "NASDAQ",
            "industry": "TECH",
            "industry_source_version": "synthetic_v1",
            "currency": "USD",
        }
    )
    rows.append(
        {
            "security_id": "SYN_LP1",
            "issuer_id": "ISS_LP",
            "ticker": "SYLP",
            "ticker_valid_from": "2025-01-02",
            "ticker_valid_to": "",
            "security_type": "partnership_unit",
            "exchange": "NYSE",
            "industry": "ENRG",
            "industry_source_version": "synthetic_v1",
            "currency": "USD",
        }
    )
    # benchmark ETF (never a strategy buy)
    rows.append(
        {
            "security_id": "SYN_VOO",
            "issuer_id": "ISS_VOO",
            "ticker": "VOO",
            "ticker_valid_from": "2025-01-02",
            "ticker_valid_to": "",
            "security_type": "etf",
            "exchange": "NYSE",
            "industry": "BENCH",
            "industry_source_version": "synthetic_v1",
            "currency": "USD",
        }
    )
    # late member (appears in later snapshot)
    rows.append(
        {
            "security_id": "SYN_NEW1",
            "issuer_id": "ISS_NEW1",
            "ticker": "SYNW",
            "ticker_valid_from": "2025-03-01",
            "ticker_valid_to": "",
            "security_type": "common_stock",
            "exchange": "NASDAQ",
            "industry": "TECH",
            "industry_source_version": "synthetic_v1",
            "currency": "USD",
        }
    )
    return rows


def build_prices(session_dates: list[date], securities: list[dict]) -> list[dict]:
    rand = mulberry32(SEED)
    rows: list[dict] = []
    # distinct drifts so rankings move over time
    for si, sec in enumerate(securities):
        sid = sec["security_id"]
        base = 80.0 + (si % 17) * 3.5
        if sid == "SYN_VOO":
            base = 450.0
            drift = 0.0004
            vol = 0.006
        elif sid == "SYN_BRK_A":
            base = 700_000.0
            drift = 0.0003
            vol = 0.005
        elif sid.startswith("SYN_DUAL"):
            base = 55.0 if sid.endswith("A") else 48.0
            drift = 0.0005
            vol = 0.010
        else:
            # make some names strong mid-sample and others weak so buffer matters
            drift = 0.0002 + ((si * 7) % 11) * 0.00008
            vol = 0.008 + ((si * 3) % 5) * 0.001
            # SYN_T01.. have stronger trends in the middle weeks
            if sid in {"SYN_T01", "SYN_T02", "SYN_T03", "SYN_F01", "SYN_F02", "SYN_H01", "SYN_H02", "SYN_E01"}:
                drift = 0.0012
            if sid in {"SYN_U01", "SYN_U02", "SYN_M01", "SYN_M02"}:
                drift = -0.0004

        price = base
        for d in session_dates:
            r = drift + vol * gauss(rand)
            price = max(price * math.exp(r), 0.5)
            # turnover proxy for class selection
            vol_shares = 1_000_000 + (si % 9) * 50_000 + int(abs(gauss(rand)) * 20_000)
            if sid == "SYN_DUAL_A":
                vol_shares = 2_500_000  # higher liquidity class
            if sid == "SYN_DUAL_B":
                vol_shares = 300_000
            rows.append(
                {
                    "security_id": sid,
                    "session_date": d.isoformat(),
                    "open": f"{price:.4f}",
                    "close": f"{price:.4f}",  # placeholder, fixed below with split/dip
                    "volume": str(vol_shares),
                    "turnover": f"{vol_shares * price:.2f}",
                    "currency": "USD",
                }
            )
    # Post-process: apply split on SYN_T01 (1->2) on 2025-03-20: prices after that date /2
    # and dividend dips on SYN_F01 ex-date.
    split_day = date(2025, 3, 20)
    div_ex_f = date(2025, 4, 10)
    div_ex_h = date(2025, 5, 8)
    for row in rows:
        sd = date.fromisoformat(row["session_date"])
        if row["security_id"] == "SYN_T01" and sd >= split_day:
            row["open"] = f"{float(row['open']) / 2:.4f}"
            row["close"] = f"{float(row['close']) / 2:.4f}"
        if row["security_id"] == "SYN_F01" and sd == div_ex_f:
            # mechanical ex-date drop of about the dividend
            row["open"] = f"{float(row['open']) - 0.50:.4f}"
            row["close"] = f"{float(row['close']) - 0.50:.4f}"
        if row["security_id"] == "SYN_E01" and sd == date(2025, 3, 28):
            row["open"] = f"{float(row['open']) - 0.40:.4f}"
            row["close"] = f"{float(row['close']) - 0.40:.4f}"
        if row["security_id"] == "SYN_H01" and sd == div_ex_h:
            row["open"] = f"{float(row['open']) - 0.30:.4f}"
            row["close"] = f"{float(row['close']) - 0.30:.4f}"
    return rows


def build_events() -> list[dict]:
    return [
        {
            "event_id": "EV_SPLIT_T01",
            "security_id": "SYN_T01",
            "event_type": "split",
            "effective_session": "2025-03-20",
            "shares_new_per_old": "2",
            "cash_amount_per_share": "",
            "cash_basis": "",
            "pay_date": "",
            "source": "synthetic",
        },
        {
            "event_id": "EV_DIV_F01",
            "security_id": "SYN_F01",
            "event_type": "cash_dividend",
            "effective_session": "2025-04-10",
            "shares_new_per_old": "",
            "cash_amount_per_share": "0.50",
            "cash_basis": "per_share_at_entitlement",
            "pay_date": "2025-04-25",
            "source": "synthetic",
        },
        {
            "event_id": "EV_DIV_H01",
            "security_id": "SYN_H01",
            "event_type": "cash_dividend",
            "effective_session": "2025-05-08",
            "shares_new_per_old": "",
            "cash_amount_per_share": "0.30",
            "cash_basis": "per_share_at_entitlement",
            "pay_date": "2025-05-22",
            "source": "synthetic",
        },
        # Dividend on a name that the demo strategy actually holds around the ex-date,
        # so stage1_demo shows receivable_cash and a pay-date cash increase.
        {
            "event_id": "EV_DIV_E01",
            "security_id": "SYN_E01",
            "event_type": "cash_dividend",
            "effective_session": "2025-03-28",
            "shares_new_per_old": "",
            "cash_amount_per_share": "0.40",
            "cash_basis": "per_share_at_entitlement",
            "pay_date": "2025-04-11",
            "source": "synthetic",
        },
        # VOO benchmark dividend (score/benchmark only)
        {
            "event_id": "EV_DIV_VOO",
            "security_id": "SYN_VOO",
            "event_type": "cash_dividend",
            "effective_session": "2025-03-28",
            "shares_new_per_old": "",
            "cash_amount_per_share": "1.20",
            "cash_basis": "per_share_at_entitlement",
            "pay_date": "2025-04-04",
            "source": "synthetic",
        },
    ]


def build_snapshots(session_dates: list[date]) -> tuple[list[dict], list[dict]]:
    """Multiple snapshots with explicit public times, including a future snapshot and a revision."""
    core = []
    for ind in INDUSTRY_SPECS:
        for i in range(1, 6):
            core.append(f"SYN_{ind[:1]}{i:02d}")
    # exclusions present in raw holdings to prove they are filtered
    excluded = ["SYN_BRK_A", "SYN_REIT1", "SYN_SPAC1", "SYN_LP1"]

    snap_meta = [
        {
            "snapshot_id": "SNAP_2025_01",
            "holdings_as_of": "2025-01-31",
            "available_at": "2025-02-12",
            "availability_precision": "date",
            "source": "synthetic_voo_holdings_v1",
            "source_hash": "synthetic_v1",
            "retrieved_at": "2025-02-13T00:00:00Z",
            "revision": "original",
        },
        {
            "snapshot_id": "SNAP_2025_01_REV",
            "holdings_as_of": "2025-01-31",
            "available_at": "2025-03-05",
            "availability_precision": "date",
            "source": "synthetic_voo_holdings_v1r",
            "source_hash": "synthetic_v1r",
            "retrieved_at": "2025-03-06T00:00:00Z",
            "revision": "revision_drop_u02",
        },
        {
            "snapshot_id": "SNAP_2025_03",
            "holdings_as_of": "2025-03-31",
            "available_at": "2025-04-11",
            "availability_precision": "date",
            "source": "synthetic_voo_holdings_v2",
            "source_hash": "synthetic_v2",
            "retrieved_at": "2025-04-12T00:00:00Z",
            "revision": "original",
        },
        {
            "snapshot_id": "SNAP_FUTURE",
            "holdings_as_of": "2025-05-30",
            "available_at": "2025-07-15",
            "availability_precision": "date",
            "source": "synthetic_voo_holdings_future",
            "source_hash": "synthetic_future",
            "retrieved_at": "2025-07-16T00:00:00Z",
            "revision": "must_not_leak_into_early_signals",
        },
    ]

    members: list[dict] = []

    def add_members(snap_id: str, ids: list[str]) -> None:
        for sid in sorted(ids):
            members.append(
                {
                    "snapshot_id": snap_id,
                    "security_id": sid,
                    "original_name": sid,
                    "weight": "",
                }
            )

    add_members("SNAP_2025_01", core + ["SYN_DUAL_A", "SYN_DUAL_B", "SYN_PREF_ADR", "SYN_BRK_B"] + excluded)
    # revision: drop SYN_U02 (member removal via revision of same holdings_as_of)
    add_members(
        "SNAP_2025_01_REV",
        [x for x in core if x != "SYN_U02"] + ["SYN_DUAL_A", "SYN_DUAL_B", "SYN_PREF_ADR", "SYN_BRK_B"] + excluded,
    )
    # March snapshot: drop some, add SYN_NEW1
    march_core = [x for x in core if x not in {"SYN_M03", "SYN_U02"}]
    add_members(
        "SNAP_2025_03",
        march_core + ["SYN_NEW1", "SYN_DUAL_A", "SYN_DUAL_B", "SYN_PREF_ADR", "SYN_BRK_B"] + excluded,
    )
    add_members("SNAP_FUTURE", march_core + ["SYN_NEW1", "SYN_DUAL_A"])
    return snap_meta, members


def generate(output_dir: Path) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    session_rows = build_sessions()
    session_dates = [date.fromisoformat(r["session_date"]) for r in session_rows]
    securities = build_securities()
    prices = build_prices(session_dates, securities)
    events = build_events()
    snap_meta, members = build_snapshots(session_dates)

    write_csv(
        output_dir / "calendar.csv",
        ["session_date", "open_time_utc", "close_time_utc", "is_early_close", "note"],
        session_rows,
    )
    write_csv(
        output_dir / "securities.csv",
        [
            "security_id",
            "issuer_id",
            "ticker",
            "ticker_valid_from",
            "ticker_valid_to",
            "security_type",
            "exchange",
            "industry",
            "industry_source_version",
            "currency",
        ],
        securities,
    )
    write_csv(
        output_dir / "prices.csv",
        ["security_id", "session_date", "open", "close", "volume", "turnover", "currency"],
        prices,
    )
    write_csv(
        output_dir / "events.csv",
        [
            "event_id",
            "security_id",
            "event_type",
            "effective_session",
            "shares_new_per_old",
            "cash_amount_per_share",
            "cash_basis",
            "pay_date",
            "source",
        ],
        events,
    )
    write_csv(
        output_dir / "snapshots.csv",
        [
            "snapshot_id",
            "holdings_as_of",
            "available_at",
            "availability_precision",
            "source",
            "source_hash",
            "retrieved_at",
            "revision",
        ],
        snap_meta,
    )
    write_csv(
        output_dir / "snapshot_members.csv",
        ["snapshot_id", "security_id", "original_name", "weight"],
        members,
    )

    readme = output_dir / "GENERATION.md"
    readme.write_text(
        f"""# Synthetic fixtures for LeadTrend v0.5 stage 1

- Generator: `leadtrend_v05/fixtures.py` (`python -m leadtrend_v05 generate-fixtures`)
- Seed: `{SEED}` (Mulberry32; not Python `random`)
- dataset_kind: synthetic (NOT real VOO holdings or prices)
- Sessions: {len(session_rows)} explicit NYSE-like sessions 2025-01-02..2025-05-30
  - Holidays: 2025-01-20, 2025-02-17, **2025-04-18 (Friday holiday)**, 2025-05-26
  - Early closes: 2025-04-17, 2025-05-23 (13:00 America/New_York)
- Securities: {len(securities)} ids
  - 8 industries x 5 common stocks (eligible pool >= 40 so len(F) can exceed 20)
  - Dual class: SYN_DUAL_A / SYN_DUAL_B (same issuer ISS_DUAL)
  - Allowed specials: SYN_PREF_ADR (equity preferred ADR), SYN_BRK_B
  - Excluded examples: SYN_BRK_A (ticker), SYN_REIT1, SYN_SPAC1, SYN_LP1
  - Benchmark ETF: SYN_VOO (never a strategy trade)
  - Late member: SYN_NEW1
- Snapshots (explicit available_at, date precision = open of next session after that date):
  - SNAP_2025_01: as_of 2025-01-31, available after 2025-02-12
  - SNAP_2025_01_REV: same as_of, later revision available after 2025-03-05 (drops SYN_U02)
  - SNAP_2025_03: as_of 2025-03-31, available after 2025-04-11 (adds SYN_NEW1, drops SYN_M03)
  - SNAP_FUTURE: as_of 2025-05-30, available after 2025-07-15 — must NOT affect earlier signals
- Events:
  - EV_SPLIT_T01: 1->2 split on 2025-03-20
  - EV_DIV_F01: $0.50/sh ex 2025-04-10, pay 2025-04-25
  - EV_DIV_H01: $0.30/sh ex 2025-05-08, pay 2025-05-22
  - EV_DIV_E01: $0.40/sh ex 2025-03-28, pay 2025-04-11 (held name in demo)
  - EV_DIV_VOO: benchmark dividend only
- Prices: GBM-like paths with per-name drift/vol; split halfing after 2025-03-20 on SYN_T01;
  mechanical ex-date drops on dividend names. All values are synthetic.
""",
        encoding="utf-8",
    )
    print(f"fixtures written to {output_dir} ({len(session_rows)} sessions, {len(securities)} securities)")
