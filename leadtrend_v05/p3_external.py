"""External data for P3 (plan 9.3(5)-(6)): FRED DTB3, the early-history check, and the OpenD coverage check.

Network: FRED (public, no key); SEC EDGAR with the User-Agent in config/v05_stage2.json (read, passed on, never printed,
logged or written); OpenD on the configured localhost port. Files: the FRED series is stored once under
data/v05/raw/external/ with a sidecar (source URL, retrieval time in UTC, sha256); the rest goes to the caller's
output folder under work/. Nothing here changes existing files under data/ or research/.
"""
import csv
import hashlib
import io
import json
import re
import socket
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from .sec_nport import TARGET_REG_CIK, _http_get, fetch_submissions, require_sec_user_agent

FRED_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3'
DTB3_RAW = 'data/v05/raw/external/DTB3.csv'
DTB3_SIDECAR = 'data/v05/raw/external/DTB3.source.json'
EARLY_FORMS = {'N-Q', 'N-Q/A', 'N-CSR', 'N-CSRS', 'N-CSR/A', 'N-CSRS/A'}
CUSIP_CELL = re.compile(r'^[0-9A-Z]{8}[0-9]$')


def download_dtb3(root):
    """Store the FRED DTB3 series once, as downloaded, with its source sidecar. Returns the sidecar."""
    root = Path(root)
    target, sidecar = root / DTB3_RAW, root / DTB3_SIDECAR
    if target.exists() and sidecar.exists():
        return json.loads(sidecar.read_text(encoding='utf-8'))
    request = urllib.request.Request(FRED_URL, headers={'User-Agent': 'LeadTrend research (FRED public series)'})
    retrieved = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    rows = list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
    dates = [r.get('observation_date') or r.get('DATE') for r in rows]
    record = dict(source_url=FRED_URL, series='DTB3: 3-Month Treasury Bill Secondary Market Rate, percent per year',
                  retrieved_at_utc=retrieved, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                  observations=len(rows), first_date=dates[0], last_date=dates[-1],
                  note='stored as downloaded; every analysis uses observations up to 2024-06-28 only')
    sidecar.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return record


