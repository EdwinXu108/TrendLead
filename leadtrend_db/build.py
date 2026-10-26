"""Assemble leadtrend_db v1 from the raw archive and write data/db/leadtrend_db_v1.sqlite.

Each input is read through RawStore: an archived file is read from disk (sha256 verified); a missing one is fetched
from SEC once and archived. offline=True forbids fetching. The database is a function of the archive and
config/db_overrides_v1.csv only; the content hash leaves out timestamps and build metadata, so two builds from the
same archive hash identically.

Identity order for a holding (no placeholder is ever a key):
  security  : valid CUSIP -> US ISIN's CUSIP -> v05 security whose evidence carries the ISIN -> ISIN itself -> none
  issuer    : override -> v05 SEC-corroborated CIK -> SEC current ticker (name-checked) -> SEC name lookup
              (each candidate must be valid at the holding date: a SEC name in force that day and a periodic filing
              near it). A candidate that fails is never accepted silently.
  2019-12-31: the N-CSR rows are matched to N-PORT rows that have a valid security key (shares within 1%, same class,
              similar name). Pre-N-PORT names inherit the security of their match.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import sources as S
from .identity import (CLASS_LETTER, EXCLUDED_TYPES, cik10, cikcls_security, class_code_from_text, core_name,
                       cusip_security, isin_security, issuer_id, load_overrides, name_key, name_ratio, name_signature, norm_core,
                       overrides_of, parse_cik_lookup, parse_company_tickers, security_type, ticker_key,
                       us_isin_cusip, v05_evidence_fields, valid_cusip, valid_isin, valid_ticker)
from .parse_ncsr import parse_500_schedule
from .parse_nport import holdings_lines, screen
from .sources import RawMissing, RawStore, sha256_bytes, utc_now_iso
from .status import PERIODIC, classify, detect_successors, filing_rows

SCHEMA_FILE = Path(__file__).with_name('schema.sql')
SCHEMA_VERSION = 1
PRE_FORMS = ('N-Q', 'N-CSR', 'N-CSRS', 'NPORT-EX')
KIND = {'N-Q': 'nq', 'N-CSR': 'ncsr', 'N-CSRS': 'ncsrs', 'NPORT-EX': 'other', 'NPORT-P': 'nport'}
SNAP_PREFIX = {'N-Q': 'NQ', 'N-CSR': 'NCSR', 'N-CSRS': 'NCSRS', 'NPORT-EX': 'NPORTEX', 'NPORT-P': 'NPORT'}
PRE_START, BRIDGE_DATE, NPORT_START, LATEST_QUARTER = '2013-12-31', '2019-12-31', '2019-09-30', '2026-06-30'
FILED_FROM = '2014-01-01'
MIN_FULL_ROWS = 400             # a 500 Index Fund schedule has about 500 equity lines
COVERAGE_FULL = 0.95            # N-PORT: stock total must be at least this share of total investments
NAME_BRIDGE_RATIO = 0.5
BRIDGE_SHARE_TOL = 0.01         # relative share-count tolerance for the 2019-12-31 match (sources differ by ~0.3%)
CIK_PERIOD_DAYS = 95            # a periodic filing this close to the holding date supports a CIK
ARCHIVE = 'https://www.sec.gov/Archives/edgar/data/36405'
SUBMISSIONS = 'https://data.sec.gov/submissions'
V05_SECURITIES = 'securities.csv'
V05_SNAPSHOT_MEMBERS = 'snapshot_members.csv'
V05_SNAPSHOTS = 'snapshots.csv'
TABLE_COLUMNS = {
    'sources': ['source_id', 'kind', 'url', 'raw_path', 'sha256', 'bytes', 'retrieved_at_utc', 'note'],
    'issuers': ['issuer_id', 'cik', 'name', 'sic', 'sic_description', 'state_of_incorporation', 'lei', 'source_id'],
    'issuer_names': ['issuer_id', 'name', 'name_kind', 'valid_from', 'valid_to', 'first_seen', 'last_seen', 'source_id'],
    'issuer_links': ['predecessor_issuer_id', 'successor_issuer_id', 'relation', 'confidence', 'via_override',
                     'effective_date', 'evidence', 'source_id'],
    'securities': ['security_id', 'issuer_id', 'class_code', 'security_type', 'type_basis', 'name', 'id_scheme', 'notes',
                   'source_id'],
    'security_identifiers': ['security_id', 'id_type', 'id_value', 'valid_from', 'valid_to', 'source_id'],
    'snapshots': ['snapshot_id', 'fund_series', 'form', 'accession', 'primary_doc', 'holdings_as_of', 'filed_date',
                  'available_at_utc', 'is_primary', 'completeness', 'row_count', 'excluded_rows', 'excluded_value_usd',
                  'total_investments_reported_usd', 'reported_common_stock_total_usd', 'stock_total_usd',
                  'coverage_ratio', 'reconciled', 'start_rule', 'source_id', 'notes'],
    'holdings': ['snapshot_id', 'line_no', 'line_kind', 'raw_name', 'raw_class_text', 'cusip_reported', 'isin_reported',
                 'lei_reported', 'title_reported', 'shares', 'value_usd', 'pct_val_percent', 'weight',
                 'security_id', 'issuer_id', 'map_method', 'map_confidence', 'unmapped_reason', 'source_id'],
    'issuer_resolution': ['issuer_id', 'resolved_issuer_id', 'method'],
    'membership_spans': ['level', 'subject_id', 'issuer_id', 'span_no', 'first_as_of', 'last_as_of',
                         'first_available_at_utc', 'last_available_at_utc', 'quarters_present', 'next_absent_as_of'],
    'listing_status': ['issuer_id', 'status', 'status_as_of', 'delisting_date', 'reason', 'counterparty', 'evidence',
                       'method', 'needs_review', 'source_id'],
    'review_items': ['review_id', 'review_type', 'subject_id', 'snapshot_id', 'detail', 'source_id'],
}


def source_id(sha256: str) -> str:
    return 'SRC-' + sha256[:16]


def acc_nodash(accession: str) -> str:
    return accession.replace('-', '')


def quarter_ends(start: str, end: str) -> list[str]:
    out = []
    y, m = int(start[:4]), int(start[5:7])
    while True:
        last = (date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)).isoformat()
        if last > end:
            break
        if last >= start:
            out.append(last)
        m += 3
        if m > 12:
            y, m = y + 1, m - 12
    return out


def _ts(text: str) -> str:
    """SEC acceptance time ('2014-02-28T19:29:40.000Z') as ISO UTC without fractions."""
    return datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _quarter_index(d: str) -> int:
    return int(d[:4]) * 4 + (int(d[5:7]) - 1) // 3


def _day10(text: str) -> str:
    return (text or '')[:10]


def _code_commit() -> str:
    try:
        out = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=S.ROOT, capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


class Build:
    def __init__(self, store: RawStore, log=print):
        self.store = store
        self.log = log
        self.steps: list[tuple[str, str]] = []
        self.review: list[dict] = []
        self.sub_cache: dict[str, dict | None] = {}
        self.overrides = load_overrides(S.OVERRIDES_PATH)
        self.cusip_over = overrides_of(self.overrides, 'cusip_cik')
        self.name_over = overrides_of(self.overrides, 'name_cik')
        self.type_over = overrides_of(self.overrides, 'security_type')

    def step(self, name: str, detail: str = '') -> None:
        self.steps.append((name, detail))
        self.log(f'[{name}] {detail}')

    def flag(self, review_type: str, subject: str, snapshot: str = '', detail: str = '', source: str = '') -> None:
        self.review.append(dict(review_type=review_type, subject_id=subject, snapshot_id=snapshot, detail=detail,
                                source_id=source))

    # -- SEC inputs ----------------------------------------------------------------------------------------------
    def trust_filings(self) -> list[dict]:
        main = self.store.get_json(f'{SUBMISSIONS}/CIK{S.CIK}.json', f'submissions/CIK{S.CIK}.json', 'submissions',
                                   'trust submissions')
        rows = filing_rows(main['filings']['recent'])
        for f in main['filings'].get('files', []):
            rows += filing_rows(self.store.get_json(f'{SUBMISSIONS}/{f["name"]}', f'submissions/{f["name"]}',
                                                    'submissions', 'trust submissions page'))
        seen, out = set(), []
        for r in rows:
            if r['accession'] not in seen:
                seen.add(r['accession'])
                out.append(r)
        return out

    def issuer_sub(self, cik: str) -> dict | None:
        """All submissions pages of one issuer, with formerNames and their dates. None if the SEC has no file."""
        if cik in self.sub_cache:
            return self.sub_cache[cik]
        raw = self.store.get_optional(f'{SUBMISSIONS}/CIK{cik}.json', f'submissions/CIK{cik}.json', 'submissions',
                                      'issuer submissions')
        if raw is None:
            self.sub_cache[cik] = None
            return None
        obj = json.loads(raw.decode('utf-8'))
        filings = filing_rows(obj['filings']['recent'])
        for f in obj['filings'].get('files', []):
            filings += filing_rows(self.store.get_json(f'{SUBMISSIONS}/{f["name"]}', f'submissions/{f["name"]}',
                                                       'submissions', 'issuer submissions page'))
        d = dict(cik=cik, name=obj.get('name', ''), sic=str(obj.get('sic') or ''),
                 sic_description=obj.get('sicDescription', '') or '',
                 state=obj.get('stateOfIncorporation', '') or '',
                 exchanges=list(obj.get('exchanges') or []), tickers=list(obj.get('tickers') or []),
                 former=[dict(name=x.get('name', ''), valid_from=_day10(x.get('from')), valid_to=_day10(x.get('to')))
                         for x in (obj.get('formerNames') or [])],
                 filings=filings, source_sha=sha256_bytes(raw))
        self.sub_cache[cik] = d
        return d

    def name_valid_at(self, cik: str, name: str, when: str) -> bool:
        """True if a SEC name of the issuer equal to this name was in force on the date (formerNames dates honoured)."""
        sub = self.issuer_sub(cik)
        if sub is None:
            return False
        target = name_signature(core_name(name))
        for f in sub['former']:
            if name_signature(core_name(f['name'])) == target and (not f['valid_from'] or f['valid_from'] <= when) and \
                    (not f['valid_to'] or when <= f['valid_to']):
                return True
        current_from = max([f['valid_to'] for f in sub['former'] if f['valid_to']] or [''])
        return name_signature(core_name(sub['name'])) == target and (not current_from or when >= current_from)

    def periodic_near(self, cik: str, when: str) -> bool:
        sub = self.issuer_sub(cik)
        if sub is None:
            return False
        d0 = date.fromisoformat(when)
        for f in sub['filings']:
            if f['form'] not in PERIODIC:
                continue
            if f['report_date'] and abs((date.fromisoformat(f['report_date']) - d0).days) <= CIK_PERIOD_DAYS:
                return True
            if d0 <= date.fromisoformat(f['filing_date']) <= d0 + timedelta(days=120):
                return True
        return False

    def valid_at(self, cik: str, name: str, when: str) -> bool:
        return self.name_valid_at(cik, name, when) and self.periodic_near(cik, when)

    def pre_snapshots(self, trust_rows: list[dict]) -> list[dict]:
        """N-Q, N-CSR, N-CSRS and NPORT-EX filings for report dates 2013-12-31 to 2019-12-31, filed from 2014. The
        500 Index Fund schedule is parsed from the primary document; if it does not reconcile, the other htm documents
        of the same filing are checked. An NPORT-EX exhibit without the schedule belongs to another fund and is left out."""
        out = []
        for f in trust_rows:
            if f['form'] not in PRE_FORMS or f['filing_date'] < FILED_FROM:
                continue
            if not (PRE_START <= f['report_date'] <= BRIDGE_DATE):
                continue
            acc = acc_nodash(f['accession'])
            url = f'{ARCHIVE}/{acc}/{f["document"]}'
            raw = self.store.get(url, f'filings/{acc}/{f["document"]}', KIND[f['form']],
                                 f'{f["form"]} report {f["report_date"]}')
            res, checked, used = parse_500_schedule(raw), [f['document']], f['document']
            if not (res['found'] and res.get('reconciled')) and f['form'] != 'NPORT-EX':
                index = self.store.get_json(f'{ARCHIVE}/{acc}/index.json', f'filings/{acc}/index.json',
                                            'filing_index', 'filing index')
                for item in index['directory']['item']:
                    name = item['name']
                    if name == f['document'] or '-index' in name.lower() or not name.lower().endswith(('.htm', '.html')):
                        continue
                    other = self.store.get_optional(f'{ARCHIVE}/{acc}/{name}', f'filings/{acc}/{name}', 'other',
                                                    'other document of the same filing')
                    if other is None:
                        checked.append(f'{name} (not served)')
                        continue
                    checked.append(name)
                    cand = parse_500_schedule(other)
                    if cand['found'] and cand.get('reconciled'):
                        raw, res, used = other, cand, name
                        break
            if f['form'] == 'NPORT-EX' and not res['found']:
                continue
            out.append(dict(form=f['form'], report_date=f['report_date'], filing_date=f['filing_date'],
                            accession=f['accession'], document=used, raw=raw, res=res, checked=checked,
                            acceptance=_ts(f['acceptance']), completeness=self.judge_schedule(res),
                            url=f'{ARCHIVE}/{acc}/{used}'))
        return out

    @staticmethod
    def judge_schedule(res: dict) -> str:
        if not res['found']:
            return 'summary'
        if res.get('reconciled') and len(res['stock_rows']) >= MIN_FULL_ROWS:
            return 'full'
        return 'partial'

    def nport_screen(self, trust_rows: list[dict]) -> dict[str, dict]:
        """Screen each N-PORT filing of the trust once; decisions are kept in raw/nport_screen.csv."""
        path = self.store.raw_dir / 'nport_screen.csv'
        rows: dict[str, dict] = {}
        if path.exists():
            with path.open(encoding='utf-8', newline='') as f:
                rows = {r['accession']: r for r in csv.DictReader(f)}
        added = 0
        for f in sorted((r for r in trust_rows if r['form'] == 'NPORT-P' and r['report_date'] >= NPORT_START),
                        key=lambda r: (r['report_date'], r['accession'])):
            if f['accession'] in rows:
                continue
            url = f'{ARCHIVE}/{acc_nodash(f["accession"])}/primary_doc.xml'
            relpath = f'nport/nport_{acc_nodash(f["accession"])}.xml'
            local = S.V05_RAW / f'nport_{S.FUND_SERIES}_{f["report_date"]}_{f["accession"]}.xml'
            if self.store.has(url):
                raw, source = self.store.get(url, relpath, 'nport'), 'archive'
            elif local.exists():
                raw, source = local.read_bytes(), 'v05_raw_local'
            elif self.store.offline:
                raise RawMissing(f'N-PORT screen missing for {f["accession"]}; run the build online once')
            else:
                raw, source = self.store.get(url, relpath, 'nport', 'N-PORT primary document'), 'sec_fetch'
            sc = screen(raw)
            decision = 'target' if sc['is_target'] else 'other_series'
            if decision == 'target' and source == 'v05_raw_local':
                self.store.import_local(url, relpath, 'nport', local,
                                        f'copied from data/v05/raw/{local.name}; same SEC URL')
            rows[f['accession']] = dict(accession=f['accession'], report_date=f['report_date'],
                                        series_id=sc['series_id'], decision=decision, source=source)
            added += 1
        if added or not path.exists():
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=['accession', 'report_date', 'series_id', 'decision', 'source'],
                               lineterminator='\n')
            w.writeheader()
            for acc in sorted(rows, key=lambda a: (rows[a]['report_date'], a)):
                w.writerow(rows[acc])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(buf.getvalue(), encoding='utf-8', newline='')
        self.step('nport_screen', f'{len(rows)} N-PORT filings screened, {added} new, '
                  f'{sum(1 for r in rows.values() if r["decision"] == "target")} for the 500 Index Fund')
        return rows

    def nport_snapshots(self, trust_rows: list[dict], screen_rows: dict[str, dict]) -> list[dict]:
        by_date: dict[str, list[dict]] = {}
        for r in screen_rows.values():
            if r['decision'] == 'target':
                by_date.setdefault(r['report_date'], []).append(r)
        acceptance = {r['accession']: r['acceptance'] for r in trust_rows}
        filed = {r['accession']: r['filing_date'] for r in trust_rows}
        out = []
        for rd in sorted(by_date):
            cands = sorted(by_date[rd], key=lambda r: acceptance[r['accession']])
            pick = cands[-1]
            if len(cands) > 1:
                self.flag('nport_multiple_targets', pick['accession'], '', f'{rd}: {[c["accession"] for c in cands]}; latest used')
            acc = pick['accession']
            url = f'{ARCHIVE}/{acc_nodash(acc)}/primary_doc.xml'
            raw = self.store.get(url, f'nport/nport_{acc_nodash(acc)}.xml', 'nport', 'N-PORT primary document')
            info = holdings_lines(raw)
            lines = [dict(raw_name=h['name'], raw_class_text=_class_text(h['title']), cusip=h['cusip'], isin=h['isin'],
                          lei=h['lei'], title=h['title'], shares=h['balance'], value_usd=h['val_usd'],
                          pct_val=h['pct_val'], asset_cat=h['asset_cat'], line_kind='stock', negative=False,
                          cls=class_code_from_text(h['title'] or h['name']), line_no=h['line_no'])
                     for h in info['lines']]
            total = info['total_investments_usd']
            stock_total_raw = sum(h['val_usd'] for h in info['lines'])
            complete = 'full' if len(lines) >= MIN_FULL_ROWS and total and stock_total_raw / total >= COVERAGE_FULL \
                else 'partial'
            out.append(dict(form='NPORT-P', snapshot_id=f'NPORT_{rd}_{acc}', accession=acc, primary_doc='primary_doc.xml',
                            report_date=rd, filing_date=filed[acc], acceptance=_ts(acceptance[acc]), raw=raw,
                            completeness=complete, lines=lines, total=total, reported_common=None,
                            excluded_rows=info['non_equity_rows'], excluded_value=info['non_equity_value_usd'],
                            reconciled=1, start_rule='nport_xml', notes='', url=url))
        return out

    # -- snapshots -----------------------------------------------------------------------------------------------
    def snapshots(self, trust_rows: list[dict]) -> list[dict]:
        screen_rows = self.nport_screen(trust_rows)
        snaps = []
        for p in self.pre_snapshots(trust_rows):
            res = p['res']
            lines = []
            if p['completeness'] != 'summary':
                for i, r in enumerate(res['stock_rows'], 1):
                    lines.append(dict(raw_name=r['name'], raw_class_text=r['raw_class_text'], cusip='', isin='', lei='',
                                      title='', shares=r['shares'], value_usd=r['value_usd'], pct_val=None,
                                      asset_cat='EC', line_kind='stock', negative=False,
                                      cls=class_code_from_text(r['raw_class_text']), line_no=i))
                for r in res['negative_rows']:
                    lines.append(dict(raw_name=r['name'], raw_class_text=r['raw_class_text'], cusip='', isin='', lei='',
                                      title='', shares=r['shares'], value_usd=r['value_usd'], pct_val=None,
                                      asset_cat='EC', line_kind='negative', negative=True,
                                      cls=class_code_from_text(r['raw_class_text']), line_no=len(lines) + 1))
            notes = [f'start rule {res.get("rule")}', f'checked {len(p["checked"])} document(s)'] + res['notes'][:5]
            snaps.append(dict(form=p['form'], snapshot_id=f'{SNAP_PREFIX[p["form"]]}_{p["report_date"]}_{p["accession"]}',
                              accession=p['accession'], primary_doc=p['document'], report_date=p['report_date'],
                              filing_date=p['filing_date'], acceptance=p['acceptance'], raw=p['raw'],
                              completeness=p['completeness'], lines=lines, total=res['total_investments_usd'],
                              reported_common=res['total_common_stocks_usd'],
                              excluded_rows=len(res['excluded']),
                              excluded_value=sum(e['value_usd'] for e in res['excluded']),
                              reconciled=int(bool(res.get('reconciled'))), start_rule=res.get('rule') or '',
                              notes='; '.join(notes), url=p['url']))
        snaps += self.nport_snapshots(trust_rows, screen_rows)
        snaps.sort(key=lambda s: (s['report_date'], s['form'], s['accession']))
        return snaps

    # -- identity ------------------------------------------------------------------------------------------------
    def v05_securities(self) -> dict[str, dict]:
        """v05 securities keyed by CUSIP, with the ISIN, the OpenFIGI type text and the SEC-corroborated CIK."""
        raw = self.v05_reference(V05_SECURITIES).decode('utf-8')
        out: dict[str, dict] = {}
        self.v05_isin_to_cusip: dict[str, str] = {}
        for r in csv.DictReader(io.StringIO(raw)):
            sid = r['security_id']
            if not sid.startswith('CUSIP:'):
                continue
            cusip = valid_cusip(sid[6:])
            if not cusip:
                continue
            ev = v05_evidence_fields(r.get('source_evidence') or '')
            isin = valid_isin(ev.get('isin', ''))
            if isin:
                self.v05_isin_to_cusip.setdefault(isin, cusip)
            out[cusip] = dict(ticker=valid_ticker(r.get('ticker', '')), evidence_cik=ev.get('evidence_cik', ''),
                              isin=isin, openfigi_type=ev.get('openfigi_type', ''), name=r.get('original_name', ''))
        return out

    def v05_reference(self, name: str) -> bytes:
        """Archived copy of a v05 normalized table: the build reads the copy, not data/v05."""
        return self.store.import_local(f'local:data/v05/normalized/{name}', f'reference/v05/{name}', 'other',
                                       S.V05_NORMALIZED / name, f'copied from data/v05/normalized/{name}')

    def cik_by_name(self, name: str, when: str, lookup: dict[str, set[str]]) -> tuple[str | None, str]:
        """CIK for a holding name at a date. Candidates from the SEC name lookup must each be valid at that date
        (name in force, periodic filing near it). A single valid candidate is accepted; several are narrowed by an
        exact SEC name; anything still open stays unmapped."""
        key = norm_core(name)
        if name_key(name) in self.name_over:
            return cik10(self.name_over[name_key(name)]['value']), 'override_name_cik'
        cands = sorted(lookup.get(key, set()))
        if not cands:
            return None, 'no_cik_candidate'
        valid = [c for c in cands if self.valid_at(c, name, when)]
        if len(valid) == 1:
            return valid[0], 'cik_lookup_name_valid_at_date' if len(cands) > 1 else 'cik_lookup_name_valid_at_date_single'
        if not valid:
            return None, f'no_candidate_valid_at_date:{len(cands)}'
        exact = [c for c in valid if self.issuer_sub(c) and any(name_key(n) == key for n in
                                                                [self.issuer_sub(c)['name']] +
                                                                [f['name'] for f in self.issuer_sub(c)['former']])]
        if len(exact) == 1:
            return exact[0], 'cik_lookup_name_exact_sec_name'
        return None, f'ambiguous_candidates:{len(cands)}->{len(valid)}'

    def security_issuer(self, cusip: str, name: str, title: str, when: str, lookup, tickers, v05) -> tuple[str | None, str, str, str]:
        """(cik, method, confidence, reason) for a security known by CUSIP (or a derived/ISIN identity)."""
        if cusip and cusip in self.cusip_over:
            return cik10(self.cusip_over[cusip]['value']), 'override_cusip_cik', 'high', ''
        ev = v05.get(cusip, {}) if cusip else {}
        if ev.get('evidence_cik') and self.valid_at(ev['evidence_cik'], name, when):
            return ev['evidence_cik'], 'v05_source_evidence_cik', 'high', ''
        tk = ev.get('ticker', '')
        if tk and ticker_key(tk) in tickers:
            t = tickers[ticker_key(tk)]
            if name_ratio(name, t['title']) >= NAME_BRIDGE_RATIO and self.valid_at(t['cik'], name, when):
                return t['cik'], 'sec_company_tickers_via_v05_ticker', 'high', ''
        cik, how = self.cik_by_name(core_name(name), when, lookup)
        if cik:
            return cik, how, 'medium', ''
        return None, '', '', how

    def identity(self, snaps: list[dict]) -> None:
        tickers = parse_company_tickers(json.loads(self.store.get(
            'https://www.sec.gov/files/company_tickers.json', 'reference/company_tickers.json', 'company_tickers',
            'SEC company_tickers.json (current tickers)').decode('utf-8')))
        lookup = parse_cik_lookup(self.store.get(
            'https://www.sec.gov/Archives/edgar/cik-lookup-data.txt', 'reference/cik-lookup-data.txt', 'cik_lookup',
            'SEC cik-lookup-data.txt (names to CIK)').decode('utf-8', errors='replace'))
        v05 = self.v05_securities()
        self.tickers, self.v05 = tickers, v05

        # 1. security key of every N-PORT equity line; placeholders never become keys
        first_date: dict[str, str] = {}
        sec_name: dict[str, dict] = {}
        for snap in snaps:
            if snap['form'] != 'NPORT-P':
                continue
            for ln in snap['lines']:
                cusip = valid_cusip(ln['cusip'])
                isin = valid_isin(ln['isin'])
                key, how = None, ''
                if cusip:
                    key, how = cusip_security(cusip), 'cusip'
                elif isin and us_isin_cusip(isin):
                    cusip = us_isin_cusip(isin)
                    key, how = cusip_security(cusip), 'us_isin_derived'
                elif isin and isin in self.v05_isin_to_cusip:
                    cusip = self.v05_isin_to_cusip[isin]
                    key, how = cusip_security(cusip), 'v05_isin_match'
                elif isin:
                    key, how = isin_security(isin), 'isin_only'
                ln['sec_key'], ln['sec_how'], ln['cusip_valid'], ln['isin_valid'] = key, how, cusip, isin
                if key:
                    first_date.setdefault(key, snap['report_date'])
                    rec = sec_name.setdefault(key, dict(names=[], titles=[], isin=isin, lei='', cusip=cusip, lines=[]))
                    rec['names'].append(ln['raw_name'])
                    rec['titles'].append(ln['title'])
                    rec['lines'].append(ln)
                    rec['lei'] = rec['lei'] or valid_lei(ln['lei'])
                    rec['isin'] = rec['isin'] or isin
                else:
                    self.flag('unidentified_security', ln['raw_name'], snap['snapshot_id'],
                              f'cusip {ln["cusip"] or "blank"!r}, isin {ln["isin"] or "blank"!r}; kept unmapped',
                              '')
        self.sec_name = sec_name

        # 2. issuer of every security key (period-validated)
        sec_map: dict[str, tuple] = {}
        for key in sorted(sec_name):
            rec = sec_name[key]
            cusip = rec['cusip']
            name = rec['names'][0]
            title = rec['titles'][0]
            when = first_date[key]
            if key.startswith('CUSIP:'):
                cik, meth, conf, why = self.security_issuer(cusip, name, title, when, lookup, tickers, v05)
            else:
                cik, meth, conf, why = self.cik_by_name(core_name(name), when, lookup)[0], 'isin_only_name_lookup', \
                    'medium', ''
                if cik is None:
                    why = 'isin_only_no_valid_cik'
            if cik is None:
                self.flag('nport_security_unmapped', key, '', f'{name}: {why}', '')
            sec_map[key] = (cik, meth, conf, why)
        self.sec_map = sec_map

        # 3. 2019-12-31 bridge: N-CSR rows against N-PORT lines with a valid security key
        bridge = next((s for s in snaps if s['form'] != 'NPORT-P' and s['report_date'] == BRIDGE_DATE), None)
        nport_1231 = next((s for s in snaps if s['form'] == 'NPORT-P' and s['report_date'] == BRIDGE_DATE), None)
        matched: dict[tuple[str, str], str] = {}
        bridge_stats = dict(ncsr_rows=0, matched=0, unmatched=0, ambiguous=0, nport_rows=0)
        ratios: list[float] = []
        if bridge and nport_1231:
            cands_all = [c for c in nport_1231['lines'] if c.get('sec_key')]
            bridge_stats['nport_rows'] = len(cands_all)
            used: set[str] = set()
            for ln in bridge['lines']:
                if ln['line_kind'] != 'stock':
                    continue
                bridge_stats['ncsr_rows'] += 1
                cands = [c for c in cands_all if c['sec_key'] not in used and c['cls'] == ln['cls']
                         and abs(c['shares'] - ln['shares']) <= BRIDGE_SHARE_TOL * ln['shares']]
                scored = sorted(((name_ratio(ln['raw_name'], c['raw_name']), c) for c in cands), key=lambda x: -x[0])
                if not scored or scored[0][0] < NAME_BRIDGE_RATIO:
                    bridge_stats['unmatched'] += 1
                    self.flag('bridge_unmatched', ln['raw_name'], bridge['snapshot_id'],
                              f'shares {ln["shares"]:.0f} class {ln["cls"]}: no N-PORT line with a valid key')
                    continue
                if len(scored) > 1 and scored[0][0] == scored[1][0]:
                    bridge_stats['ambiguous'] += 1
                    self.flag('bridge_ambiguous', ln['raw_name'], bridge['snapshot_id'], 'tie on name and shares')
                    continue
                key = scored[0][1]['sec_key']
                used.add(key)
                matched[(norm_core(ln['raw_name']), ln['cls'])] = key
                ratios.append(scored[0][1]['shares'] / ln['shares'])
                bridge_stats['matched'] += 1
        if ratios:
            ratios.sort()
            bridge_stats.update(share_ratio_min=round(ratios[0], 6), share_ratio_median=round(ratios[len(ratios) // 2], 6),
                                share_ratio_max=round(ratios[-1], 6))
        self.bridge_stats = bridge_stats
        self.step('bridge_2019_12_31', ' '.join(f'{k}={v}' for k, v in bridge_stats.items()))
        self.matched = matched

        # 4. pre-N-PORT lines: security (bridge, then CIK + class to the first N-PORT security, else CIKCLS) and issuer
        cluster_qe: dict[str, str] = {}
        for snap in snaps:
            if snap['form'] == 'NPORT-P':
                continue
            for ln in snap['lines']:
                key = norm_core(ln['raw_name'])
                cluster_qe[key] = min(cluster_qe.get(key, snap['report_date']), snap['report_date'])
        first_nport = next((s for s in snaps if s['form'] == 'NPORT-P'), None)
        cik_class: dict[tuple[str, str], list[str]] = {}
        if first_nport:
            for ln in first_nport['lines']:
                if ln.get('sec_key') in sec_map and sec_map[ln['sec_key']][0] and ln['sec_key'].startswith('CUSIP:'):
                    cik_class.setdefault((sec_map[ln['sec_key']][0], ln['cls']), []).append(ln['sec_key'])
        cluster_cik: dict[str, tuple] = {}
        for (key, cls), sk in sorted(matched.items()):
            if sec_map.get(sk, (None,))[0]:
                cluster_cik[key] = (sec_map[sk][0], f'bridge_2019_12_31->{sec_map[sk][1]}', sec_map[sk][2])
        for key in sorted(cluster_qe):
            if key in cluster_cik:
                continue
            cik, how = self.cik_by_name(key, cluster_qe[key], lookup)
            cluster_cik[key] = (cik, how, 'medium' if cik else '')
            if cik is None:
                self.flag('pre_name_unmapped', key, '', how)
        self.cluster_cik = cluster_cik
        self.step('name_clusters', f'{len(cluster_cik)} pre-N-PORT name clusters; '
                  f'{sum(1 for v in cluster_cik.values() if v[0])} with a CIK')
        for snap in snaps:
            if snap['form'] == 'NPORT-P':
                for ln in snap['lines']:
                    key = ln.get('sec_key')
                    if key and sec_map.get(key, (None,))[0]:
                        cik, meth, conf, why = sec_map[key]
                        ln['issuer'], ln['sec'], ln['map'], ln['why'] = cik10(cik), key, (meth, conf), ''
                    else:
                        ln['issuer'], ln['sec'], ln['map'] = None, None, ('', '')
                        ln['why'] = (sec_map[key][3] if key in sec_map else
                                     f'no valid identifier (cusip {ln["cusip"] or "blank"}, isin {ln["isin"] or "blank"})')
                continue
            for ln in snap['lines']:
                key = norm_core(ln['raw_name'])
                cik, how, conf = cluster_cik.get(key, (None, 'no_cluster', ''))
                ln['issuer'] = cik10(cik) if cik else None
                ln['why'] = '' if cik else how
                if not cik:
                    ln['sec'], ln['map'] = None, ('', '')
                    continue
                if (key, ln['cls']) in matched:
                    sk = matched[(key, ln['cls'])]
                    ln['sec'], ln['map'] = sk, (how + '|bridge_2019_12_31', 'high')
                elif (cik10(cik), ln['cls']) in cik_class and len(cik_class[(cik10(cik), ln['cls'])]) == 1:
                    ln['sec'], ln['map'] = cik_class[(cik10(cik), ln['cls'])][0], (how + '|cik_class_first_nport', 'medium')
                else:
                    ln['sec'], ln['map'] = cikcls_security(cik, ln['cls']), (how, conf or 'medium')
        self.first_nport_cik_class = cik_class

    # -- outputs ---------------------------------------------------------------------------------------------------
    def line_type(self, ln: dict, issuer_sic: str) -> tuple[str, str]:
        """(security_type, type_basis) of one line from evidence: an override, the OpenFIGI type text of the v05
        security, the holding's own title and name, the issuer SIC (REIT) and the SEC asset category."""
        sk = ln.get('sec') or ''
        if sk in self.type_over:
            ov = self.type_over[sk]
            return ov['value'], 'override: ' + ov['evidence']
        cusip = sk[6:] if sk.startswith('CUSIP:') else ''
        ev_type = self.v05.get(cusip, {}).get('openfigi_type', '') if cusip else ''
        return security_type(ln['raw_name'], ln.get('title', ''), issuer_sic, ev_type, ln.get('asset_cat', ''))

    def assemble(self) -> dict:
        trust_rows = self.trust_filings()
        self.step('trust_filings', f'{len(trust_rows)} filings listed for the trust')
        snaps = self.snapshots(trust_rows)
        by_quarter: dict[str, list[dict]] = {}
        for s in snaps:
            by_quarter.setdefault(s['report_date'], []).append(s)
        for s in snaps:
            s['is_primary'] = 0
        for rd in quarter_ends(PRE_START, LATEST_QUARTER):
            cands = [s for s in by_quarter.get(rd, []) if s['completeness'] == 'full']
            if not cands:
                self.flag('missing_quarter', rd, '', 'no complete 500 Index Fund schedule for this quarter')
                continue
            pick = max(cands, key=lambda s: (s['form'] == 'NPORT-P', s['filing_date'], s['accession']))
            pick['is_primary'] = 1
            if len(cands) > 1:
                self.flag('multiple_complete_snapshots', rd, pick['snapshot_id'],
                          f'{[c["snapshot_id"] for c in cands]}; primary chosen')
        for s in snaps:
            if not s['is_primary'] and s['completeness'] != 'full':
                self.flag('snapshot_not_full', s['accession'], s['snapshot_id'], s['completeness'])
        self.v05_reference(V05_SNAPSHOT_MEMBERS)
        self.v05_reference(V05_SNAPSHOTS)
        self.identity(snaps)
        for snap in snaps:
            for ln in snap['lines']:
                cik = ln.get('issuer')
                sub = self.issuer_sub(cik) if cik else None
                ln['type'], ln['type_basis'] = self.line_type(ln, sub['sic'] if sub else '')
                ln['weight'] = None
            denom = sum(ln['value_usd'] for ln in snap['lines'] if ln['type'] not in EXCLUDED_TYPES)
            snap['stock_total'] = denom
            for ln in snap['lines']:
                if ln['type'] not in EXCLUDED_TYPES and denom:
                    ln['weight'] = ln['value_usd'] / denom
        primary = [s for s in snaps if s['is_primary']]
        latest = primary[-1]
        latest_common = {ln['issuer'] for ln in latest['lines']
                         if ln.get('issuer') and ln['type'] in ('common', 'adr') and ln['line_kind'] == 'stock'}
        self.step('primary_quarters', f'{len(primary)} primary snapshots of {len(snaps)} snapshots')
        return self.tables(trust_rows, snaps, primary, latest_common)

    def tables(self, trust_rows, snaps, primary, latest_common) -> dict:
        latest_date = primary[-1]['report_date']
        status_as_of = max(r['retrieved_at_utc'] for r in self.store.records.values()
                           if r['kind'] == 'submissions')[:10]
        issuers_all = sorted({ln['issuer'] for s in snaps for ln in s['lines'] if ln.get('issuer')})
        universe = sorted({ln['issuer'] for s in primary for ln in s['lines']
                           if ln.get('issuer') and ln['type'] not in EXCLUDED_TYPES})
        info: dict[str, dict] = {}
        for cik in issuers_all:
            info[cik] = self.issuer_info(cik, snaps)
        prelim = {cik: classify(info[cik], cik in latest_common, status_as_of, latest_date) for cik in info}
        delisted = {cik: date.fromisoformat(prelim[cik]['delisting_date']) for cik in info
                    if prelim[cik]['status'] == 'delisted' and prelim[cik]['delisting_date'] and cik in universe}
        auto_links, succ_reviews = detect_successors({c: info[c] for c in universe}, delisted)
        self.review += succ_reviews
        over_links = []
        for r in self.overrides:
            if r['override_kind'] != 'link':
                continue
            pred, succ = (cik10(x) for x in r['key'].split('>'))
            over_links.append(dict(predecessor=pred, successor=succ, relation=r['value'], confidence='high',
                                   via_override=1, effective_date=r['field'],
                                   evidence=f'{r["reason"]} | {r["evidence"]}'))
        covered = {l['predecessor'] for l in over_links}
        links = over_links + [l for l in auto_links if l['predecessor'] not in covered]
        for l in links:
            for c in (l['predecessor'], l['successor']):
                if c not in info:
                    info[c] = self.issuer_info(c, snaps)
                    prelim[c] = classify(info[c], c in latest_common, status_as_of, latest_date)
        status_over = overrides_of(self.overrides, 'status')
        final = {}
        for cik in sorted(info):
            st = dict(prelim[cik])
            if cik in status_over:
                ov = status_over[cik]
                st.update(status=ov['value'], delisting_date=ov['field'], reason=ov.get('reason', 'unknown'),
                          method='override_status', evidence=ov['evidence'], needs_review=0)
            final[cik] = st
        for l in links:
            pred = final.get(l['predecessor'])
            if pred and pred['status'] in ('delisted', 'unknown'):
                pred.update(status='continued_by_successor', counterparty=issuer_id(l['successor']),
                            method=pred['method'] + '+successor_link',
                            needs_review=0 if l['via_override'] else int(l['confidence'] != 'high'))
            elif pred and pred['status'] in ('in_index', 'listed_out_of_index'):
                self.flag('link_predecessor_listed', l['predecessor'], '', f'link to {l["successor"]} but still listed')
        resolved = {}
        for cik in info:
            target, hops = cik, 0
            while hops < 3:
                nxt = next((l['successor'] for l in links if l['predecessor'] == target and l['relation'] in
                            ('successor_reorganization', 'merger_survivor')), None)
                if nxt is None:
                    break
                target, hops = nxt, hops + 1
            resolved[cik] = (target, 'successor_chain' if target != cik else 'self')
        for cik, st in final.items():
            if st['needs_review'] or st['status'] == 'unknown':
                self.flag('status_' + ('unknown' if st['status'] == 'unknown' else 'review'), issuer_id(cik), '',
                          st['evidence'] or st['method'])
        self.step('listing_status', ' '.join(f'{k}={sum(1 for v in final.values() if v["status"] == k)}' for k in
                                             ('in_index', 'listed_out_of_index', 'delisted', 'continued_by_successor',
                                              'unknown')) + f' review={sum(1 for v in final.values() if v["needs_review"])}')
        return self.build_rows(trust_rows, snaps, primary, info, final, links, resolved, universe, status_as_of)

    def issuer_info(self, cik: str, snaps: list[dict]) -> dict:
        sub = self.issuer_sub(cik)
        first = next((ln for s in snaps for ln in s['lines'] if ln.get('issuer') == cik), None)
        name = sub['name'] if sub else (core_name(first['raw_name']).strip() if first else '')
        return dict(cik=cik, name=name, former_names=[f['name'] for f in sub['former']] if sub else [],
                    former=sub['former'] if sub else [], exchanges=sub['exchanges'] if sub else [],
                    tickers=sub['tickers'] if sub else [], filings=sub['filings'] if sub else [],
                    sic=sub['sic'] if sub else '', sic_description=sub['sic_description'] if sub else '',
                    state=sub['state'] if sub else '', source_sha=sub['source_sha'] if sub else '')

    @staticmethod
    def spans(primary: list[dict], universe: list[str]) -> list[dict]:
        """Membership spans at issuer and security level. A subject that leaves and returns starts a new span. A
        quarter with no complete snapshot ends every span (membership unknown). Excluded types are not counted."""
        presence: dict[tuple[str, str, str], list[int]] = {}
        for i, s in enumerate(primary):
            for ln in s['lines']:
                if not ln.get('issuer') or ln.get('type') in EXCLUDED_TYPES or ln.get('line_kind') != 'stock':
                    continue
                presence.setdefault(('issuer', ln['issuer'], ln['issuer']), []).append(i)
                if ln.get('sec'):
                    presence.setdefault(('security', ln['sec'], ln['issuer']), []).append(i)
        rows = []
        for (level, subj, issuer), idxs in sorted(presence.items()):
            idxs = sorted(set(idxs))
            spans, cur = [], []
            for i in idxs:
                if cur and i == cur[-1] + 1 and _quarter_index(primary[i]['report_date']) == \
                        _quarter_index(primary[cur[-1]]['report_date']) + 1:
                    cur.append(i)
                else:
                    if cur:
                        spans.append(cur)
                    cur = [i]
            if cur:
                spans.append(cur)
            for n, sp in enumerate(spans, 1):
                first, last = primary[sp[0]], primary[sp[-1]]
                nxt = sp[-1] + 1
                absent = ''
                if nxt < len(primary) and _quarter_index(primary[nxt]['report_date']) == \
                        _quarter_index(last['report_date']) + 1:
                    absent = primary[nxt]['report_date']
                rows.append(dict(level=level, subject_id=subj, issuer_id=issuer_id(issuer), span_no=n,
                                 first_as_of=first['report_date'], last_as_of=last['report_date'],
                                 first_available_at_utc=first['acceptance'], last_available_at_utc=last['acceptance'],
                                 quarters_present=len(sp), next_absent_as_of=absent))
        return rows

    def build_rows(self, trust_rows, snaps, primary, info, final, links, resolved, universe, status_as_of) -> dict:
        """Every output table as a list of dicts. Ordering is fixed so the content hash is stable."""
        t = {k: [] for k in TABLE_COLUMNS}
        seen_src = {}
        for rec in sorted(self.store.records.values(), key=lambda r: r['path']):
            sid = source_id(rec['sha256'])
            if sid in seen_src:
                continue
            seen_src[sid] = rec
            t['sources'].append(dict(source_id=sid, kind=rec['kind'], url=rec['url'], raw_path=rec['path'],
                                     sha256=rec['sha256'], bytes=rec['bytes'], retrieved_at_utc=rec['retrieved_at_utc'],
                                     note=rec.get('note', '')))
        known_sources = set(seen_src)

        def sub_source(cik):
            sha = info[cik]['source_sha'] if cik in info else ''
            return source_id(sha) if sha else None

        # security facts: the first known value of each identifier, taken from every line that carries it
        sec_facts: dict[str, dict] = {}
        raw_of: dict[str, bytes] = {}
        for s in snaps:
            src = source_id(sha256_bytes(s['raw']))
            for ln in s['lines']:
                sk = ln.get('sec')
                if not sk:
                    continue
                raw_of.setdefault(sk, s['raw'])
                f = sec_facts.setdefault(sk, dict(issuer=ln['issuer'], cls=ln['cls'], name=ln['raw_name'], type=None,
                                                  basis='', isin='', lei='', source=src, derivation='',
                                                  scheme='CUSIP' if sk.startswith('CUSIP:') else
                                                  ('ISIN' if sk.startswith('ISIN:') else 'CIKCLS')))
                if f['type'] is None or (f['type'] == 'unknown' and ln['type'] != 'unknown'):
                    f['type'], f['basis'] = ln['type'], ln['type_basis']
                f['isin'] = f['isin'] or valid_isin(ln.get('isin'))
                f['lei'] = f['lei'] or valid_lei(ln.get('lei'))
                if ln.get('sec_how') and ln.get('sec_how') != 'cusip':
                    f['derivation'] = ln['sec_how']
        self.sec_facts = sec_facts
        for cik in sorted(info):
            d = info[cik]
            lei = next((sec_facts[sk]['lei'] for sk in sorted(sec_facts)
                        if sec_facts[sk]['issuer'] == cik and sec_facts[sk]['lei']), '')
            t['issuers'].append(dict(issuer_id=issuer_id(cik), cik=cik, name=d['name'], sic=d['sic'],
                                     sic_description=d['sic_description'], state_of_incorporation=d['state'], lei=lei,
                                     source_id=sub_source(cik)))
            st = final[cik]
            t['listing_status'].append(dict(issuer_id=issuer_id(cik), **{k: st[k] for k in
                                       ('status', 'status_as_of', 'delisting_date', 'reason', 'counterparty', 'evidence',
                                        'method', 'needs_review')}, source_id=sub_source(cik)))
            target, how = resolved[cik]
            t['issuer_resolution'].append(dict(issuer_id=issuer_id(cik), resolved_issuer_id=issuer_id(target), method=how))
            names: dict[tuple, dict] = {}
            if d['name']:
                names[(d['name'], 'sec_current', '')] = dict(name=d['name'], kind='sec_current', valid_from='',
                                                             valid_to='', source=sub_source(cik))
            for f in d['former']:
                if f['name']:
                    names[(f['name'], 'sec_former', f['valid_from'])] = dict(
                        name=f['name'], kind='sec_former', valid_from=f['valid_from'], valid_to=f['valid_to'],
                        source=sub_source(cik))
            for key, n in sorted(names.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2])):
                t['issuer_names'].append(dict(issuer_id=issuer_id(cik), name=n['name'], name_kind=n['kind'],
                                              valid_from=n['valid_from'], valid_to=n['valid_to'], first_seen='',
                                              last_seen='', source_id=n['source']))
        obs: dict[tuple[str, str], dict] = {}
        for s in snaps:
            src = source_id(sha256_bytes(s['raw']))
            for ln in s['lines']:
                if ln.get('issuer'):
                    o = obs.setdefault((ln['issuer'], ln['raw_name']),
                                       dict(first=s['report_date'], last=s['report_date'], src=src))
                    o['first'] = min(o['first'], s['report_date'])
                    o['last'] = max(o['last'], s['report_date'])
        for (cik, name), o in sorted(obs.items()):
            t['issuer_names'].append(dict(issuer_id=issuer_id(cik), name=name, name_kind='holdings_observed', valid_from='',
                                          valid_to='', first_seen=o['first'], last_seen=o['last'], source_id=o['src']))
        for l in sorted(links, key=lambda x: (x['predecessor'], x['successor'], x['relation'])):
            src = sub_source(l['successor']) or sub_source(l['predecessor'])
            t['issuer_links'].append(dict(predecessor_issuer_id=issuer_id(l['predecessor']),
                                          successor_issuer_id=issuer_id(l['successor']), relation=l['relation'],
                                          confidence=l['confidence'], via_override=l['via_override'],
                                          effective_date=l['effective_date'], evidence=l['evidence'], source_id=src))
        for sk, f in sorted(sec_facts.items()):
            cik = f['issuer']
            t['securities'].append(dict(security_id=sk, issuer_id=issuer_id(cik), class_code=f['cls'],
                                        security_type=f['type'] or 'unknown', type_basis=f['basis'], name=f['name'],
                                        id_scheme=f['scheme'], notes=f['derivation'], source_id=source_id(sha256_bytes(raw_of[sk]))))
            ids = []
            if sk.startswith('CUSIP:'):
                ids.append(('cusip', sk[6:]))
            if f['isin']:
                ids.append(('isin', f['isin']))
            if f['lei']:
                ids.append(('lei', f['lei']))
            v05row = self.v05.get(sk[6:], {}) if sk.startswith('CUSIP:') else {}
            if v05row.get('ticker'):
                ids.append(('ticker', v05row['ticker']))
            else:
                tks = [k for k, v in self.tickers.items() if v['cik'] == cik]
                if len(tks) > 1:   # warrant and right lines of the same issuer are not its share code
                    tks = [k for k in tks if not re.search(r'(WS|WT|W|R|U)$', k) or len(k) <= 4]
                if len(tks) == 1 and f['cls'] == 'COM' and f['type'] in ('common', 'adr'):
                    ids.append(('ticker', tks[0]))
            seen_ids = set()
            for id_type, value in ids:
                if value and (id_type, value) not in seen_ids:
                    seen_ids.add((id_type, value))
                    t['security_identifiers'].append(dict(security_id=sk, id_type=id_type, id_value=value, valid_from='',
                                                          valid_to='', source_id=source_id(sha256_bytes(raw_of[sk]))))
        for s in snaps:
            src = source_id(sha256_bytes(s['raw']))
            total = s['total']
            stock_total = s.get('stock_total', 0.0)
            t['snapshots'].append(dict(
                snapshot_id=s['snapshot_id'], fund_series=S.FUND_SERIES, form=s['form'], accession=s['accession'],
                primary_doc=s['primary_doc'], holdings_as_of=s['report_date'], filed_date=s['filing_date'],
                available_at_utc=s['acceptance'], is_primary=s['is_primary'], completeness=s['completeness'],
                row_count=sum(1 for ln in s['lines'] if ln['line_kind'] == 'stock'), excluded_rows=s['excluded_rows'],
                excluded_value_usd=s['excluded_value'], total_investments_reported_usd=total,
                reported_common_stock_total_usd=s['reported_common'], stock_total_usd=stock_total,
                coverage_ratio=(stock_total / total) if total else None, reconciled=s['reconciled'],
                start_rule=s['start_rule'], source_id=src, notes=s['notes']))
            for n, ln in enumerate(s['lines'], 1):
                cik = ln.get('issuer')
                t['holdings'].append(dict(
                    snapshot_id=s['snapshot_id'], line_no=n, line_kind=ln['line_kind'], raw_name=ln['raw_name'],
                    raw_class_text=ln['raw_class_text'], cusip_reported=ln.get('cusip', ''), isin_reported=ln.get('isin', ''),
                    lei_reported=ln.get('lei', ''), title_reported=ln.get('title', ''), shares=ln['shares'],
                    value_usd=ln['value_usd'], pct_val_percent=ln.get('pct_val'), weight=ln.get('weight'),
                    security_id=ln.get('sec') if cik else None, issuer_id=issuer_id(cik) if cik else None,
                    map_method=ln.get('map', ('', ''))[0], map_confidence=ln.get('map', ('', ''))[1],
                    unmapped_reason='' if cik else ln.get('why', ''), source_id=src))
        t['membership_spans'] = self.spans(primary, universe)
        for n, r in enumerate(sorted(self.review, key=lambda r: (r['review_type'], r.get('subject_id', ''),
                                                                 r.get('snapshot_id', ''), r.get('detail', ''))), 1):
            src = r.get('source_id') or None
            t['review_items'].append(dict(review_id=n, review_type=r['review_type'], subject_id=r.get('subject_id', ''),
                                          snapshot_id=r.get('snapshot_id', ''), detail=r.get('detail', ''),
                                          source_id=src if src in known_sources else None))
        t['_extra'] = dict(bridge=self.bridge_stats, universe=len(universe), trust_filings=len(trust_rows),
                           requests=self.store.stats.requests, retries=self.store.stats.retries,
                           failures=self.store.stats.failures, status_as_of=status_as_of, steps=self.steps)
        return t


