"""Security identity, exclusion rules and issuer share-class selection."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from statistics import median

from .config import BootstrapConfig
from .dataio import Inputs, PriceRow, SecurityRow


@dataclass
class EligibilityResult:
    security_id: str
    eligible: bool
    reason: str
    issuer_id: str
    industry: str
    security_type: str
    ticker: str
    exchange: str


def is_excluded_type(security_type: str, config: BootstrapConfig) -> bool:
    return security_type.strip().lower() in {t.lower() for t in config.excluded_security_types}


def is_excluded_ticker(ticker: str, config: BootstrapConfig) -> bool:
    return ticker.strip().upper() in {t.upper() for t in config.excluded_tickers}


def evaluate_security(sec: SecurityRow, config: BootstrapConfig) -> EligibilityResult:
    reasons: list[str] = []
    if is_excluded_ticker(sec.ticker, config):
        reasons.append("excluded_ticker")
    if is_excluded_type(sec.security_type, config):
        reasons.append("excluded_security_type")
    if sec.exchange.strip().upper() == "OTC":
        reasons.append("otc_exchange")
    # Equity preferred ADR is allowed; fixed-income preferred is excluded via security_type.
    eligible = not reasons
    reason = "eligible" if eligible else ";".join(reasons)
    return EligibilityResult(
        security_id=sec.security_id,
        eligible=eligible,
        reason=reason,
        issuer_id=sec.issuer_id,
        industry=sec.industry,
        security_type=sec.security_type,
        ticker=sec.ticker,
        exchange=sec.exchange,
    )


class ClassSelector:
    """Pick one share class per issuer. Does not depend on trend window L or N."""

    def __init__(self, config: BootstrapConfig, prices_by_sec: dict[str, list[PriceRow]]):
        self.config = config
        self.prices_by_sec = prices_by_sec
        self.price_dates = {sid: [p.session_date for p in rows] for sid, rows in prices_by_sec.items()}
        self.chosen: dict[str, str] = {}  # issuer_id -> security_id
        self.notes: list[dict] = []

    def turnover_median(self, security_id: str, as_of: date) -> float | None:
        all_rows = self.prices_by_sec.get(security_id, [])
        end = bisect_right(self.price_dates.get(security_id, []), as_of)
        rows = all_rows[max(0, end - self.config.class_selection_turnover_days):end]
        if not rows:
            return None
        vals: list[float] = []
        for p in rows:
            if p.turnover is not None:
                vals.append(float(p.turnover))
            elif p.volume is not None:
                vals.append(float(p.volume) * float(p.close))
            else:
                return None
        return float(median(vals))

    def select_for_issuer(
        self,
        issuer_id: str,
        candidates: list[EligibilityResult],
        as_of: date,
    ) -> str | None:
        """candidates must already be eligible and in-universe."""
        if not candidates:
            # previously chosen class may have left; drop memory
            self.chosen.pop(issuer_id, None)
            return None
        by_id = {c.security_id: c for c in candidates}
        prev = self.chosen.get(issuer_id)
        if prev is not None and prev in by_id:
            return prev

        scored: list[tuple[float | None, str, EligibilityResult]] = []
        for c in candidates:
            tv = self.turnover_median(c.security_id, as_of)
            scored.append((tv, c.security_id, c))

        with_tv = [x for x in scored if x[0] is not None]
        if with_tv and len(with_tv) == len(scored) and len(scored) > 1:
            with_tv.sort(key=lambda x: (-float(x[0]), x[1]))
            chosen_id = with_tv[0][1]
            self.chosen[issuer_id] = chosen_id
            self.notes.append(
                {
                    "issuer_id": issuer_id,
                    "security_id": chosen_id,
                    "method": "turnover_median",
                    "as_of": as_of.isoformat(),
                }
            )
            return chosen_id

        # Degraded fallback: stable security_id, do not exclude the company.
        ordered = sorted(candidates, key=lambda c: c.security_id)
        chosen_id = ordered[0].security_id
        self.chosen[issuer_id] = chosen_id
        method = "stable_security_id_degraded"
        if not with_tv:
            reason = "turnover_unavailable"
        elif len(scored) == 1:
            method = "single_class"
            reason = "single_eligible_class"
        else:
            reason = "turnover_not_comparable"
        self.notes.append(
            {
                "issuer_id": issuer_id,
                "security_id": chosen_id,
                "method": method,
                "reason": reason,
                "as_of": as_of.isoformat(),
            }
        )
        return chosen_id


def build_price_index(prices: list[PriceRow]) -> dict[str, list[PriceRow]]:
    idx: dict[str, list[PriceRow]] = {}
    for p in prices:
        idx.setdefault(p.security_id, []).append(p)
    for rows in idx.values():
        rows.sort(key=lambda r: r.session_date)
    return idx


def security_index(securities: list[SecurityRow]) -> dict[str, SecurityRow]:
    return {s.security_id: s for s in securities}


def eligibility_index(inputs: Inputs) -> dict[str, EligibilityResult]:
    return {
        s.security_id: evaluate_security(s, inputs.config)
        for s in inputs.securities
    }