class _Tables(HTMLParser):
    """Every table row of an HTML filing as a list of cell texts."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._row = []
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self._row is not None and self._cell is not None:
            self._row.append(' '.join(''.join(self._cell).split()))
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_holdings_html(raw):
    """Best-effort parse of a pre-2019 N-Q or N-CSR table: rows with a CUSIP cell, a shares-type header, a total line."""
    parser = _Tables()
    parser.feed(raw.decode('utf-8', errors='replace'))
    holdings = []
    for row in parser.rows:
        cusips = [c for c in row if CUSIP_CELL.match(c)]
        if cusips:
            numbers = [c for c in row if re.fullmatch(r'\(?\$?[0-9][0-9,]*(\.[0-9]+)?\)?', c)]
            holdings.append(dict(cusip=cusips[0], cells=row, last_number=numbers[-1] if numbers else ''))
    headers = [row for row in parser.rows if any(re.search(r'shares|principal', c, re.I) for c in row)]
    totals = [row for row in parser.rows if row and re.search(r'total\s+investments', row[0], re.I)]
    return dict(rows=len(parser.rows), holdings=holdings, shares_header_rows=len(headers),
                total_investment_rows=[row for row in totals][:3])


def _submissions(ua):
    sub = fetch_submissions(TARGET_REG_CIK, ua)
    filings = {k: list(v) for k, v in sub['filings']['recent'].items()}
    for extra in sub['filings'].get('files', []):
        more = json.loads(_http_get(f"https://data.sec.gov/submissions/{extra['name']}", ua).decode('utf-8'))
        for k in filings:
            filings[k].extend(more.get(k, []))
    return [dict(form=filings['form'][i], filing_date=filings['filingDate'][i], report_date=filings.get('reportDate', [''] * len(filings['form']))[i],
                 accession=filings['accessionNumber'][i], document=filings['primaryDocument'][i])
            for i in range(len(filings['form']))]


def early_history(root, out, opend_host='127.0.0.1', opend_port=11111, sample_year=2015):
    """9.3(6): what the SEC lists for 2010-2019, one parsed sample, and OpenD coverage of its holdings. Reports only."""
    root, out = Path(root), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((root / 'config/v05_stage2.json').read_text(encoding='utf-8'))
    ua = require_sec_user_agent(cfg.get('sec_user_agent'))  # used for requests only
    filings = [f for f in _submissions(ua) if f['form'] in EARLY_FORMS and '2010-01-01' <= f['filing_date'] <= '2019-12-31']
    by_year = {}
    for f in filings:
        key = f"{f['filing_date'][:4]} {f['form']}"
        by_year[key] = by_year.get(key, 0) + 1
    formats = {}
    for f in filings:
        ext = Path(f['document']).suffix.lower() or '(none)'
        formats[ext] = formats.get(ext, 0) + 1
    report = dict(filings_2010_2019=len(filings), by_year_and_form=dict(sorted(by_year.items())), document_formats=formats,
                  sample=None, opend=None, leavers=None)
    sample_filings = [f for f in filings if f['filing_date'].startswith(str(sample_year))]
    if sample_filings:
        pick = sorted(sample_filings, key=lambda f: (f['form'] != 'N-Q', f['filing_date']))[0]
        url = f"https://www.sec.gov/Archives/edgar/data/{int(TARGET_REG_CIK)}/{pick['accession'].replace('-', '')}/{pick['document']}"
        raw = _http_get(url, ua)
        (out / f"sample_{sample_year}_{pick['accession']}{Path(pick['document']).suffix}").write_bytes(raw)
        parsed = parse_holdings_html(raw)
        cusip_map = _cusip_map(root)
        sample_sids = {}
        for h in parsed['holdings']:
            if h['cusip'] in cusip_map:
                sample_sids[h['cusip']] = cusip_map[h['cusip']]
        report['sample'] = dict(form=pick['form'], filing_date=pick['filing_date'], report_date=pick['report_date'],
                                accession=pick['accession'], table_rows=parsed['rows'], holdings_with_cusip=len(parsed['holdings']),
                                shares_header_rows=parsed['shares_header_rows'],
                                total_investment_lines=[' | '.join(r) for r in parsed['total_investment_rows']],
                                holdings_matched_to_our_securities=len(sample_sids),
                                holdings_unmatched=len(parsed['holdings']) - sum(1 for h in parsed['holdings'] if h['cusip'] in cusip_map))
        report['opend'] = _opend_coverage(root, out, sample_sids, pick['report_date'] or pick['filing_date'], opend_host, opend_port)
        report['leavers'] = _leavers(root, sample_sids)
    (out / 'early_history.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def _cusip_map(root):
    """CUSIP -> (security_id, ticker) from the securities table's source evidence (cusip=...)."""
    out = {}
    with (root / 'data/v05/normalized/securities.csv').open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            m = re.search(r'cusip=([0-9A-Z]{9})', r.get('source_evidence', '') or '')
            if m:
                out.setdefault(m.group(1), (r['security_id'], r.get('ticker', '')))
    return out


def _opend_coverage(root, out, sample_sids, start, host, port):
    """Daily bars from the sample date to 2019-11-29 for each matched holding, through OpenD, if it answers."""
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            pass
    except OSError as exc:
        return dict(status='not reachable', detail=type(exc).__name__, requested=0)
    from .opend_prices import OpenDPriceFetcher
    fetcher = OpenDPriceFetcher(out / 'opend_cache', host=host, port=int(port))
    results = []
    try:
        for cusip, (sid, ticker) in sorted(sample_sids.items()):
            if not ticker:
                results.append(dict(cusip=cusip, security_id=sid, ok=False, n_bars=0, error='no ticker'))
                continue
            res = fetcher.fetch_one(ticker, sid, start, '2019-11-29')
            results.append(dict(cusip=cusip, security_id=sid, ticker=ticker, ok=res.ok, n_bars=res.n_bars, error=res.error))
    finally:
        fetcher.close()
    return dict(status='answered', requested=len(results), with_bars=sum(1 for r in results if r['ok'] and r['n_bars'] > 0),
                results=results)


def _leavers(root, sample_sids):
    """Sample holdings not held in the VOO snapshot available on 2019-11-29: they left the index before the segment."""
    with (root / 'data/v05/normalized/snapshots.csv').open(encoding='utf-8', newline='') as f:
        snaps = [r for r in csv.DictReader(f) if r['available_at'] <= '2019-11-29T23:59:59Z']
    snapshot = max(snaps, key=lambda r: (r['holdings_as_of'], r['available_at']))['snapshot_id'] if snaps else None
    members = set()
    with (root / 'data/v05/normalized/snapshot_members.csv').open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            if r['snapshot_id'] == snapshot and float(r['weight'] or 0) > 0:
                members.add(r['security_id'])
    left = [sid for _, (sid, _) in sample_sids.items() if sid not in members]
    return dict(snapshot=snapshot, sample_matched=len(sample_sids), left_the_index=len(left), left_security_ids=left)