def valid_lei(text: str | None) -> str:
    s = (text or '').strip().upper()
    return s if re.fullmatch(r'[0-9A-Z]{20}', s) else ''


def _class_text(text: str) -> str:
    m = CLASS_LETTER.search(text or '')
    return f'Class {m.group(1)}' if m else ''


def content_hash(conn: sqlite3.Connection) -> str:
    """sha256 over every table and view (meta and build_log excluded), rows ordered by all columns."""
    h = hashlib.sha256()
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT IN ('meta','build_log') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for name in names:
        ncol = len(conn.execute(f'PRAGMA table_info("{name}")').fetchall())
        order = ','.join(str(i) for i in range(1, ncol + 1))
        h.update(name.encode('utf-8') + b'\n')
        for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY {order}'):
            h.update(json.dumps(row, ensure_ascii=False).encode('utf-8') + b'\n')
    return h.hexdigest()


def write_db(tables: dict, path: Path, steps: list, meta: dict) -> str:
    """Write the database with foreign keys enforced. Parents are inserted before children (TABLE_COLUMNS order);
    any foreign-key violation aborts the write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.building')
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.executescript(SCHEMA_FILE.read_text(encoding='utf-8'))
        for name, cols in TABLE_COLUMNS.items():
            rows = tables.get(name, [])
            if not rows:
                continue
            sql = f'INSERT INTO {name} ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})'
            conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        conn.executemany('INSERT INTO build_log (step_no, ts_utc, step, detail) VALUES (?,?,?,?)',
                         [(i, utc_now_iso(), s, d) for i, (s, d) in enumerate(steps, 1)])
        conn.commit()
        bad = conn.execute('PRAGMA foreign_key_check').fetchall()
        if bad:
            raise RuntimeError(f'foreign key violations: {len(bad)}; first {bad[:3]}')
        digest = content_hash(conn)
        conn.executemany('INSERT INTO meta (key, value) VALUES (?,?)',
                         [('content_sha256', digest), ('schema_version', str(SCHEMA_VERSION)),
                          ('built_at_utc', utc_now_iso())] + [(k, str(v)) for k, v in sorted(meta.items())])
        conn.commit()
    finally:
        conn.close()
    path.unlink(missing_ok=True)
    tmp.replace(path)
    return digest


def build(offline: bool = False, log=print, db_path: Path = S.DB_PATH) -> dict:
    """Build the database. Returns the content hash and the network counters of this run."""
    store = RawStore(user_agent=None if offline else S.load_user_agent(), offline=offline)
    b = Build(store, log=log)
    t = b.assemble()
    extra = t.pop('_extra')
    manifest = S.RAW_DIR / 'manifest.jsonl'
    manifest_sha = sha256_bytes(manifest.read_bytes()) if manifest.exists() else ''
    commit = _code_commit()
    build_id = sha256_bytes(f'{manifest_sha}|{commit}|{utc_now_iso()}'.encode())[:16]
    digest = write_db(t, db_path, b.steps + [('write', db_path.name)],
                      dict(build_id=build_id, code_commit=commit, raw_manifest_sha256=manifest_sha,
                           content_sha256_scope='all tables and views except meta and build_log'))
    counts = {k: len(v) for k, v in t.items() if isinstance(v, list)}
    return dict(content_sha256=digest, network_requests=store.stats.requests, retries=store.stats.retries,
                failures=store.stats.failures, offline=offline, counts=counts, extra=extra, build_id=build_id,
                code_commit=commit, raw_manifest_sha256=manifest_sha)
