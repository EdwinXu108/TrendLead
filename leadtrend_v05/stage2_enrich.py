"""Stage-2 score-only corporate actions, source diagnostics and repair evidence.

No ledger, portfolio return, order, parameter search or universe expansion.
"""
from __future__ import annotations
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from .opend_prices import OpenDPriceFetcher, finite_float
from .config import DEFAULTS
from . import paths


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def reconcile_cash(amounts, rehab):
    """Compare cash endpoint components with the event's backward cash term.

    The rehab coefficients are per event, not differences of cumulative B's.
    Never choose whichever interpretation gives a nicer return.
    """
    expected = finite_float(rehab.get("backward_adj_factorB"))
    if expected is None or expected <= 0:
        return "unresolved", None, []
    tolerance = 0.00002  # provider factor rounding; USD/share, not a return threshold
    if abs(sum(amounts) - expected) <= tolerance:
        return "sum", expected, list(range(len(amounts)))
    hits = [i for i, value in enumerate(amounts) if abs(value - expected) <= tolerance]
    if len(hits) == 1:
        return "one", expected, hits
    return "unresolved", expected, []


def classify_jump(raw_change, front_change):
    if front_change is None or not math.isfinite(front_change): return "unresolved"
    if abs(raw_change) < .4: return "not_a_jump"
    if abs(front_change) >= .4: return "real_price_move_qfq_confirmed"
    if abs(front_change) <= .1: return "missing_corporate_action"
    return "unresolved"


# Ratios are per old parent share. The sources state the actual distribution;
# a price adjustment factor never becomes an invented child-share ratio.
CHILDREN = {
 ("T", "2022-04-11"): [("WBD", .241917, "https://investors.att.com/stockholder-services/cost-basis-guide/worksheet/atandt-inc-wbd")],
 ("IBM", "2021-11-04"): [("KD", .2, "https://www.ibm.com/investor/services/faqs-about-the-kyndryl-holdings-inc-distribution")],
 ("MRK", "2021-06-03"): [("OGN", .1, "https://www.merck.com/news/merck-declares-record-date-and-dividend-for-the-organon-co-spinoff/")],
 ("EXC", "2022-02-02"): [("CEG", 1/3, "https://www.exeloncorp.com/newsroom/exelon-completes-separation-of-constellation")],
 ("BDX", "2022-04-01"): [("EMBC", .2, "https://www.sec.gov/Archives/edgar/data/10795/000001079522000040/bdx-20220331.htm")],
 ("BWA", "2023-07-05"): [("PHIN", .2, "https://www.sec.gov/Archives/edgar/data/908255/000110465924034928/tm2332902d2_ars.pdf")],
 ("BBWI", "2021-08-03"): [("VSCO", 1/3, "https://investors.bbwinc.com/news-releases/news-release-details/bath-body-works-inc-completes-separation-victorias-secret")],
 ("DHR", "2023-10-02"): [("VLTO", 1/3, "https://investors.danaher.com/2023-09-30-Danaher-Corporation-Completes-Separation-of-Veralto-Corporation")],
 ("GE", "2023-01-04"): [("GEHC", 1/3, "https://www.ge.com/news/press-releases/ge-board-of-directors-approves-separation-of-ge-healthcare")],
 ("GE", "2024-04-02"): [("GEV", .25, "https://www.ge.com/news/reports/ge-board-of-directors-approves-spin-off-of-ge-vernova-ge-vernova-and-ge-aerospace-to-launch")],
 ("DD", "2019-06-03"): [("CTVA", 1/3, "https://www.sec.gov/Archives/edgar/data/1755672/000119312519163314/d753864d8k.htm")],
 ("DD", "2025-11-03"): [("Q", .5, "https://www.investors.dupont.com/news-and-media/press-release-details/2025/DuPont-Completes-Separation-of-Qnity-Electronics/default.aspx")],
 ("RTX", "2020-04-03"): [("CARR", 1, "https://www.sec.gov/Archives/edgar/data/101829/000114036120008397/nc10010681x2_8k.htm"),
                             ("OTIS", .5, "https://www.sec.gov/Archives/edgar/data/101829/000114036120008397/nc10010681x2_8k.htm")],
 ("HON", "2026-06-29"): [("HONA", .5, "https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2026-399")],
}


