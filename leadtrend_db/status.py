"""Listing status of every issuer that was ever in VOO, and successor links.

Two separate facts are kept apart:
  * latest_common: the issuer's common or ADR shares appear in the latest report-date snapshot (a CVR or a right of
    the same issuer does not count);
  * current evidence on the build date (status_as_of): the SEC lists an NYSE, Nasdaq or Cboe exchange now, and the
    issuer's Form 25 / 25-NSE / Form 15 filings.

Rules:
  in_index                 listed now and latest_common
  listed_out_of_index      listed now, not in the latest snapshot (exchange listing and holdings are not the same)
  delisted                 not listed now and a Form 25 / 25-NSE (delisting date = last one + 10 days) or a Form 15
                           (date of the last one) exists. If latest_common still holds on or after that date, the
                           row is also flagged needs_review (the latest holding contradicts the notice).
  continued_by_successor   the issuer is the predecessor of a successor link (issuer_links)
  unknown                  everything else: no current listing and no delisting notice, or the identity is not
                           confirmed. Never declared delisted without a notice; needs_review = 1.
"""
from __future__ import annotations

from datetime import date, timedelta

LISTED_WORDS = ('nyse', 'nasdaq', 'cboe', 'bats')
FORM25 = ('25', '25-NSE')
FORM15 = ('15-12B', '15-12G', '15-15D')
PERIODIC = ('10-K', '10-Q', '10-K405', '20-F', '40-F')
MERGER_FORMS = ('DEFM14A', 'SC 14D9', 'SC TO-T', 'SC TO-C', 'SC 14D9/A', 'SC TO-T/A')
GOING_PRIVATE_FORMS = ('SC 13E3', 'SC 13E3/A')
SUCCESSOR_FORM = '8-K12B'


def filing_rows(page: dict) -> list[dict]:
    """Rows of a submissions 'recent' block or a paginated submissions file (parallel arrays)."""
    n = len(page.get('form', []))
    return [dict(form=page['form'][i], filing_date=page['filingDate'][i],
                 report_date=page['reportDate'][i], accession=page['accessionNumber'][i],
                 document=page['primaryDocument'][i], acceptance=page['acceptanceDateTime'][i])
            for i in range(n)]


def listed_now(exchanges: list[str]) -> bool:
    return any(any(w in (ex or '').lower() for w in LISTED_WORDS) for ex in exchanges)


def _d(text: str) -> date:
    return date.fromisoformat(text)


def merger_reason(filings: list[dict], end: date) -> str:
    """A merger proxy, tender offer or going-private filing within a year before the exit date; else unknown."""
    window = [f for f in filings if end - timedelta(days=365) <= _d(f['filing_date']) <= end + timedelta(days=30)]
    if any(f['form'] in GOING_PRIVATE_FORMS for f in window):
        return 'going_private'
    if any(f['form'] in MERGER_FORMS for f in window):
        return 'acquired_or_merged'
    return 'unknown'


def classify(d: dict, latest_common: bool, status_as_of: str, latest_date: str) -> dict:
    """Preliminary status of one issuer. d: exchanges, filings (filing_rows), source_id; latest_date = latest
    report date of the fund's snapshots."""
    filings = d['filings']
    exchanges = d.get('exchanges', [])
    out = dict(status='unknown', status_as_of=status_as_of, delisting_date='', reason='unknown', counterparty='',
               evidence='', method='', needs_review=0, source_id=d.get('source_id', ''))
    if listed_now(exchanges):
        out.update(status='in_index' if latest_common else 'listed_out_of_index',
                   method='exchange_listed_now' + ('+latest_common' if latest_common else ''),
                   evidence=f'exchanges={sorted(exchanges)}')
        return out
    f25 = sorted(f['filing_date'] for f in filings if f['form'] in FORM25)
    f15 = sorted(f['filing_date'] for f in filings if f['form'] in FORM15)
    if f25 or f15:
        if f25:
            end = _d(f25[-1]) + timedelta(days=10)
            method, evidence = 'form25_filing_date_plus_10d', f'last Form 25 filed {f25[-1]}'
        else:
            end = _d(f15[-1])
            method, evidence = 'form15_filing_date', f'last Form 15 filed {f15[-1]}'
        out.update(status='delisted', delisting_date=end.isoformat(), method=method,
                   evidence=f'{evidence}; not listed now (exchanges={sorted(exchanges)})',
                   reason=merger_reason(filings, end))
        if latest_common and end.isoformat() <= latest_date:
            out.update(needs_review=1, evidence=out['evidence'] + '; common shares still in the latest snapshot')
        return out
    if latest_common:
        out.update(status='unknown', needs_review=1, method='latest_holding_without_listing_evidence',
                   evidence=f'in latest snapshot; exchanges={sorted(exchanges)}; no Form 25/15 on file')
        return out
    if not filings:
        out.update(status='unknown', needs_review=1, method='no_submissions', evidence='no filings in submissions')
        return out
    recent = [f for f in filings if f['form'] in PERIODIC and _d(f['filing_date']) > _d(status_as_of) - timedelta(days=365)]
    last = max(f['filing_date'] for f in filings)
    out.update(status='unknown', needs_review=1, method='no_listing_no_delisting_notice',
               evidence=f'no Form 25/15; last filing {last}; exchanges={sorted(exchanges)}'
                        + ('; periodic report within a year' if recent else ''))
    return out


def detect_successors(info: dict[str, dict], delisted: dict[str, date], window_days: int = 120,
                      high_ratio: float = 0.85) -> tuple[list[dict], list[dict]]:
    """Successor links for delisted issuers: an 8-K12B by another issuer in the universe within window_days of the
    exit, with a name similar to the predecessor's (current or former) name. Only a clear name match (ratio at least
    high_ratio) gives a link; any other candidate goes to review."""
    from .identity import name_ratio

    succ_forms = []
    for cik, d in info.items():
        for f in d['filings']:
            if f['form'] in ('8-K12B', '8-K12G3'):
                succ_forms.append((cik, _d(f['filing_date'])))
    links, reviews = [], []
    for pred in sorted(delisted):
        end = delisted[pred]
        cands = []
        for scik, sdate in succ_forms:
            if scik == pred or abs((sdate - end).days) > window_days:
                continue
            names = [info[scik]['name']] + list(info[scik].get('former_names', []))
            ratio = max(name_ratio(info[pred]['name'], n) for n in names)
            cands.append((round(ratio, 4), scik, sdate.isoformat()))
        good = sorted([c for c in cands if c[0] >= high_ratio], reverse=True)
        if len(good) == 1:
            ratio, scik, sdate = good[0]
            links.append(dict(predecessor=pred, successor=scik, relation='successor_reorganization',
                              confidence='high', via_override=0, effective_date=sdate,
                              evidence=f'8-K12B {sdate} by {scik}; name similarity {ratio}'))
        elif cands:
            reviews.append(dict(review_type='successor_candidate', subject_id=pred,
                                detail='; '.join(f'{c[1]} 8-K12B {c[2]} sim {c[0]}' for c in sorted(cands, reverse=True))))
    return links, reviews
