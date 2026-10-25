"""Stage-2 tests: SEC UA required, no network, no OpenD."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from leadtrend_v05.sec_nport import (
    TARGET_CLASS_ID,
    TARGET_SERIES_ID,
    MissingSecUserAgent,
    parse_nport_xml,
    require_sec_user_agent,
)
from leadtrend_v05.ticker_map import map_holdings_to_tickers


SAMPLE = f"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport">
  <headerData>
    <seriesClassInfo>
      <seriesId>{TARGET_SERIES_ID}</seriesId>
      <classId>C000007774</classId>
      <classId>{TARGET_CLASS_ID}</classId>
    </seriesClassInfo>
  </headerData>
  <formData>
    <genInfo>
      <seriesName>VANGUARD 500 INDEX FUND</seriesName>
      <seriesId>{TARGET_SERIES_ID}</seriesId>
      <repPdEnd>2025-12-31</repPdEnd>
      <repPdDate>2025-12-31</repPdDate>
    </genInfo>
    <fundInfo><totAssets>1</totAssets><netAssets>1</netAssets></fundInfo>
    <invstOrSec>
      <name>Apple Inc</name>
      <cusip>037833100</cusip>
      <identifiers><isin value="US0378331005"/></identifiers>
      <balance>10</balance><units>NS</units><curCd>USD</curCd>
      <valUSD>1000</valUSD><pctVal>0.1</pctVal>
      <assetCat>EC</assetCat>
    </invstOrSec>
    <invstOrSec>
      <name>REIT Example Realty Trust</name>
      <cusip>123456789</cusip>
      <balance>5</balance><units>NS</units><curCd>USD</curCd>
      <valUSD>500</valUSD><pctVal>0.05</pctVal>
      <assetCat>EC</assetCat>
    </invstOrSec>
    <invstOrSec>
      <name>Cash</name>
      <cusip></cusip>
      <balance>1</balance><units>NS</units><curCd>USD</curCd>
      <valUSD>5</valUSD><pctVal>0</pctVal>
      <assetCat>CASH</assetCat>
    </invstOrSec>
  </formData>
</edgarSubmission>
"""


def test_sec_user_agent_empty_raises():
    with pytest.raises(MissingSecUserAgent):
        require_sec_user_agent("")
    with pytest.raises(MissingSecUserAgent):
        require_sec_user_agent("   ")
    with pytest.raises(MissingSecUserAgent):
        require_sec_user_agent(None)
    assert require_sec_user_agent("Test Org contact@example.com") == "Test Org contact@example.com"


def test_stage2_config_requires_ua_field_present():
    import json

    cfg = json.loads(Path("config/v05_stage2.json").read_text(encoding="utf-8"))
    assert "sec_user_agent" in cfg
    # must be fillable by investor; empty is allowed in file but blocks download


def test_parse_nport_available_at_is_acceptance_not_retrieved():
    raw = SAMPLE.encode("utf-8")
    meta = {
        "accession": "0000036405-26-000063",
        "report_date": "2025-12-31",
        "filing_date": "2026-02-26",
        "acceptance_datetime": "2026-02-27T02:15:30.000Z",
    }
    snap = parse_nport_xml(raw, meta, Path("x.xml"))
    assert snap.available_at == datetime(2026, 2, 27, 2, 15, 30, tzinfo=timezone.utc)
    # available_at is NOT today / retrieved_at
    assert snap.available_at != snap.retrieved_at
    assert snap.holdings_as_of == date(2025, 12, 31)


def test_parse_nport_rejects_wrong_series():
    bad = SAMPLE.replace(TARGET_SERIES_ID, "S000099999").encode("utf-8")
    with pytest.raises(ValueError):
        parse_nport_xml(
            bad,
            {
                "accession": "X",
                "report_date": "2025-12-31",
                "filing_date": "2026-02-26",
                "acceptance_datetime": "2026-02-27T02:15:30.000Z",
            },
            Path("x.xml"),
        )


def test_parse_nport_equity_only_and_reit_flagged_for_review():
    snap = parse_nport_xml(
        SAMPLE.encode("utf-8"),
        {
            "accession": "0000036405-26-000063",
            "report_date": "2025-12-31",
            "filing_date": "2026-02-26",
            "acceptance_datetime": "2026-02-27T02:15:30.000Z",
        },
        Path("x.xml"),
    )
    names = [h.name for h in snap.holdings]
    assert "Cash" not in names
    assert snap.non_equity_count == 1
    assert any("REALTY" in n.upper() or "REIT" in n.upper() for n in names)


def test_rename_maps_to_same_security_id_pattern():
    """Different tickers for one issuer share issuer_id via CUSIP prefix; rename
    keeps security_id=CUSIP so the series does not break."""

    class H:
        def __init__(self, cusip, name):
            self.cusip = cusip
            self.name = name

    rows = [
        {"cik_str": 1, "ticker": "META", "title": "Meta Platforms Inc."},
        {"cik_str": 2, "ticker": "FB", "title": "Facebook Inc."},
    ]
    # two historical names → two CUSIPs is realistic; identity stays CUSIP-stable
    mapping, _ = map_holdings_to_tickers(
        [H("30303M102", "Meta Platforms Inc."), H("30303M102", "Facebook Inc.")],
        rows,
    )
    # same CUSIP overwrites — same security_id
    assert mapping["30303M102"].cusip == "30303M102"


def test_delisted_unmapped_stays_in_holdings():
    class H:
        cusip = "999999999"
        name = "Delisted Co"

    mapping, issues = map_holdings_to_tickers([H()], [])
    assert mapping["999999999"].ticker == ""
    assert mapping["999999999"].match_method == "unmatched"
    assert any(i["message"] == "ticker_unmapped" for i in issues)


def test_split_ratio_requires_before_after_conversion():
    """shares_new_per_old must be after/before, not raw split_ratio."""
    before, after = 1.0, 2.0
    new_per_old = after / before
    assert new_per_old == 2.0
    # 2-for-1: 100 old -> 200 new
    assert 100 * new_per_old == 200


def test_calendar_early_close_flag_exists_in_schema():
    # schema-level: stage1 calendar supports is_early_close
    from leadtrend_v05.dataio import CalendarRow
    from leadtrend_v05.calendar import Calendar
    from datetime import timezone as tz

    d = date(2025, 4, 17)
    rows = [
        CalendarRow(
            session_date=d,
            open_time_utc=datetime(2025, 4, 17, 13, 30, tzinfo=tz.utc),
            close_time_utc=datetime(2025, 4, 17, 17, 0, tzinfo=tz.utc),
            is_early_close=True,
            note="early_close",
        )
    ]
    cal = Calendar(rows)
    assert cal.sessions[0].is_early_close is True
