"""Assemble leadtrend_db v2 from the raw archive and write the SQLite database.

Each input is read through RawStore: an archived file is read from disk (sha256 verified); a missing one is fetched
from SEC once and archived. offline=True forbids fetching. The database is a function of the archive,
config/db_overrides_v1.csv and rules.py only. The content hash leaves out timestamps and build metadata, so two
builds from the same archive hash identically.

Identity of a holding line, on the line's own holding date (no placeholder is ever a key):
  security : valid CUSIP -> US ISIN's CUSIP -> v05 security whose evidence carries the ISIN -> ISIN -> none
  issuer   : dated override -> v05 SEC-corroborated CIK, if that CIK has periodic filings near the date (the CUSIP
             evidence is not overruled by a name difference) -> SEC ticker of the v05 security, name checked ->
             SEC name lookup, each candidate valid on the date (name in force and a periodic filing near it).
             A line that cannot be placed keeps its candidate CIKs for the bound check (top200.py).
  2019-12-31: the N-CSR rows are matched to N-PORT lines that have a valid security key (shares within 1%, same
             class, similar name). Earlier N-CSR lines carry the security of their issuer and class.
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

from . import rules as R
from . import sources as S
from .identity import (CLASS_LETTER, EXCLUDED_TYPES, NON_STOCK_TYPES, ELIGIBLE_TYPES, cik10, cikcls_security,
                       class_code_from_text, core_name, cusip_security, isin_security, issuer_id, load_overrides,
                       name_key, name_ratio, name_signature, norm_core, overrides_of, parse_cik_lookup,
                       parse_company_tickers, security_type, ticker_key, us_isin_cusip, v05_evidence_fields,
                       valid_cusip, valid_isin, valid_ticker, window_of)
from .parse_ncsr import parse_500_schedule
from .parse_nport import holdings_lines, screen
from .sources import RawMissing, RawStore, sha256_bytes, utc_now_iso
from .status import PERIODIC, classify, detect_successors, filing_rows

SCHEMA_FILE = Path(__file__).with_name('schema.sql')
SCHEMA_VERSION = 2
PRE_FORMS = ('N-Q', 'N-CSR', 'N-CSRS', 'NPORT-EX')
KIND = {'N-Q': 'nq', 'N-CSR': 'ncsr', 'N-CSRS': 'ncsrs', 'NPORT-EX': 'other', 'NPORT-P': 'nport'}
SNAP_PREFIX = {'N-Q': 'NQ', 'N-CSR': 'NCSR', 'N-CSRS': 'NCSRS', 'NPORT-EX': 'NPORTEX', 'NPORT-P': 'NPORT'}
PRE_START, BRIDGE_DATE, NPORT_START = '2013-12-31', '2019-12-31', '2019-09-30'
FILED_FROM = '2014-01-01'
MIN_FULL_ROWS = 400             # a 500 Index Fund schedule has about 500 equity lines
COVERAGE_FULL = 0.95            # N-PORT: stock total must be at least this share of total investments
NAME_BRIDGE_RATIO = 0.5
BRIDGE_SHARE_TOL = 0.01         # relative share-count tolerance for the 2019-12-31 match (sources differ by ~0.3%)
CIK_PERIOD_DAYS = 95            # a periodic filing this close to the holding date supports a CIK
NAME_LAG_DAYS = 45              # a former SEC name still appears in holdings reported this soon after its end date
FUZZY_RATIO = 0.9               # bounding only: lookup names this similar to an unplaced name are its possible issuers
ARCHIVE = 'https://www.sec.gov/Archives/edgar/data/36405'
SUBMISSIONS = 'https://data.sec.gov/submissions'
V05_SECURITIES = 'securities.csv'
V05_SNAPSHOT_MEMBERS = 'snapshot_members.csv'
V05_SNAPSHOTS = 'snapshots.csv'
# security type precedence when lines of one security disagree: an explicit exclusion wins over common
TYPE_ORDER = ('cvr', 'right', 'warrant', 'etf', 'reit', 'partnership', 'spac', 'preferred', 'other', 'adr', 'common',
              'unknown')
TABLE_COLUMNS = {
    'sources': ['source_id', 'kind', 'url', 'raw_path', 'sha256', 'bytes', 'retrieved_at_utc', 'note'],
    'issuers': ['issuer_id', 'cik', 'name', 'sic', 'sic_description', 'state_of_incorporation', 'lei', 'source_id'],
    'issuer_names': ['issuer_id', 'name', 'name_kind', 'valid_from', 'valid_to', 'first_seen', 'last_seen', 'source_id'],
    'issuer_links': ['predecessor_issuer_id', 'successor_issuer_id', 'relation', 'confidence', 'via_override',
                     'effective_date', 'evidence', 'source_id'],
    'securities': ['security_id', 'issuer_id', 'class_code', 'security_type', 'type_basis', 'name', 'id_scheme',
                   'notes', 'source_id'],
    'security_identifiers': ['security_id', 'id_type', 'id_value', 'valid_from', 'valid_to', 'source_id'],
    'security_issuer_spans': ['security_id', 'issuer_id', 'span_no', 'first_as_of', 'last_as_of', 'lines', 'method',
                              'source_id'],
    'snapshots': ['snapshot_id', 'fund_series', 'form', 'accession', 'primary_doc', 'holdings_as_of', 'filed_date',
                  'available_at_utc', 'is_primary', 'completeness', 'row_count', 'excluded_rows', 'excluded_value_usd',
                  'total_investments_reported_usd', 'reported_common_stock_total_usd', 'stock_total_usd',
                  'coverage_ratio', 'reconciled', 'residual_usd', 'rounding_bound_usd', 'unparsed_rows',
                  'start_rule', 'source_id', 'notes'],
    'holdings': ['snapshot_id', 'line_no', 'line_kind', 'raw_name', 'raw_class_text', 'cusip_reported', 'isin_reported',
                 'lei_reported', 'title_reported', 'shares', 'value_usd', 'pct_val_percent', 'fund_weight_raw',
                 'stock_weight', 'security_id', 'issuer_id', 'security_type', 'map_method', 'map_confidence',
                 'unmapped_reason', 'candidate_issuers', 'source_id'],
    'pool_versions': ['version_id', 'report_period', 'snapshot_id', 'form', 'accession', 'filed_date',
                      'available_at_utc', 'completeness', 'reconciled', 'is_canonical', 'supersedes_version_id',
                      'version_note', 'rule_version', 'quarter_state', 'quarter_reason', 'source_id'],
    'company_ranks': ['version_id', 'rank', 'issuer_id', 'amount_cents', 'eligible_lines', 'tie_group',
                      'in_top200', 'boundary_status', 'unresolved_lines', 'unresolved_cents', 'candidate_bound_cents'],
    'top200_companies': ['version_id', 'rank', 'issuer_id', 'issuer_name_at_period', 'amount_cents', 'fund_weight_raw',
                         'tie_group', 'eligible_securities', 'blocked_members', 'rule_version'],
    'top200_securities': ['version_id', 'issuer_id', 'security_id', 'class_code', 'security_type', 'ticker_at_period',
                          'holding_lines', 'amount_cents', 'fund_weight_raw', 'candidate_eligible', 'candidate_reason'],
    'strategy_companies': ['version_id', 'issuer_id', 'historical_rank', 'status_cutoff', 'current_status',
                           'resolved_current_issuer', 'current_ticker', 'strategy_eligible', 'blocked_reason',
                           'excluded_reason'],
    'strategy_securities': ['version_id', 'issuer_id', 'security_id', 'class_code', 'current_security_id',
                            'current_ticker', 'strategy_eligible', 'reason'],
    'quarter_checks': ['version_id', 'check_name', 'status', 'detail'],
    'membership_spans': ['level', 'subject_id', 'issuer_id', 'span_no', 'first_as_of', 'last_as_of',
                         'first_available_at_utc', 'last_available_at_utc', 'quarters_present', 'next_absent_as_of'],
    'issuer_resolution': ['issuer_id', 'resolved_issuer_id', 'method'],
    'listing_status': ['issuer_id', 'status', 'status_as_of', 'delisting_date', 'reason', 'counterparty', 'evidence',
                       'method', 'needs_review', 'source_id'],
    'review_items': ['review_id', 'review_type', 'subject_id', 'snapshot_id', 'detail', 'source_id'],
}


def source_id(sha256: str) -> str:
    return 'SRC-' + sha256[:16]


def _words(text: str) -> str:
    """Name as written: upper case, punctuation removed, legal form kept as words."""
    return ' '.join(re.sub(r'[^A-Z0-9]+', ' ', (text or '').upper().replace('&', ' AND ')).split())


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


def _day10(text: str) -> str:
    return (text or '')[:10]


def _quarter_index(d: str) -> int:
    return int(d[:4]) * 4 + (int(d[5:7]) - 1) // 3


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
        self.valid_cache: dict[tuple, bool] = {}
        self.resolve_cache: dict[tuple, dict] = {}
        self.lookup: dict[str, set[str]] = {}
        self.lookup_sig: dict[str, set[str]] = {}
        self.overrides = load_overrides(S.OVERRIDES_PATH)
        self.cusip_over = overrides_of(self.overrides, 'cusip_cik')
        self.name_over = overrides_of(self.overrides, 'name_cik')
        self.type_over = overrides_of(self.overrides, 'security_type')
        self.end_over = overrides_of(self.overrides, 'security_end')
        self.ticker_over = overrides_of(self.overrides, 'security_ticker')
        # a predecessor of a dated link stops being the issuer of new holdings on the link's effective date
        self.ended = {cik10(r['key'].split('>')[0]): r['field'] for r in self.overrides
                      if r['override_kind'] == 'link' and r['field']}
        self.succ_over = {cik10(r['key'].split('>')[1]): r['field'] for r in self.overrides
                          if r['override_kind'] == 'link' and r['field']}

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
            if r['accession'] in seen:
                continue
            seen.add(r['accession'])
            if _ts(r['acceptance']) <= R.BUILD_CUTOFF_UTC:      # only filings public by the fixed build cutoff
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
        """True if a SEC name of the issuer with the same signature was in force on the date. A former name is in
        force between its own dates. The current name is in force from the end of the last former name that has the
        same signature (the name it replaced); formerNames of other entities never move that date."""
        sub = self.issuer_sub(cik)
        if sub is None:
            return False
        target = name_signature(core_name(name))
        for f in sub['former']:
            # a holding report can still carry the name a few days after the SEC change (NAME_LAG_DAYS)
            lag_end = (date.fromisoformat(f['valid_to']) + timedelta(days=NAME_LAG_DAYS)).isoformat() if f['valid_to'] else ''
            if name_signature(core_name(f['name'])) == target and (not f['valid_from'] or f['valid_from'] <= when) and \
                    (not lag_end or when <= lag_end):
                return True
        if name_signature(core_name(sub['name'])) != target:
            return False
        same = [f['valid_to'] for f in sub['former'] if f['valid_to'] and
                name_signature(core_name(f['name'])) == name_signature(core_name(sub['name']))]
        return not same or when >= max(same)

    def periodic_near(self, cik: str, when: str) -> bool:
        """The issuer filed a periodic report near the date (existence evidence on the holding date)."""
        key = ('periodic', cik, when)
        if key in self.valid_cache:
            return self.valid_cache[key]
        sub = self.issuer_sub(cik)
        ok = False
        if sub is not None:
            d0 = date.fromisoformat(when)
            for f in sub['filings']:
                if f['form'] not in PERIODIC:
                    continue
                if f['report_date'] and abs((date.fromisoformat(f['report_date']) - d0).days) <= CIK_PERIOD_DAYS:
                    ok = True
                    break
                if d0 <= date.fromisoformat(f['filing_date']) <= d0 + timedelta(days=120):
                    ok = True
                    break
        self.valid_cache[key] = ok
        return ok

    def succession_date(self, cik: str) -> str | None:
        """Effective date of the issuer as a successor registrant. A link override with this CIK as successor gives
        the date. Otherwise the earliest Form 8-K12B of the CIK counts only when the CIK had no periodic report in the
        year before it (a successor registrant has not been reporting as itself; an 8-K12B filed by an operating
        company that already reports, such as Cisco 2021, is not a succession). Before the date the entity was not
        the issuer of the shares; its holdings belong to the predecessor."""
        if cik in self.succ_over:
            return self.succ_over[cik]
        sub = self.issuer_sub(cik)
        if sub is None:
            return None
        for d8 in sorted(f['filing_date'] for f in sub['filings'] if f['form'] == '8-K12B'):
            d0 = date.fromisoformat(d8)
            reporting = any(f['form'] in PERIODIC and d0 - timedelta(days=365) <= date.fromisoformat(f['filing_date']) < d0
                            for f in sub['filings'])
            if not reporting:
                return d8
        return None

    def exists_at(self, cik: str, when: str) -> bool:
        """The CIK is the issuer of shares on the date: it filed periodic reports near the date, it is not before its
        successor effective date, and it is not a predecessor whose link took effect on or before the date."""
        key = ('exists', cik, when)
        if key not in self.valid_cache:
            ok = self.periodic_near(cik, when)
            succ = self.succession_date(cik)
            if succ and when < succ:
                ok = False
            if cik in self.ended and when >= self.ended[cik]:
                ok = False
            self.valid_cache[key] = ok
        return self.valid_cache[key]

    def valid_at(self, cik: str, name: str, when: str) -> bool:
        key = ('name', cik, name_signature(core_name(name)), when)
        if key not in self.valid_cache:
            self.valid_cache[key] = self.name_valid_at(cik, name, when) and self.exists_at(cik, when)
        return self.valid_cache[key]

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
            if r['decision'] == 'target' and r['accession'] in {t['accession'] for t in trust_rows}:
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
                            reconciled=1, start_rule='nport_xml', notes='', url=url, residual=None, bound=None,
                            unparsed=0))
        return out

    # -- snapshots -----------------------------------------------------------------------------------------------
    def snapshots(self, trust_rows: list[dict]) -> list[dict]:
        screen_rows = self.nport_screen(trust_rows)
        snaps = []
        public = {r['accession'] for r in trust_rows}
        for p in self.pre_snapshots(trust_rows):
            if p['accession'] not in public:
                continue
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
                              notes='; '.join(notes), url=p['url'], residual=res.get('residual_usd'),
                              bound=res.get('rounding_bound_usd'), unparsed=res.get('unparsed_rows', 0)))
        snaps += self.nport_snapshots(trust_rows, screen_rows)
        snaps.sort(key=lambda s: (s['report_date'], s['form'], s['accession']))
        return snaps

    # -- identity ------------------------------------------------------------------------------------------------
    def v05_securities(self) -> dict[str, dict]:
        """v05 securities keyed by CUSIP: the ISIN, the SEC-corroborated CIK, the ticker, the OpenFIGI type text and
        the v05 security type."""
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
                              isin=isin, openfigi_type=ev.get('openfigi_type', ''), v05_type=r.get('security_type', ''),
                              name=r.get('original_name', '') or ev.get('openfigi', ''))
        return out

    def v05_alias_map(self) -> dict[str, str]:
        """v05 security aliases (prep/stage2_security_aliases.csv): an old identifier and the canonical security it was
        merged into (an identifier succession confirmed in v05). Kept as v05 has it; never split again."""
        src = S.ROOT / 'data' / 'v05' / 'prep' / 'stage2_security_aliases.csv'
        data = self.store.import_local('local:data/v05/prep/stage2_security_aliases.csv',
                                       'reference/v05/stage2_security_aliases.csv', 'other', src,
                                       'copied from data/v05/prep/stage2_security_aliases.csv')
        return {r['old_security_id']: r['security_id'] for r in csv.DictReader(io.StringIO(data.decode('utf-8')))}

    def v05_reference(self, name: str) -> bytes:
        """Archived copy of a v05 normalized table: the build reads the copy, not data/v05."""
        return self.store.import_local(f'local:data/v05/normalized/{name}', f'reference/v05/{name}', 'other',
                                       S.V05_NORMALIZED / name, f'copied from data/v05/normalized/{name}')

    def name_candidates(self, name: str) -> set[str]:
        """SEC lookup CIKs for a holding name: the same name key, else the same words in another order ('CR Bard' and
        'BARD C R' have one signature). Only candidates; each still has to be valid on the holding date."""
        key = name_key(core_name(name))
        found = set(self.lookup.get(key, set()))
        if not found and key:
            found = set(self.lookup_sig.get(''.join(sorted(key.split())), set()))
        return found

    def issuer_by_name(self, name: str, when: str) -> dict:
        """CIK of a holding name on a date by the SEC name lookup. Every candidate must be valid on that date."""
        key = ('name', name_key(core_name(name)), when)
        if key in self.resolve_cache:
            return self.resolve_cache[key]
        out = dict(cik=None, method='', conf='', why='', cands=set())
        override = self.name_over.get(name_key(name))
        if override and self._window_ok(override, when):
            out.update(cik=cik10(override['value']), method='override_name_cik', conf='high')
        else:
            cands = self.name_candidates(core_name(name))
            out['cands'] = set(cands)
            if not cands:
                out.update(method='no_cik_candidate', why='no_cik_candidate',
                           cands=self.fuzzy_candidates(name))
            else:
                valid = sorted(c for c in cands if self.valid_at(c, name, when))
                if not valid:
                    out.update(method=f'no_candidate_valid_at_date:{len(cands)}', why=f'no_candidate_valid_at_date:{len(cands)}')
                elif len(valid) == 1:
                    out.update(cik=valid[0], conf='medium', method='cik_lookup_name_valid_at_date' if len(cands) > 1
                               else 'cik_lookup_name_valid_at_date_single')
                else:
                    exact = [c for c in valid if self._sec_name_equal(c, name)]
                    if len(exact) != 1:      # legal form decides when the words agree: 'Actavis plc' vs 'Actavis, Inc.'
                        full = [c for c in valid if self._sec_name_equal(c, name, full=True)]
                        exact = full if len(full) == 1 else exact
                    if len(exact) == 1:
                        out.update(cik=exact[0], conf='medium', method='cik_lookup_name_exact_sec_name')
                    else:
                        out.update(cands=set(valid), method=f'ambiguous_candidates:{len(cands)}->{len(valid)}',
                                   why=f'ambiguous_candidates:{len(cands)}->{len(valid)}')
        self.resolve_cache[key] = out
        return out

    def _sec_name_equal(self, cik: str, name: str, full: bool = False) -> bool:
        """An SEC name of the issuer equals the holding name: by name signature, or with full=True by the words as
        written (legal form and punctuation removed but kept as words)."""
        sub = self.issuer_sub(cik)
        if not sub:
            return False
        if full:
            target = _words(core_name(name))
            return any(_words(n) == target for n in [sub['name']] + [f['name'] for f in sub['former']])
        target = name_signature(core_name(name))
        return any(name_signature(core_name(n)) == target for n in
                   [sub['name']] + [f['name'] for f in sub['former']])

    def fuzzy_candidates(self, name: str) -> set[str]:
        """Bounding candidates only (never an identity): SEC lookup names that are very similar to a name the lookup
        does not hold. Used so that an unplaced line is bounded by real entities, not by an unknown company."""
        import difflib
        if not hasattr(self, '_first_token_index'):
            idx: dict[str, list[str]] = {}
            for key in self.lookup:
                first = key.split(' ', 1)[0] if key else ''
                idx.setdefault(first, []).append(key)
            self._first_token_index = idx
        key = name_key(core_name(name))
        first = key.split(' ', 1)[0] if key else ''
        out = set()
        for k in self._first_token_index.get(first, []):
            if difflib.SequenceMatcher(None, key, k).quick_ratio() >= 0.9 and \
                    difflib.SequenceMatcher(None, key, k).ratio() >= FUZZY_RATIO:
                out |= self.lookup[k]
        return out

    @staticmethod
    def _window_ok(override: dict, when: str) -> bool:
        lo, hi = window_of(override.get('field', ''))
        return (not lo or lo <= when) and (not hi or when <= hi)

    def issuer_for_cusip(self, cusip: str, name: str, title: str, when: str, v05row: dict) -> dict:
        """Issuer of a CUSIP-keyed line on its date: a dated override, then the v05 SEC-corroborated CIK (kept when
        that issuer existed on the date), then the v05 ticker, then the name lookup."""
        key = ('cusip', cusip, when, name_key(core_name(name)))
        if key in self.resolve_cache:
            return self.resolve_cache[key]
        out = dict(cik=None, method='', conf='', why='', cands=set())
        over = self.cusip_over.get(cusip)
        if over and self._window_ok(over, when):
            out.update(cik=cik10(over['value']), method='override_cusip_cik', conf='high')
        else:
            ev = v05row.get('evidence_cik', '')
            if ev and self.exists_at(ev, when):
                out.update(cik=ev, method='v05_source_evidence_cik', conf='high')
            else:
                tk = v05row.get('ticker', '')
                t = self.tickers.get(ticker_key(tk)) if tk else None
                if t and self.exists_at(t['cik'], when) and name_ratio(name, t['title']) >= NAME_BRIDGE_RATIO:
                    out.update(cik=t['cik'], method='sec_company_tickers_via_v05_ticker', conf='high')
                else:
                    res = self.issuer_by_name(name, when)
                    out.update(cik=res['cik'], method=res['method'], conf=res['conf'], why=res['why'],
                               cands=res['cands'])
                    if ev:
                        out['cands'] = set(out['cands']) | {ev}
                    if t:
                        out['cands'] = set(out['cands']) | {t['cik']}
        if out['cik'] is None and not out['cands']:
            out['cands'] = self.name_candidates(name)
        self.resolve_cache[key] = out
        return out

    def identity(self, snaps: list[dict]) -> None:
        tickers = parse_company_tickers(json.loads(self.store.get(
            'https://www.sec.gov/files/company_tickers.json', 'reference/company_tickers.json', 'company_tickers',
            'SEC company_tickers.json (current tickers)').decode('utf-8')))
        self.lookup = parse_cik_lookup(self.store.get(
            'https://www.sec.gov/Archives/edgar/cik-lookup-data.txt', 'reference/cik-lookup-data.txt', 'cik_lookup',
            'SEC cik-lookup-data.txt (names to CIK)').decode('utf-8', errors='replace'))
        self.lookup_sig: dict[str, set[str]] = {}
        for key, ciks in self.lookup.items():
            self.lookup_sig.setdefault(''.join(sorted(key.split())), set()).update(ciks)
        v05 = self.v05_securities()
        self.tickers, self.v05 = tickers, v05
        self.alias = self.v05_alias_map()

        # 1. security key of every N-PORT equity line; placeholders never become keys
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
                if key in self.alias:       # v05's confirmed merge: the old identifier is the same security as the canonical
                    key, how = self.alias[key], how + '+v05_alias'
                    cusip = key[6:] if key.startswith('CUSIP:') else cusip
                ln['sec_key'], ln['sec_how'] = key, how
                ln['cusip_valid'], ln['isin_valid'] = cusip, isin
        # 2. issuer of every N-PORT line on its own holding date
        for snap in snaps:
            if snap['form'] != 'NPORT-P':
                continue
            for ln in snap['lines']:
                self.resolve_line(ln, snap['report_date'], snap['snapshot_id'])

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
                matched[(_words(core_name(ln['raw_name'])), ln['cls'])] = key
                ratios.append(scored[0][1]['shares'] / ln['shares'])
                bridge_stats['matched'] += 1
        if ratios:
            ratios.sort()
            bridge_stats.update(share_ratio_min=round(ratios[0], 6), share_ratio_median=round(ratios[len(ratios) // 2], 6),
                                share_ratio_max=round(ratios[-1], 6))
        self.bridge_stats = bridge_stats
        self.step('bridge_2019_12_31', ' '.join(f'{k}={v}' for k, v in bridge_stats.items()))
        self.matched = matched

        # 4. pre-N-PORT lines: the issuer on the line's own date by name; the security from the bridge, else the first
        #    N-PORT CUSIP of the same issuer and class (only when unique), else the issuer's CIK-class key
        first_nport = next((s for s in snaps if s['form'] == 'NPORT-P'), None)
        cik_class: dict[tuple[str, str], list[str]] = {}
        if first_nport:
            for ln in first_nport['lines']:
                if ln.get('issuer') and ln.get('sec_key', '') and ln['sec_key'].startswith('CUSIP:'):
                    cik_class.setdefault((ln['issuer'], ln['cls']), []).append(ln['sec_key'])
        self.first_nport_cik_class = cik_class
        for snap in snaps:
            if snap['form'] == 'NPORT-P':
                continue
            for ln in snap['lines']:
                key = _words(core_name(ln['raw_name']))
                if (key, ln['cls']) in matched:       # the same name and class as a CUSIP security of 2019-12-31
                    sk = matched[(key, ln['cls'])]
                    ln['sec_key'], ln['sec_how'] = sk, 'bridge_2019_12_31'
                    ln['cusip_valid'] = sk[6:] if sk.startswith('CUSIP:') else ''
                    ln['isin_valid'] = ''
                    res = self.issuer_for_cusip(sk[6:], ln['raw_name'], '', snap['report_date'],
                                                self.v05.get(sk[6:], {}))
                    ln['issuer'], ln['map'], ln['why'], ln['cands'] = res['cik'], (res['method'], res['conf'] or 'medium'), \
                        res['why'], res['cands']
                    continue
                res = self.issuer_by_name(core_name(ln['raw_name']), snap['report_date'])
                ln['issuer'], ln['map'], ln['why'], ln['cands'] = res['cik'], (res['method'], res['conf'] or 'medium'), \
                    res['why'], res['cands']
                ln['sec_key'], ln['sec_how'] = None, ''
                ln['cusip_valid'], ln['isin_valid'] = '', ''
                if res['cik'] is None:
                    continue
                if (res['cik'], ln['cls']) in cik_class and len(set(cik_class[(res['cik'], ln['cls'])])) == 1:
                    ln['sec_key'] = cik_class[(res['cik'], ln['cls'])][0]
                    ln['sec_how'] = 'cik_class_first_nport'
                else:
                    ln['sec_key'] = cikcls_security(res['cik'], ln['cls'])
                    ln['sec_how'] = 'cikcls'
        self.propagate_cusip(snaps)
        self.step('identity', f'{sum(1 for s in snaps for ln in s["lines"] if ln.get("issuer"))} lines with an issuer; '
                  f'{sum(1 for s in snaps for ln in s["lines"] if not ln.get("issuer"))} without')

    def propagate_cusip(self, snaps: list[dict]) -> None:
        """An unresolved CUSIP line takes the issuer that the same security (CUSIP) has on the nearest resolved dates.
        The issuer of one security changes only at a reorganisation: before the successor's effective date (its
        Form 8-K12B) the predecessor still holds the shares. Neighbours that disagree and cannot be placed stay open."""
        def key_of(snap: dict, ln: dict) -> str:
            """The same security across dates: its CUSIP; for a line of a pre-N-PORT filing without a CUSIP, its name
            and class (the same name and class in another quarter is the same security)."""
            sk = ln.get('sec_key') or ''
            if sk.startswith('CUSIP:'):
                return sk
            if snap['form'] != 'NPORT-P' and ln['line_kind'] == 'stock':
                return f'CL|{_words(core_name(ln["raw_name"]))}|{ln["cls"]}'
            return ''

        timeline: dict[str, dict[str, set[str]]] = {}
        for snap in snaps:
            for ln in snap['lines']:
                key = key_of(snap, ln)
                if ln['line_kind'] == 'stock' and key and ln.get('issuer'):
                    timeline.setdefault(key, {}).setdefault(snap['report_date'], set()).add(ln['issuer'])
        single = {sk: {d: next(iter(v)) for d, v in dates.items() if len(v) == 1} for sk, dates in timeline.items()}
        filled = dict(previous=0, next=0, same=0, before_successor=0, after_successor=0, open=0)
        for snap in snaps:
            d = snap['report_date']
            for ln in snap['lines']:
                sk = key_of(snap, ln)
                if ln['line_kind'] != 'stock' or ln.get('issuer') or not sk:
                    continue
                dates = single.get(sk, {})
                prev = max((x for x in dates if x < d), default=None)
                nxt = min((x for x in dates if x > d), default=None)
                cik, how = None, ''
                if prev and nxt:
                    a, b = dates[prev], dates[nxt]
                    succ = self.succession_date(b)
                    if a == b:
                        cik, how = a, 'same'
                    elif succ and d < succ:
                        cik, how = a, 'before_successor'
                    elif succ and d >= succ:
                        cik, how = b, 'after_successor'
                elif prev:
                    cik, how = dates[prev], 'previous'
                elif nxt:
                    succ = self.succession_date(dates[nxt])
                    if not (succ and d < succ):
                        cik, how = dates[nxt], 'next'
                if cik:
                    ln['issuer'] = cik
                    ln['map'] = (f'{"cusip" if sk.startswith("CUSIP:") else "name_class"}_propagated_{how}', 'medium')
                    ln['why'] = ''
                    if not sk.startswith('CUSIP:'):
                        ln['sec_key'] = cikcls_security(cik, ln['cls'])
                        ln['sec_how'] = f'name_class_propagated_{how}'
                    filled[how] += 1
                else:
                    filled['open'] += 1
        self.step('cusip_propagation', ' '.join(f'{k}={v}' for k, v in filled.items()))

    def resolve_line(self, ln: dict, when: str, snap_id: str) -> None:
        sk = ln.get('sec_key')
        if sk and sk.startswith('CUSIP:'):
            res = self.issuer_for_cusip(sk[6:], ln['raw_name'], ln.get('title', ''), when,
                                        self.v05.get(sk[6:], {}))
        elif sk:
            res = self.issuer_by_name(ln['raw_name'], when)
            res = dict(res, method='isin_only_' + res['method'])
        else:
            res = dict(cik=None, method='', conf='', why=f'no valid identifier (cusip {ln["cusip"] or "blank"}, '
                                                       f'isin {ln["isin"] or "blank"})',
                       cands=self.name_candidates(core_name(ln['raw_name'])))
            self.flag('unidentified_security', ln['raw_name'], snap_id,
                      f'cusip {ln["cusip"] or "blank"!r}, isin {ln["isin"] or "blank"!r}; kept unmapped', '')
        ln['issuer'], ln['map'], ln['why'], ln['cands'] = res['cik'], (res['method'], res['conf'] or 'medium' if res['cik']
                                                                       else ''), res['why'], res['cands']

    # -- types, weights, status ------------------------------------------------------------------------------------
    def issuer_sic(self, cik: str | None) -> str:
        sub = self.issuer_sub(cik) if cik else None
        return sub['sic'] if sub else ''

    def assign_types(self, snaps: list[dict]) -> None:
        """Security type from explicit evidence on every line, then one type per security: an explicit exclusion on any
        of its lines wins (TYPE_ORDER), so a right or a CVR never stays in common stock."""
        per_line: dict[str, list[str]] = {}
        for snap in snaps:
            for ln in snap['lines']:
                sk = ln.get('sec_key') or ''
                if ln['line_kind'] != 'stock' and ln['line_kind'] != 'negative':
                    continue
                ev = self.v05.get(sk[6:], {}) if sk.startswith('CUSIP:') else {}
                t, basis = self.line_type(ln, ev)
                ln['type_line'], ln['type_basis_line'] = t, basis
                if sk:
                    per_line.setdefault(sk, []).append(t)
        sec_type: dict[str, tuple[str, str]] = {}
        for sk, types in per_line.items():
            best = min(types, key=lambda t: TYPE_ORDER.index(t) if t in TYPE_ORDER else len(TYPE_ORDER))
            sec_type[sk] = (best, 'most specific line evidence: ' + ','.join(sorted(set(types))))
        self.sec_type = sec_type
        for snap in snaps:
            for ln in snap['lines']:
                sk = ln.get('sec_key')
                if sk and sk in sec_type:
                    ln['type'], ln['type_basis'] = sec_type[sk]
                else:
                    ln['type'], ln['type_basis'] = ln.get('type_line', 'unknown'), ln.get('type_basis_line', '')
                if sk in self.type_over:
                    ov = self.type_over[sk]
                    ln['type'], ln['type_basis'] = ov['value'], 'override: ' + ov['evidence']
                    self.sec_type[sk] = (ov['value'], ln['type_basis'])

    def line_type(self, ln: dict, ev: dict) -> tuple[str, str]:
        sic = self.issuer_sic(ln.get('issuer'))
        return security_type(ln['raw_name'], ln.get('title', ''), sic, ev.get('openfigi_type', ''),
                             ln.get('asset_cat', ''), ev.get('v05_type', ''))

    def weights(self, snaps: list[dict]) -> None:
        """Two weights, kept apart. fund_weight_raw = value / the fund's total investments of the snapshot (N-PORT
        pctVal is kept as reported). stock_weight = value / the sum of positive stock lines that are not non-stock
        types. Negative lines and non-stock types have no stock weight."""
        for snap in snaps:
            total = snap['total'] or 0.0
            denom = sum(ln['value_usd'] for ln in snap['lines']
                        if ln['line_kind'] == 'stock' and ln['value_usd'] > 0 and ln.get('type') not in NON_STOCK_TYPES)
            snap['stock_total'] = denom
            for ln in snap['lines']:
                ln['fund_weight_raw'] = (ln['value_usd'] / total) if total else None
                ln['weight'] = (ln['value_usd'] / denom) if (ln['line_kind'] == 'stock' and ln['value_usd'] > 0
                                                             and ln.get('type') not in NON_STOCK_TYPES and denom) else None

    def assemble(self) -> dict:
        trust_rows = self.trust_filings()
        self.step('trust_filings', f'{len(trust_rows)} filings public by {R.BUILD_CUTOFF_UTC}')
        snaps = self.snapshots(trust_rows)
        by_quarter: dict[str, list[dict]] = {}
        for s in snaps:
            by_quarter.setdefault(s['report_date'], []).append(s)
        for s in snaps:
            s['is_primary'] = 0
        quarters = quarter_ends(PRE_START, max(s['report_date'] for s in snaps))
        self.quarters = quarters
        for rd in quarters:
            cands = [s for s in by_quarter.get(rd, []) if s['completeness'] == 'full' and s['reconciled']]
            if not cands:
                self.flag('missing_quarter', rd, '', 'no complete reconciled 500 Index Fund schedule public by the cutoff')
                continue
            pick = max(cands, key=lambda s: (s['form'] == 'NPORT-P', s['filing_date'], s['accession']))
            pick['is_primary'] = 1
            if len(cands) > 1:
                self.flag('multiple_complete_snapshots', rd, pick['snapshot_id'],
                          f'{[c["snapshot_id"] for c in cands]}; canonical chosen')
        for s in snaps:
            if not s['is_primary'] and s['completeness'] != 'full':
                self.flag('snapshot_not_full', s['accession'], s['snapshot_id'], s['completeness'])
        self.v05_reference(V05_SNAPSHOT_MEMBERS)
        self.v05_reference(V05_SNAPSHOTS)
        self.identity(snaps)
        self.assign_types(snaps)
        self.weights(snaps)
        primary = [s for s in snaps if s['is_primary']]
        latest = primary[-1]
        latest_common = {ln['issuer'] for ln in latest['lines']
                         if ln.get('issuer') and ln.get('type') in ELIGIBLE_TYPES and ln['line_kind'] == 'stock'}
        self.step('primary_quarters', f'{len(primary)} primary snapshots of {len(snaps)} snapshots')
        return self.tables(trust_rows, snaps, primary, latest_common)

    def tables(self, trust_rows, snaps, primary, latest_common) -> dict:
        latest_date = primary[-1]['report_date']
        status_as_of = R.STATUS_CUTOFF
        issuers_all = sorted({ln['issuer'] for s in snaps for ln in s['lines'] if ln.get('issuer')})
        universe = sorted({ln['issuer'] for s in primary for ln in s['lines']
                           if ln.get('issuer') and ln.get('type') not in EXCLUDED_TYPES})
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
        self.links, self.final, self.resolved, self.info = links, final, resolved, info
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
                if ln.get('sec_key'):
                    presence.setdefault(('security', ln['sec_key'], ln['issuer']), []).append(i)
        rows = []
        numbering: dict[tuple[str, str], int] = {}     # span numbers run over all issuers of one subject
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
            for sp in spans:
                numbering[(level, subj)] = numbering.get((level, subj), 0) + 1
                n = numbering[(level, subj)]
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

        # security facts: the identifiers and type of each security key, from every line that carries it
        sec_facts: dict[str, dict] = {}
        raw_of: dict[str, bytes] = {}
        spans_of: dict[tuple[str, str], dict] = {}
        for s in snaps:
            src = source_id(sha256_bytes(s['raw']))
            for ln in s['lines']:
                sk = ln.get('sec_key')
                if not sk or ln['line_kind'] != 'stock':
                    continue
                raw_of.setdefault(sk, s['raw'])
                f = sec_facts.setdefault(sk, dict(issuer=ln['issuer'], cls=ln['cls'], name=ln['raw_name'], isin='',
                                                  lei='', source=src, derivation='', last=s['report_date'],
                                                  scheme='CUSIP' if sk.startswith('CUSIP:') else
                                                  ('ISIN' if sk.startswith('ISIN:') else 'CIKCLS')))
                f['isin'] = f['isin'] or valid_isin(ln.get('isin'))
                f['lei'] = f['lei'] or valid_lei(ln.get('lei'))
                if ln.get('sec_how') and ln.get('sec_how') != 'cusip':
                    f['derivation'] = ln['sec_how']
                if ln.get('issuer'):
                    f['issuer'] = ln['issuer']
                    f['last'] = s['report_date']
                    span = spans_of.setdefault((sk, ln['issuer']), dict(first=s['report_date'], last=s['report_date'],
                                                                         lines=0, method=ln.get('map', ('', ''))[0]))
                    span['first'] = min(span['first'], s['report_date'])
                    span['last'] = max(span['last'], s['report_date'])
                    span['lines'] += 1
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
        sec_ok = {sk for sk, f in sec_facts.items() if f['issuer']}
        for sk, f in sorted(sec_facts.items()):
            if sk not in sec_ok:
                continue
            cik = f['issuer']
            sec_t, sec_basis = self.sec_type.get(sk, ('unknown', ''))
            t['securities'].append(dict(security_id=sk, issuer_id=issuer_id(cik), class_code=f['cls'],
                                        security_type=sec_t, type_basis=sec_basis, name=f['name'],
                                        id_scheme=f['scheme'], notes=f['derivation'],
                                        source_id=source_id(sha256_bytes(raw_of[sk]))))
            ids = []
            if sk.startswith('CUSIP:'):
                ids.append(('cusip', sk[6:]))
            if f['isin']:
                ids.append(('isin', f['isin']))
            if f['lei']:
                ids.append(('lei', f['lei']))
            v05row = self.v05.get(sk[6:], {}) if sk.startswith('CUSIP:') else {}
            ov_t = self.ticker_over.get(sk)
            if ov_t:
                ids.append(('ticker', ov_t['value']))
            elif v05row.get('ticker'):
                ids.append(('ticker', v05row['ticker']))
            elif self.sec_type.get(sk, ('',))[0] in ('common', 'adr'):
                # no v05 code: the issuer's one current SEC ticker (any class), only for an eligible class; warrant
                # and unit codes ('-WT', 'WS') are not share codes and make the choice ambiguous
                own = sorted(k for k, v in self.tickers.items() if v['cik'] == cik and '-' not in k)
                if len(own) == 1:
                    ids.append(('ticker', own[0]))
            seen_ids = set()
            for id_type, value in ids:
                if value and (id_type, value) not in seen_ids:
                    seen_ids.add((id_type, value))
                    t['security_identifiers'].append(dict(security_id=sk, id_type=id_type, id_value=value, valid_from='',
                                                          valid_to='', source_id=source_id(sha256_bytes(raw_of[sk]))))
        for (sk, cik), sp in sorted(spans_of.items()):
            t['security_issuer_spans'].append(dict(security_id=sk, issuer_id=issuer_id(cik), span_no=1,
                                                   first_as_of=sp['first'], last_as_of=sp['last'], lines=sp['lines'],
                                                   method=sp['method'], source_id=None))
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
                residual_usd=s.get('residual'), rounding_bound_usd=s.get('bound'), unparsed_rows=s.get('unparsed', 0),
                start_rule=s['start_rule'], source_id=src, notes=s['notes']))
            for n, ln in enumerate(s['lines'], 1):
                cik = ln.get('issuer')
                cands = sorted(ln.get('cands') or []) if not cik else []
                t['holdings'].append(dict(
                    snapshot_id=s['snapshot_id'], line_no=n, line_kind=ln['line_kind'], raw_name=ln['raw_name'],
                    raw_class_text=ln['raw_class_text'], cusip_reported=ln.get('cusip', ''), isin_reported=ln.get('isin', ''),
                    lei_reported=ln.get('lei', ''), title_reported=ln.get('title', ''), shares=ln['shares'],
                    value_usd=ln['value_usd'], pct_val_percent=ln.get('pct_val'), fund_weight_raw=ln.get('fund_weight_raw'),
                    stock_weight=ln.get('weight'),
                    security_id=(ln.get('sec_key') if ln['line_kind'] == 'stock' and ln.get('sec_key') in sec_ok
                                 else None),
                    issuer_id=issuer_id(cik) if cik else None, security_type=ln.get('type', 'unknown'),
                    map_method=ln.get('map', ('', ''))[0], map_confidence=ln.get('map', ('', ''))[1],
                    unmapped_reason='' if cik else ln.get('why', ''), candidate_issuers=' '.join(cands), source_id=src))
        t['membership_spans'] = self.spans(primary, universe)
        from . import top200
        extra_tables = top200.compute(self, snaps, primary, info, final, resolved, status_as_of)
        for k, v in extra_tables.items():
            t[k] = v
        for n, r in enumerate(sorted(self.review, key=lambda r: (r['review_type'], r.get('subject_id', ''),
                                                                 r.get('snapshot_id', ''), r.get('detail', ''))), 1):
            src = r.get('source_id') or None
            t['review_items'].append(dict(review_id=n, review_type=r['review_type'], subject_id=r.get('subject_id', ''),
                                          snapshot_id=r.get('snapshot_id', ''), detail=r.get('detail', ''),
                                          source_id=src if src in known_sources else None))
        t['_extra'] = dict(bridge=self.bridge_stats, universe=len(universe), trust_filings=len(trust_rows),
                           requests=self.store.stats.requests, retries=self.store.stats.retries,
                           failures=self.store.stats.failures, status_as_of=status_as_of, steps=self.steps,
                           quarters=self.quarters)
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
                          ('rule_version', R.RULE_VERSION), ('built_at_utc', utc_now_iso()),
                          ('status_cutoff', R.STATUS_CUTOFF), ('build_cutoff_utc', R.BUILD_CUTOFF_UTC)]
                         + [(k, str(v)) for k, v in sorted(meta.items())])
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
                           content_sha256_scope='all tables and views except meta and build_log',
                           rules=json.dumps(R.describe(), ensure_ascii=False, sort_keys=True)))
    counts = {k: len(v) for k, v in t.items() if isinstance(v, list)}
    return dict(content_sha256=digest, network_requests=store.stats.requests, retries=store.stats.retries,
                failures=store.stats.failures, offline=offline, counts=counts, extra=extra, build_id=build_id,
                code_commit=commit, raw_manifest_sha256=manifest_sha)
