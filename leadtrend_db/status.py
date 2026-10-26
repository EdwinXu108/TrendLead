"""Listing status of every issuer that was ever in VOO, and successor links.

Status rules (decided on the build date, from the SEC submissions files and the latest snapshot):
  in_index                 in the latest fund snapshot
  listed_out_of_index      not in the latest snapshot, but the SEC submissions list an NYSE, Nasdaq or Cboe exchange
  continued_by_successor   not listed, and a successor issuer (issuer_links) carries on the business
  delisted                 not listed on those exchanges (acquired, merged away, taken private, bankrupt,
                           moved to OTC, or only debt registered); delisting_date from the first Form 25 (filing
                           date + 10 days, the effective date of the delisting) or else the first Form 15
  unknown                  no usable evidence, or a periodic report was filed in the last year without a Form 25
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
    """Cheap, transparent hint: a merger proxy or tender offer near the exit date, else unknown."""
    window = [f for f in filings if end - timedelta(days=365) <= _d(f['filing_date']) <= end + timedelta(days=30)]
    if any(f['form'] in GOING_PRIVATE_FORMS for f in window):
        return 'going_private'
    if any(f['form'] in MERGER_FORMS for f in window):
        return 'acquired_or_merged'
    return 'unknown'


def classify(d: dict, in_latest: bool, status_as_of: str) -> dict:
    """Preliminary status for one issuer. d: name, exchanges, filings (list of filing_rows), source_id."""
    filings = d['filings']
    exchanges = d.get('exchanges', [])
    out = dict(status='unknown', status_as_of=status_as_of, delisting_date='', reason='unknown', counterparty='',
               evidence='', method='', needs_review=0, source_id=d.get('source_id', ''))
    if in_latest:
        out.update(status='in_index', method='in_latest_snapshot')
        return out
    if listed_now(exchanges):
        out.update(status='listed_out_of_index', method='submissions_exchanges',
                   evidence=f'exchanges={sorted(exchanges)}')
        return out
    f25 = sorted(f['filing_date'] for f in filings if f['form'] in FORM25)
    f15 = sorted(f['filing_date'] for f in filings if f['form'] in FORM15)
    if f25:   # the last notice is the final delisting (an earlier 25-NSE can be withdrawn or unrelated)
        end = _d(f25[-1]) + timedelta(days=10)
        out.update(status='delisted', delisting_date=end.isoformat(), method='form25_filing_date_plus_10d',
                   evidence=f'last Form 25 filed {f25[-1]}; exchanges={sorted(exchanges)}')
    elif f15:
        end = _d(f15[-1])
        out.update(status='delisted', delisting_date=end.isoformat(), method='form15_filing_date',
                   evidence=f'last Form 15 filed {f15[-1]}; exchanges={sorted(exchanges)}')
    elif not filings:
        out.update(evidence='no filings in submissions', needs_review=1)
        return out
    else:
        last = max(f['filing_date'] for f in filings)
        recent = [f for f in filings if f['form'] in PERIODIC
                  and _d(f['filing_date']) > _d(status_as_of) - timedelta(days=365)]
        if recent:
            out.update(status='unknown', needs_review=1, method='no_exchange_but_recent_periodic_filings',
                       evidence=f'exchanges={sorted(exchanges)}; last periodic {recent[0]["filing_date"]}')
            return out
        end = _d(last)
        out.update(status='delisted', delisting_date=end.isoformat(), method='no_exchange_last_filing_date',
                   needs_review=1, evidence=f'no Form 25/15; last filing {last}; exchanges={sorted(exchanges)}')
    out['reason'] = merger_reason(filings, end)
    return out


def detect_successors(info: dict[str, dict], delisted: dict[str, date], window_days: int = 120,
                      high_ratio: float = 0.85) -> tuple[list[dict], list[dict]]:
    """Successor links for delisted issuers: a Form 8-K12B by another issuer in the universe within window_days of
    the exit, with a similar name. One clear candidate gives a link; several or none give a review item."""
    from .identity import name_ratio

    succ_forms = []
    for cik, d in info.items():
        for f in d['filings']:
            if f['form'] == SUCCESSOR_FORM:
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
        if len(good) == 1:   # automatic links only on a clear name match; everything else goes to review
            ratio, scik, sdate = good[0]
            links.append(dict(predecessor=pred, successor=scik, relation='successor_reorganization',
                              confidence='high', via_override=0, effective_date=sdate,
                              evidence=f'8-K12B {sdate} by {scik}; name similarity {ratio}'))
        elif cands:
            reviews.append(dict(review_type='successor_candidate', subject_id=pred,
                                detail='; '.join(f'{c[1]} 8-K12B {c[2]} sim {c[0]}' for c in sorted(cands, reverse=True))))
    return links, reviews