def run_enrich(root: Path):
    root = Path(root); work = root / paths.PREP; norm = root / paths.NORMALIZED; raw = root / paths.RAW
    cfg_path = root / "config/v05_stage2.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    private_value = cfg["sec_user_agent"]
    base = work / "snapshots" / "stage2_revision_before_claude"
    base.mkdir(parents=True, exist_ok=True)
    for name in ("events.csv", "securities.csv"):
        if not (base / name).exists(): (base / name).write_bytes((norm / name).read_bytes())
    events = read_csv(base / "events.csv"); securities = read_csv(base / "securities.csv")
    sec_by = {r["security_id"]: r for r in securities}
    prices_by_sid = defaultdict(dict); prices_by_ticker = defaultdict(dict)
    for row in read_csv(norm / "prices.csv"):
        sid, day = row["security_id"], row["session_date"]
        prices_by_sid[sid][day] = float(row["close"])
        prices_by_ticker[sec_by[sid]["ticker"]][day] = float(row["close"])
    last_session = max(prices_by_sid["VOO_ETF"])
    start, end = cfg["price_start"], cfg["price_end"]
    fetcher = OpenDPriceFetcher(raw / "opend_kline_none_v1")
    qfq = OpenDPriceFetcher(raw / "opend_kline_qfq_v1", autype="QFQ")
    report = {"stage": 2, "score_only": True, "no_real_returns_viewed": True}
    rehab_cache = {}
    def rehab(ticker):
        if ticker not in rehab_cache:
            p = raw / "opend_corporate_actions" / f"rehab_US_{ticker.replace('.', '_')}.csv"
            rehab_cache[ticker] = {r["ex_div_date"]: r for r in read_csv(p)} if p.exists() else {}
        return rehab_cache[ticker]
    try:
        report["quota_before"] = fetcher.query_kline_quota()
        valuation_rows = []; aux_rows = []
        for ev in events:
            if ev["event_type"] != "spinoff": continue
            sid, day = ev["security_id"], ev["effective_session"]
            ticker = sec_by[sid]["ticker"]; close = prices_by_sid[sid].get(day)
            ratio = math.prod(float(e["shares_new_per_old"]) for e in events
                if e["security_id"] == sid and e["effective_session"] == day and e["event_type"] in {"split", "reverse_split"})
            match = re.search(r"price_factor=([\d.eE+-]+)", ev["source"])
            factor = float(match.group(1)) if match else None
            vendor_value = ratio * close * (factor - 1) if close and factor and factor >= 1 else None
            children = CHILDREN.get((ticker, day), [])
            child_values = []
            for child, child_ratio, evidence in children:
                value = prices_by_ticker.get(child, {}).get(day)
                source = "normalized_parent_universe_price"
                if value is None:
                    outcome = fetcher.fetch_one(child, "CHILD:" + child, start, end)
                    bars = fetcher.load_cache(child, start, end) or []
                    prices_by_ticker[child].update({b.session_date.isoformat(): b.close for b in bars})
                    value = prices_by_ticker[child].get(day)
                    p = fetcher._cache_path(child, start, end)
                    source = str(p.relative_to(root)) + ";sha256=" + hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else outcome.gap_category
                if value is not None:
                    child_values.append(value * child_ratio)
                    aux_rows.append({"event_id": ev["event_id"], "child_ticker": child, "session_date": day,
                        "shares_per_old": child_ratio, "raw_close": value, "value_per_old": value * child_ratio,
                        "source": source + ";ratio_evidence=" + evidence})
            direct = sum(child_values) if children and len(child_values) == len(children) else None
            value = direct if direct is not None else vendor_value
            method = "child_raw_close" if direct is not None else "vendor_factor" if vendor_value is not None else "unresolved"
            ev["spinoff_value_per_old_share"] = "" if value is None else f"{value:.12g}"
            ev["spinoff_valuation"] = method
            valuation_rows.append({"event_id": ev["event_id"], "security_id": sid, "ticker": ticker, "session_date": day,
                "parent_raw_close": close, "parent_shares_new_per_old": ratio, "vendor_price_factor": factor,
                "child_value_per_old": direct, "vendor_value_per_old": vendor_value,
                "value_per_old": value, "spinoff_valuation": method,
                "relative_value_difference": (direct-vendor_value)/direct if direct and vendor_value is not None else ""})
        # Reconcile the original 70 distinct cash groups, keeping quarantined rows.
        original_gaps = read_csv(base / "stage2_corporate_action_gaps.csv")
        group_reviews = []; quarantine = []; blocked = []
        removed_ids = set()
        for gap in original_gaps:
            if gap["message"] != "distinct_cash_dividends_require_source_review": continue
            sid = gap["security_id"]; ticker = sec_by[sid]["ticker"]
            day = re.search(r"ex=(\d{4}-\d{2}-\d{2})", gap["detail"]).group(1)
            group = [e for e in events if e["security_id"] == sid and e["effective_session"] == day and e["event_type"] == "cash_dividend"]
            values = [float(e["cash_amount_per_share"]) for e in group]
            status, expected, keep = reconcile_cash(values, rehab(ticker).get(day, {}))
            if status == "one":
                for i,e in enumerate(group):
                    if i not in keep: removed_ids.add(e["event_id"]); quarantine.append(e)
            if status == "unresolved":
                blocked.append({"security_id": sid, "effective_session": day, "reason": "unverified_dividend_components"})
            group_reviews.append({"security_id": sid, "ticker": ticker, "ex_date": day, "amounts": "|".join(map(str,values)),
                "rehab_cash_adjustment": expected, "result": status,
                "source": f"data/v05/raw/opend_corporate_actions/rehab_US_{ticker.replace('.', '_')}.csv"})
        events = [e for e in events if e["event_id"] not in removed_ids]
        # New instruction: cash amount may be verified by rehab for scoring,
        # without inventing a ledger payment date.
        bny_sid = next(s for s,r in sec_by.items() if r["ticker"] == "BNY")
        existing_cash = {(e["security_id"],e["effective_session"]) for e in events if e["event_type"] == "cash_dividend"}
        bny_added = []
        official_path = raw / 'bny_official/selected_cash_2019_2020.json'
        official = json.loads(official_path.read_text()) if official_path.exists() else {'records': []}
        for day, record in rehab("BNY").items():
            amount = finite_float(record.get("per_cash_div"))
            if start <= day <= last_session and amount and (bny_sid,day) not in existing_cash:
                event = {"event_id": f"SCORE_CASH_BNY_{day}", "security_id": bny_sid, "event_type": "cash_dividend",
                    "effective_session": day, "shares_new_per_old": "", "cash_amount_per_share": f"{amount:.10g}",
                    "cash_basis": "per_share_at_entitlement", "pay_date": "", "source": "opend_get_rehab:US.BNY;score_only_verified_cash;payment_date_unresolved;authorized_2026-10-25"}
                matches = [r for r in official['records'] if abs((datetime.strptime(r['ex_date'], '%m/%d/%y').date()-date.fromisoformat(day)).days) <= 1 and abs(float(r['amount'].lstrip('$'))-amount) < .00001]
                if len(matches) == 1:
                    r = matches[0]
                    event['pay_date'] = datetime.strptime(r['payable'], '%m/%d/%y').date().isoformat()
                    event['source'] = ('opend_get_rehab:US.BNY;cash_and_exdate_from_rehab;pay_date_from=' + official['source']['url']
                        + ';acquired_at=' + official['source']['acquired_at'] + ';official_reported_exdate=' + r['ex_date']
                        + ';official_exdate_discrepancy_preserved;raw_evidence=' + str(official_path.relative_to(root)))
                events.append(event); bny_added.append(event)
        # The two N/A identifiers are CVRs, not unrecognized ordinary shares.
        identity_reviews = []
        manifest = read_csv(work / "stage2_holdings_manifest.csv")
        cvr_ids = {r["security_id"] for r in manifest if re.search(r"\bCVR\b",r["title"], re.I)}
        for sid in cvr_ids:
            row = sec_by[sid]; row["security_type"] = "contingent_value_right"
            row["source_evidence"] += ";NPORT original title confirms CVR;not ordinary stock;LEI does not identify share class"
            identity_reviews.append({"security_id": sid, "decision": "exclude_contingent_value_right", "evidence": "|".join(sorted({r['title'] for r in manifest if r['security_id']==sid})), "issuer_id": row["issuer_id"]})
        write_csv(norm / "securities.csv", securities, list(securities[0]))
        fields = list(events[0]) + [k for k in ("spinoff_value_per_old_share","spinoff_valuation") if k not in events[0]]
        write_csv(norm / "events.csv", events, fields)
        write_csv(norm / "spinoff_child_prices.csv", aux_rows, ["event_id","child_ticker","session_date","shares_per_old","raw_close","value_per_old","source"])
        write_csv(work / "stage2_spinoff_valuations.csv", valuation_rows, list(valuation_rows[0]))
        write_csv(work / "stage2_dividend_component_reconciliation.csv", group_reviews, list(group_reviews[0]))
        write_csv(work / "stage2_dividend_component_quarantine.csv", quarantine, list(events[0]))
        write_csv(work / "stage2_scoring_blocks.csv", blocked, ["security_id","effective_session","reason"])
        write_csv(work / "stage2_bny_scoring_cash_added.csv", bny_added, list(bny_added[0]) if bny_added else fields)
        write_csv(work / "stage2_identity_revision_review.csv", identity_reviews, ["security_id","decision","evidence","issuer_id"])
        # Existing unresolved jump rows only; QFQ is a diagnostic, never scoring input.
        jumps = [dict(r) for r in original_gaps
                 if r["message"] == "abs_close_change_ge_40pct_without_event" and r.get("conclusion") in {"", "unresolved"}]
        jump_reviews = []
        for r in jumps:
            sid,day = r["security_id"], r["effective_session"]; ticker = sec_by[sid]["ticker"]
            outcome = qfq.fetch_one(ticker, sid, start, end)
            bars = qfq.load_cache(ticker,start,end) or []
            front = {b.session_date.isoformat():b.close for b in bars}
            raw_change = float(r["close"])/float(r["previous_close"])-1
            front_change = front[day]/front[r["previous_session"]]-1 if day in front and r["previous_session"] in front else None
            conclusion = classify_jump(raw_change, front_change)
            if conclusion != "real_price_move_qfq_confirmed":
                blocked.append({"security_id":sid,"effective_session":day,"reason":"unresolved_price_jump"})
            r["conclusion"]=conclusion; r["review_note"]=f"QFQ comparison;raw_change={raw_change};qfq_change={front_change};source={qfq._cache_path(ticker,start,end).relative_to(root)}"
            jump_reviews.append({**r,"raw_change":raw_change,"qfq_change":front_change})
        write_csv(work / "stage2_scoring_blocks.csv", blocked, ["security_id","effective_session","reason"])
        all_gaps = read_csv(work / "stage2_corporate_action_gaps.csv")
        review_by={(r["security_id"],r["effective_session"]):r for r in jump_reviews}
        cleaned=[]
        for r in all_gaps:
            if r["message"] in {"distinct_cash_dividends_require_source_review","bank_dividend_missing_in_truncated_mixed_feed"}: continue
            if (r["security_id"],r.get("effective_session")) in review_by:
                r.update({k:review_by[(r["security_id"],r["effective_session"])][k] for k in ("conclusion","review_note")})
            cleaned.append(r)
        write_csv(work / "stage2_corporate_action_gaps.csv",cleaned,list(all_gaps[0]))
        write_csv(work / "acceptance/price_jump_review_audit.csv",jump_reviews,list(jump_reviews[0]) if jump_reviews else ["empty"])
        unpriced=read_csv(work / "stage2_unpriced_members.csv")
        for r in unpriced:
            if r["security_id"] in cvr_ids: r["classification"]="excluded_contingent_value_right"
            if r["ticker"]=="EA":
                r["classification"]="historical_member_no_price"
                if "issuer confirms acquisition 2026-08-04" not in r["failure_detail"]:
                    r["failure_detail"]+=";issuer confirms acquisition 2026-08-04;https://www.ea.com/security/news/ea-announces-completion-of-acquisition;Yahoo probe did not return historical EA equity series"
        write_csv(work / "stage2_unpriced_members.csv",unpriced,list(unpriced[0]))
        cfg.update({"remediable_gap_max_ratio":.01,"historical_no_price_max_ratio":None,
            "weekly_scored_industry_min_ratio":.95,"score_coverage_gate_blocks":False,
            "gate_note":"Engineering gates, unvalidated; <=1% repairable gaps, historical no-price reported without a gate; >=95% industry among scored members"})
        cfg["excluded_security_types"] = list(dict.fromkeys(list(cfg.get("excluded_security_types", DEFAULTS["excluded_security_types"])) + ["contingent_value_right"]))
        assert cfg["sec_user_agent"]==private_value
        cfg_path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        report.update({"spinoff_valuation_counts":dict(Counter(r["spinoff_valuation"] for r in valuation_rows)),
            "dividend_groups":dict(Counter(r["result"] for r in group_reviews)),"bny_cash_added":len(bny_added),
            "excluded_cvr_records":len(cvr_ids),"jump_results":dict(Counter(r["conclusion"] for r in jump_reviews)),
            "quota_after":fetcher.query_kline_quota(),"requests_none":fetcher.request_count,"requests_qfq":qfq.request_count})
        (work / "stage2_enrichment_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return report
    finally:
        fetcher.close();qfq.close()
