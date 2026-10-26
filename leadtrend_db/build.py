"""Assemble leadtrend_db v1 from the raw archive and write data/db/leadtrend_db_v1.sqlite.

Each input is read through RawStore: an archived file is read from disk (sha256 verified); a missing one is fetched
from SEC once and archived. offline=True forbids fetching. Everything written to the database comes from the
archive and config/db_overrides_v1.csv, and the content hash leaves out timestamps, so two builds from the same
archive hash identically.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import sources as S
from .identity import (class_code_from_text, cik10, cikcls_security, core_name, cusip_security, issuer_id,
                       load_overrides, name_ratio, name_key, norm_core, overrides_of, parse_company_tickers,
                       parse_cik_lookup, security_type, ticker_key, CLASS_LETTER)
from .parse_ncsr import parse_500_schedule
from .parse_nport import parse_holdings, screen
from .sources import RawMissing, RawStore, sha256_bytes, utc_now_iso
from .status import classify, detect_successors, filing_rows

SCHEMA_FILE = Path(__file__).with_name('schema.sql')
SCHEMA_VERSION = 1
PRE_FORMS = ('N-Q', 'N-CSR', 'N-CSRS', 'NPORT-EX')
KIND = {'N-Q': 'nq', 'N-CSR': 'ncsr', 'N-CSRS': 'ncsrs', 'NPORT-EX': 'other', 'NPORT-P': 'nport'}
SNAP_PREFIX = {'N-Q': 'NQ', 'N-CSR': 'NCSR', 'N-CSRS': 'NCSRS', 'NPORT-EX': 'NPORTEX', 'NPORT-P': 'NPORT'}
PRE_START, BRIDGE_DATE, NPORT_START = '2013-12-31', '2019-12-31', '2019-09-30'
FILED_FROM = '2014-01-01'
MIN_FULL_ROWS = 400          # a 500 Index schedule has about 500 stock rows
RECONCILE_TOL = 0.001        # stock rows sum to the reported Total Common Stocks within 0.1%
NAME_BRIDGE_RATIO = 0.5      # name similarity needed to accept a 2019-12-31 share match
BRIDGE_SHARE_TOL = 0.01      # relative share-count tolerance for the 2019-12-31 match
CIK_PERIOD_DAYS = 95         # a candidate CIK must file a 10-K or 10-Q this close to the quarter end
ARCHIVE = 'https://www.sec.gov/Archives/edgar/data/36405'
SUBMISSIONS = 'https://data.sec.gov/submissions'
V05_SECURITIES = 'securities.csv'
V05_SNAPSHOT_MEMBERS = 'snapshot_members.csv'
V05_SNAPSHOTS = 'snapshots.csv'
TABLE_COLUMNS = {
    'sources': ['source_id', 'kind', 'url', 'raw_path', 'sha256', 'bytes', 'retrieved_at_utc', 'note'],
    'issuers': ['issuer_id', 'cik', 'name', 'sic', 'source_id'],
    'issuer_names': ['issuer_id', 'name', 'name_kind', 'first_seen', 'last_seen', 'source_id'],
    'issuer_links': ['predecessor_issuer_id', 'successor_issuer_id', 'relation', 'confidence', 'via_override',
                     'effective_date', 'evidence', 'source_id'],
    'securities': ['security_id', 'issuer_id', 'class_code', 'security_type', 'name', 'id_scheme', 'notes',
                   'source_id'],
    'security_identifiers': ['security_id', 'id_type', 'id_value', 'valid_from', 'valid_to', 'source_id'],
    'snapshots': ['snapshot_id', 'fund_series', 'form', 'accession', 'primary_doc', 'holdings_as_of', 'filed_date',
                  'available_at_utc', 'is_primary', 'completeness', 'row_count', 'excluded_rows', 'equity_value_usd',
                  'excluded_value_usd', 'total_investments_reported_usd', 'reported_common_stock_total_usd',
                  'coverage_ratio', 'source_id', 'notes'],
    'holdings': ['snapshot_id', 'line_no', 'raw_name', 'raw_class_text', 'cusip_reported', 'shares', 'value_usd',
                 'weight', 'weight_basis', 'security_id', 'issuer_id', 'map_method', 'map_confidence',
                 'unmapped_reason', 'source_id'],
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


def _class_text(text: str) -> str:
    m = CLASS_LETTER.search(text or '')
    return f'Class {m.group(1)}' if m else ''


class Build:
    def __init__(self, store: RawStore, log=print):
        self.store = store
        self.log = log
        self.steps: list[tuple[str, str]] = []
        self.review: list[dict] = []
        self.sub_cache: dict[str, dict | None] = {}
        self.overrides = load_overrides(S.OVERRIDES_PATH)

    def step(self, name: str, detail: str = '') -> None:
        self.steps.append((name, detail))
        self.log(f'[{name}] {detail}')

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
        """All submissions pages of one issuer. None when the SEC has no submissions file for it."""
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
                 exchanges=list(obj.get('exchanges') or []), tickers=list(obj.get('tickers') or []),
                 former_names=[x.get('name', '') for x in (obj.get('formerNames') or [])],
                 filings=filings, source_sha=sha256_bytes(raw))
        self.sub_cache[cik] = d
        return d

    def pre_snapshots(self, trust_rows: list[dict]) -> list[dict]:
        """N-Q, N-CSR, N-CSRS and NPORT-EX filings for report dates 2013-12-31 to 2019-12-31, filed from 2014.
        Each is searched for the 500 Index schedule: the primary document first, then every other htm document of
        the filing. An NPORT-EX exhibit without the schedule belongs to another fund and is left out."""
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
            if not res['found'] and f['form'] != 'NPORT-EX':
                index = self.store.get_json(f'{ARCHIVE}/{acc}/index.json', f'filings/{acc}/index.json',
                                            'filing_index', 'filing index')
                for item in index['directory']['item']:
                    name = item['name']
                    if name == f['document'] or '-index' in name.lower() \
                            or not name.lower().endswith(('.htm', '.html')):
                        continue
                    other = self.store.get_optional(f'{ARCHIVE}/{acc}/{name}', f'filings/{acc}/{name}', 'other',
                                                    'other document of the same filing')
                    if other is None:
                        checked.append(f'{name} (not served)')
                        continue
                    checked.append(name)
                    cand = parse_500_schedule(other)
                    if cand['found']:
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
        rows, total = res['stock_rows'], res['total_common_stocks_usd']
        value = sum(r['value_usd'] for r in rows)
        if len(rows) >= MIN_FULL_ROWS and total and abs(value - total) <= RECONCILE_TOL * total:
            return 'full'
        return 'partial'

    def nport_screen(self, trust_rows: list[dict]) -> dict[str, dict]:
        """Screen each N-PORT filing of the trust once and keep the decision in raw/nport_screen.csv. A filing read
        from data/v05/raw is archived only when it belongs to the 500 Index Fund."""
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
        for acc, r in screen_rows.items():
            if r['decision'] == 'target':
                by_date.setdefault(r['report_date'], []).append(r)
        acceptance = {r['accession']: r['acceptance'] for r in trust_rows}
        filed = {r['accession']: r['filing_date'] for r in trust_rows}
        out = []
        for rd in sorted(by_date):
            cands = sorted(by_date[rd], key=lambda r: acceptance[r['accession']])
            pick = cands[-1]
            if len(cands) > 1:
                self.review.append(dict(review_type='nport_multiple_targets', subject_id=pick['accession'],
                                        snapshot_id='', detail=f'{rd}: {[c["accession"] for c in cands]}; latest used'))
            acc = pick['accession']
            url = f'{ARCHIVE}/{acc_nodash(acc)}/primary_doc.xml'
            raw = self.store.get(url, f'nport/nport_{acc_nodash(acc)}.xml', 'nport', 'N-PORT primary document')
            meta = dict(accession=acc, filing_date=filed[acc], available_at_utc=_ts(acceptance[acc]))
            parsed = parse_holdings(raw, meta, f'nport/nport_{acc_nodash(acc)}.xml')
            total = screen(raw)['total_investments_usd']
            equity = sum(h.val_usd for h in parsed.holdings)
            notes = '; '.join(parsed.parse_notes)
            complete = 'full' if len(parsed.holdings) >= MIN_FULL_ROWS and not parsed.parse_notes and total \
                and equity / total >= 0.95 else 'partial'
            lines = [dict(raw_name=h.name, raw_class_text=_class_text(h.title), cusip_reported=h.cusip,
                          shares=h.balance, value_usd=h.val_usd, weight=h.pct_val / 100.0,
                          weight_basis='pct_net_assets_nport', isin=h.isin, lei=h.lei, title=h.title,
                          cls=class_code_from_text(h.title)) for h in parsed.holdings]
            out.append(dict(form='NPORT-P', snapshot_id=f'NPORT_{rd}_{acc}', accession=acc,
                            primary_doc='primary_doc.xml', report_date=rd, filing_date=filed[acc],
                            acceptance=_ts(acceptance[acc]), raw=raw, completeness=complete,
                            lines=lines, equity=equity, total=total, excluded_rows=parsed.non_equity_count,
                            excluded_value=total - equity, reported_common=None, notes=notes, url=url))
        return out

    # -- identity ------------------------------------------------------------------------------------------------
    def identity(self, snaps: list[dict]) -> None:
        """Map every holding to an issuer (CIK) and a security. Sets line['issuer'], line['sec'], and methods."""
        tickers = parse_company_tickers(json.loads(self.store.get(
            'https://www.sec.gov/files/company_tickers.json', 'reference/company_tickers.json', 'company_tickers',
            'SEC company_tickers.json (current tickers)').decode('utf-8')))
        lookup = parse_cik_lookup(self.store.get(
            'https://www.sec.gov/Archives/edgar/cik-lookup-data.txt', 'reference/cik-lookup-data.txt', 'cik_lookup',
            'SEC cik-lookup-data.txt (names to CIK)').decode('utf-8', errors='replace'))
        v05 = self.v05_securities()
        cusip_over = overrides_of(self.overrides, 'cusip_cik')
        name_over = overrides_of(self.overrides, 'name_cik')

        # 1. N-PORT CUSIPs -> CIK (v05 source evidence, then the ticker in the v05 table against SEC tickers)
        nport_cusip: dict[str, dict] = {}
        for snap in snaps:
            if snap['form'] != 'NPORT-P':
                continue
            for ln in snap['lines']:
                nport_cusip[ln['cusip_reported']] = dict(name=ln['raw_name'], title=ln['title'], isin=ln['isin'],
                                                         lei=ln['lei'], cls=ln['cls'])
        cusip_cik: dict[str, tuple[str | None, str, str, str]] = {}
        for cusip in sorted(nport_cusip):
            info = nport_cusip[cusip]
            if cusip in cusip_over:
                cusip_cik[cusip] = (cik10(cusip_over[cusip]['value']), 'override_cusip_cik', 'high', '')
                continue
            ev = v05.get(cusip, {})
            if ev.get('evidence_cik'):
                cusip_cik[cusip] = (ev['evidence_cik'], 'v05_source_evidence_cik', 'high', '')
                continue
            tk = ticker_key(ev.get('ticker', ''))
            if tk and tk in tickers:
                t = tickers[tk]
                conf = 'high' if name_ratio(info['name'], t['title']) >= NAME_BRIDGE_RATIO else 'medium'
                cusip_cik[cusip] = (t['cik'], 'sec_company_tickers_via_v05_ticker', conf, '')
            else:
                cusip_cik[cusip] = (None, '', '', 'no_cik_for_cusip' if tk else 'no_ticker_for_cusip')
        # 1b. CUSIPs with no CIK from the ticker (companies that left after 2019 are not in the SEC ticker list):
        # the N-PORT issuer name against the SEC name lookup, as for the pre-N-PORT names
        first_date: dict[str, str] = {}
        for snap in snaps:
            if snap['form'] == 'NPORT-P':
                for ln in snap['lines']:
                    first_date.setdefault(ln['cusip_reported'], snap['report_date'])
        for cusip in sorted(cusip_cik):
            cik, meth, conf, why = cusip_cik[cusip]
            if cik is None and cusip not in cusip_over:
                found, how = self.cik_by_name(norm_core(nport_cusip[cusip]['name']), first_date[cusip], lookup)
                if found:
                    cusip_cik[cusip] = (found, how, 'medium', '')
                else:
                    cusip_cik[cusip] = (None, '', '', f'{why}; name: {how}')
        for cusip, (cik, meth, conf, why) in sorted(cusip_cik.items()):
            if cik is None:
                self.review.append(dict(review_type='nport_cusip_unmapped', subject_id=cusip_security(cusip),
                                        snapshot_id='', detail=why))

        # 2. Bridge 2019-12-31: N-CSR rows against N-PORT rows with the same share count
        bridge = next((s for s in snaps if s['form'] != 'NPORT-P' and s['report_date'] == BRIDGE_DATE), None)
        nport_1231 = next((s for s in snaps if s['form'] == 'NPORT-P' and s['report_date'] == BRIDGE_DATE), None)
        matched: dict[tuple[str, str], str] = {}     # (normalised name, class) -> CUSIP
        bridge_stats = dict(ncsr_rows=0, matched=0, unmatched=0, ambiguous=0, nport_rows=0)
        ratios: list[float] = []
        if bridge and nport_1231:
            bridge_stats['nport_rows'] = len(nport_1231['lines'])
            used: set[str] = set()
            ratios = []
            for ln in bridge['lines']:
                bridge_stats['ncsr_rows'] += 1
                # the two filings report the same position with a small uniform difference in share counts
                # (about 0.3% in 2019), so shares must agree within BRIDGE_SHARE_TOL, and the name and class too
                cands = [c for c in nport_1231['lines'] if c['cusip_reported'] not in used and c['cls'] == ln['cls']
                         and abs(c['shares'] - ln['shares']) <= BRIDGE_SHARE_TOL * ln['shares']]
                scored = sorted(((name_ratio(ln['raw_name'], c['raw_name']), c) for c in cands),
                                key=lambda x: -x[0])
                if not scored or scored[0][0] < NAME_BRIDGE_RATIO:
                    bridge_stats['unmatched'] += 1
                    self.review.append(dict(review_type='bridge_unmatched', subject_id=ln['raw_name'],
                                            snapshot_id=bridge['snapshot_id'],
                                            detail=f'shares {ln["shares"]:.0f} class {ln["cls"]}: no N-PORT row'))
                    continue
                if len(scored) > 1 and scored[0][0] == scored[1][0]:
                    bridge_stats['ambiguous'] += 1
                    self.review.append(dict(review_type='bridge_ambiguous', subject_id=ln['raw_name'],
                                            snapshot_id=bridge['snapshot_id'], detail='tie on name and shares'))
                    continue
                cusip = scored[0][1]['cusip_reported']
                used.add(cusip)
                ratios.append(scored[0][1]['shares'] / ln['shares'])
                matched[(norm_core(ln['raw_name']), ln['cls'])] = cusip
                bridge_stats['matched'] += 1
        if bridge and nport_1231 and ratios:
            ratios.sort()
            bridge_stats['share_ratio_nport_over_ncsr_min'] = round(ratios[0], 6)
            bridge_stats['share_ratio_nport_over_ncsr_median'] = round(ratios[len(ratios) // 2], 6)
            bridge_stats['share_ratio_nport_over_ncsr_max'] = round(ratios[-1], 6)
        self.step('bridge_2019_12_31', ' '.join(f'{k}={v}' for k, v in bridge_stats.items()))

        # 3. Name clusters -> CIK, for pre-N-PORT rows
        cluster_qe: dict[str, str] = {}
        for snap in snaps:
            if snap['form'] == 'NPORT-P':
                continue
            for ln in snap['lines']:
                key = norm_core(ln['raw_name'])
                cluster_qe[key] = min(cluster_qe.get(key, snap['report_date']), snap['report_date'])
        cluster_cik: dict[str, tuple[str | None, str]] = {}
        for (key, cls), cusip in sorted(matched.items()):
            if cusip_cik.get(cusip, (None,))[0]:
                cluster_cik[key] = (cusip_cik[cusip][0], 'bridge_2019_12_31_share_match')
        for key in sorted(cluster_qe):
            if key in cluster_cik:
                continue
            if key in name_over:
                cluster_cik[key] = (cik10(name_over[key]['value']), 'override_name_cik')
                continue
            cluster_cik[key] = self.cik_by_name(key, cluster_qe[key], lookup)
        self.step('name_clusters', f'{len(cluster_cik)} pre-N-PORT name clusters; '
                  f'{sum(1 for v in cluster_cik.values() if v[0])} with a CIK')

        # 4. CIK + class -> the first N-PORT CUSIP (carries a pre-N-PORT security into its N-PORT identity)
        first_nport = next((s for s in snaps if s['form'] == 'NPORT-P'), None)
        cik_class: dict[tuple[str, str], list[str]] = {}
        if first_nport:
            for ln in first_nport['lines']:
                cik = cusip_cik.get(ln['cusip_reported'], (None,))[0]
                if cik:
                    cik_class.setdefault((cik, ln['cls']), []).append(ln['cusip_reported'])

        # 5. Assign every line
        for snap in snaps:
            if snap['form'] == 'NPORT-P':
                for ln in snap['lines']:
                    cik, meth, conf, why = cusip_cik[ln['cusip_reported']]
                    ln['issuer'] = cik10(cik) if cik else None
                    ln['sec'] = cusip_security(ln['cusip_reported']) if cik else None
                    ln['map'] = (meth, conf) if cik else ('', '')
                    ln['why'] = why
                    ln['cusip_for_security'] = ln['cusip_reported']
                continue
            for ln in snap['lines']:
                key = norm_core(ln['raw_name'])
                cik, how = cluster_cik.get(key, (None, 'no_cluster'))
                ln['issuer'] = cik
                ln['why'] = '' if cik else how
                if not cik:
                    ln['sec'], ln['map'] = None, ('', '')
                    continue
                if (key, ln['cls']) in matched:
                    cusip = matched[(key, ln['cls'])]
                    ln['sec'], ln['map'] = cusip_security(cusip), (how + '|cusip_bridge_2019_12_31', 'high')
                elif (cik, ln['cls']) in cik_class and len(cik_class[(cik, ln['cls'])]) == 1:
                    cusip = cik_class[(cik, ln['cls'])][0]
                    ln['sec'], ln['map'] = cusip_security(cusip), (how + '|cik_class_first_nport', 'medium')
                else:
                    ln['sec'], ln['map'] = cikcls_security(cik, ln['cls']), (how, 'medium')
        self.nport_info = nport_cusip
        self.cusip_cik = cusip_cik
        self.v05_sec = v05
        self.tickers = tickers
        self.bridge_stats = bridge_stats
        self.matched = matched
        self.cluster_cik = cluster_cik

    def cik_by_name(self, key: str, qe: str, lookup: dict[str, set[str]]) -> tuple[str | None, str]:
        """CIK for a normalised name. One SEC lookup candidate gives it; several candidates are narrowed to the
        ones whose SEC name (current or former) is the same name, then to those that filed a 10-K or 10-Q near
        the quarter end. Anything still open is left unmapped for review."""
        cands = sorted(lookup.get(key, set()))
        if not cands:
            return None, 'no_cik_candidate'
        if len(cands) == 1:
            return cands[0], 'cik_lookup_name'
        subs = {c: self.issuer_sub(c) for c in cands}
        exact = [c for c in cands if subs[c] and any(name_key(n) == key for n in [subs[c]['name']] + subs[c]['former_names'])]
        pool = exact or cands
        ok = [c for c in pool if subs[c] and any(
            f['form'] in ('10-K', '10-Q') and f['report_date'] and _days(f['report_date'], qe) <= CIK_PERIOD_DAYS
            for f in subs[c]['filings'])]
        if len(ok) == 1:
            return ok[0], 'cik_lookup_name_and_periodic_filing' + ('_exact_sec_name' if exact else '')
        return None, f'ambiguous_cik_candidates:{len(cands)}->{len(ok)}'

    def v05_securities(self) -> dict[str, dict]:
        raw = self.v05_reference(V05_SECURITIES).decode('utf-8')
        out: dict[str, dict] = {}
        for r in csv.DictReader(io.StringIO(raw)):
            sid = r['security_id']
            if not sid.startswith('CUSIP:'):
                continue
            ev = r.get('source_evidence') or ''
            m = re.search(r'SEC ticker corroboration CIK=(\d+)', ev)
            out[sid[6:]] = dict(ticker=r.get('ticker', ''), evidence_cik=cik10(m.group(1)) if m else '')
        return out

    def v05_reference(self, name: str) -> bytes:
        """Archived copy of a v05 normalized table: the build reads the copy, not data/v05."""
        return self.store.import_local(f'local:data/v05/normalized/{name}', f'reference/v05/{name}', 'other',
                                       S.V05_NORMALIZED / name, f'copied from data/v05/normalized/{name}')

    # -- assembly ------------------------------------------------------------------------------------------------
    def snapshots(self, trust_rows: list[dict]) -> list[dict]:
        screen_rows = self.nport_screen(trust_rows)
        snaps = []
        for p in self.pre_snapshots(trust_rows):
            res = p['res']
            total = res['total_investments_usd']
            stock = res['stock_rows'] if p['completeness'] != 'summary' else []
            lines = [dict(raw_name=r['name'], raw_class_text=r['raw_class_text'], cusip_reported='', shares=r['shares'],
                          value_usd=r['value_usd'], weight=(r['value_usd'] / total if total else None),
                          weight_basis='share_of_total_investments', isin='', lei='', title='',
                          cls=class_code_from_text(r['raw_class_text'])) for r in stock]
            notes = res['notes'] + [f'checked {len(p["checked"])} document(s)']
            snaps.append(dict(form=p['form'], snapshot_id=f'{SNAP_PREFIX[p["form"]]}_{p["report_date"]}_{p["accession"]}',
                              accession=p['accession'], primary_doc=p['document'], report_date=p['report_date'],
                              filing_date=p['filing_date'], acceptance=p['acceptance'], raw=p['raw'],
                              completeness=p['completeness'], lines=lines, equity=sum(r['value_usd'] for r in stock),
                              total=total, excluded_rows=len(res['excluded']),
                              excluded_value=sum(e['value_usd'] for e in res['excluded']),
                              reported_common=res['total_common_stocks_usd'], notes='; '.join(notes),
                              url=p['url']))
        snaps += self.nport_snapshots(trust_rows, screen_rows)
        snaps.sort(key=lambda s: (s['report_date'], s['form'], s['accession']))
        return snaps

    def assemble(self) -> dict:
        trust_rows = self.trust_filings()
        self.step('trust_filings', f'{len(trust_rows)} filings listed for the trust')
        snaps = self.snapshots(trust_rows)
        for s in snaps:
            if s['completeness'] != 'full' and s['form'] == 'NPORT-P':
                self.review.append(dict(review_type='snapshot_not_full', subject_id=s['accession'],
                                        snapshot_id=s['snapshot_id'], detail=s['completeness']))
        by_quarter: dict[str, list[dict]] = {}
        for s in snaps:
            by_quarter.setdefault(s['report_date'], []).append(s)
        for s in snaps:
            s['is_primary'] = 0
        for rd in quarter_ends(PRE_START, max(by_quarter)):
            cands = [s for s in by_quarter.get(rd, []) if s['completeness'] == 'full']
            if not cands:
                self.review.append(dict(review_type='missing_quarter', subject_id=rd, snapshot_id='',
                                        detail='no complete 500 Index schedule for this quarter'))
                continue
            pick = max(cands, key=lambda s: (s['form'] == 'NPORT-P', s['filing_date'], s['accession']))
            pick['is_primary'] = 1
            if len(cands) > 1:
                self.review.append(dict(review_type='multiple_complete_snapshots', subject_id=rd,
                                        snapshot_id=pick['snapshot_id'],
                                        detail=f'{[c["snapshot_id"] for c in cands]}; primary chosen'))
        for s in snaps:
            if s['is_primary'] == 0 and s['completeness'] != 'full' and s['form'] != 'NPORT-P':
                self.review.append(dict(review_type='snapshot_not_full', subject_id=s['accession'],
                                        snapshot_id=s['snapshot_id'], detail=s['completeness']))
        self.v05_reference(V05_SNAPSHOT_MEMBERS)
        self.v05_reference(V05_SNAPSHOTS)
        self.identity(snaps)
        primary = [s for s in snaps if s['is_primary']]
        self.step('primary_quarters', f'{len(primary)} primary snapshots of {len(snaps)} snapshots')
        latest = primary[-1]
        in_latest = {ln['issuer'] for ln in latest['lines'] if ln['issuer']}
        return self.tables(trust_rows, snaps, primary, in_latest)

    def tables(self, trust_rows, snaps, primary, in_latest) -> dict:
        # issuers used by holdings, universe (ever in VOO as a primary snapshot), and successors
        holding_ciks = sorted({ln['issuer'] for s in snaps for ln in s['lines'] if ln['issuer']})
        universe = sorted({ln['issuer'] for s in primary for ln in s['lines'] if ln['issuer']})
        status_as_of = max(r['retrieved_at_utc'] for r in self.store.records.values()
                           if r['kind'] == 'submissions')[:10]
        info: dict[str, dict] = {}
        for cik in sorted(set(holding_ciks)):
            sub = self.issuer_sub(cik)
            first_name = next((ln['raw_name'] for s in snaps for ln in s['lines'] if ln['issuer'] == cik), '')
            info[cik] = dict(name=sub['name'] if sub else core_name(first_name).strip(),
                             former_names=sub['former_names'] if sub else [],
                             exchanges=sub['exchanges'] if sub else [], tickers=sub['tickers'] if sub else [],
                             filings=sub['filings'] if sub else [], sic=sub['sic'] if sub else '',
                             source_sha=sub['source_sha'] if sub else '')
        prelim = {cik: classify(info[cik], cik in in_latest, status_as_of) for cik in info}
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
            for c in (l['successor'],):
                if c not in info:
                    sub = self.issuer_sub(c)
                    info[c] = dict(name=sub['name'] if sub else c, former_names=sub['former_names'] if sub else [],
                                   exchanges=sub['exchanges'] if sub else [], tickers=sub['tickers'] if sub else [],
                                   filings=sub['filings'] if sub else [], sic=sub['sic'] if sub else '',
                                   source_sha=sub['source_sha'] if sub else '')
                    prelim[c] = classify(info[c], c in in_latest, status_as_of)
        status_over = overrides_of(self.overrides, 'status')
        final = {}
        for cik in sorted(info):
            st = dict(prelim.get(cik) or classify(info[cik], cik in in_latest, status_as_of))
            if cik in status_over:
                ov = status_over[cik]
                st.update(status=ov['value'], delisting_date=ov['field'], reason=ov.get('reason', 'unknown'),
                          method='override_status', evidence=ov['evidence'], needs_review=0)
            final[cik] = st
        for l in links:
            pred = final.get(l['predecessor'])
            if pred and pred['status'] in ('delisted', 'unknown'):
                pred.update(status='continued_by_successor', counterparty=issuer_id(l['successor']),
                            method=pred['method'] + '+successor_link', needs_review=int(l['confidence'] != 'high')
                            if not l['via_override'] else 0)
            elif pred and pred['status'] in ('in_index', 'listed_out_of_index'):
                self.review.append(dict(review_type='link_predecessor_listed', subject_id=l['predecessor'],
                                        snapshot_id='', detail=f'link to {l["successor"]} but still listed'))
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
            if st['status'] == 'unknown' or st['needs_review']:
                self.review.append(dict(review_type='status_' + ('unknown' if st['status'] == 'unknown' else 'review'),
                                        subject_id=issuer_id(cik), snapshot_id='', detail=st['evidence'] or st['method']))
        self.step('listing_status', ' '.join(f'{k}={sum(1 for v in final.values() if v["status"] == k)}' for k in
                                             ('in_index', 'listed_out_of_index', 'delisted', 'continued_by_successor',
                                              'unknown')) + f' review={sum(1 for v in final.values() if v["needs_review"])}')
        return self.build_rows(trust_rows, snaps, primary, info, final, links, resolved, universe, status_as_of)

    def build_rows(self, trust_rows, snaps, primary, info, final, links, resolved, universe, status_as_of) -> dict:
        """Every output table as a list of dicts, plus spans. Ordering is fixed so the content hash is stable."""
        sid_of = lambda raw: source_id(sha256_bytes(raw))  # noqa: E731
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
        for cik in sorted(info):
            d = info[cik]
            t['issuers'].append(dict(issuer_id=issuer_id(cik), cik=cik, name=d['name'], sic=d['sic'],
                                     source_id=source_id(d['source_sha']) if d['source_sha'] else ''))
            t['listing_status'].append(dict(issuer_id=issuer_id(cik), **{k: final[cik][k] for k in
                                       ('status', 'status_as_of', 'delisting_date', 'reason', 'counterparty', 'evidence',
                                        'method', 'needs_review')},
                                       source_id=source_id(d['source_sha']) if d['source_sha'] else ''))
            target, how = resolved[cik]
            t['issuer_resolution'].append(dict(issuer_id=issuer_id(cik), resolved_issuer_id=issuer_id(target), method=how))
            names = {(d['name'], 'sec_current')} | {(n, 'sec_former') for n in d['former_names'] if n}
            for n, kind in sorted(names):
                if n:
                    t['issuer_names'].append(dict(issuer_id=issuer_id(cik), name=n, name_kind=kind, first_seen='',
                                                  last_seen='', source_id=source_id(d['source_sha']) if d['source_sha'] else ''))
        for l in sorted(links, key=lambda x: (x['predecessor'], x['successor'], x['relation'])):
            t['issuer_links'].append(dict(predecessor_issuer_id=issuer_id(l['predecessor']),
                                          successor_issuer_id=issuer_id(l['successor']), relation=l['relation'],
                                          confidence=l['confidence'], via_override=l['via_override'],
                                          effective_date=l['effective_date'], evidence=l['evidence'],
                                          source_id=source_id(info[l['predecessor']]['source_sha'])
                                          if info.get(l['predecessor'], {}).get('source_sha') else ''))
        obs: dict[tuple[str, str], list[str]] = {}
        sec_info: dict[str, dict] = {}
        for s in snaps:
            src = source_id(sha256_bytes(s['raw']))
            t['snapshots'].append(dict(snapshot_id=s['snapshot_id'], fund_series=S.FUND_SERIES, form=s['form'],
                                       accession=s['accession'], primary_doc=s['primary_doc'],
                                       holdings_as_of=s['report_date'], filed_date=s['filing_date'],
                                       available_at_utc=s['acceptance'], is_primary=s['is_primary'],
                                       completeness=s['completeness'], row_count=len(s['lines']),
                                       excluded_rows=s['excluded_rows'], equity_value_usd=s['equity'],
                                       excluded_value_usd=s['excluded_value'],
                                       total_investments_reported_usd=s['total'],
                                       reported_common_stock_total_usd=s['reported_common'],
                                       coverage_ratio=(s['equity'] / s['total']) if s['total'] else None,
                                       source_id=src, notes=s['notes']))
            for n, ln in enumerate(s['lines'], 1):
                cik = ln['issuer']
                t['holdings'].append(dict(snapshot_id=s['snapshot_id'], line_no=n, raw_name=ln['raw_name'],
                                          raw_class_text=ln['raw_class_text'], cusip_reported=ln['cusip_reported'],
                                          shares=ln['shares'], value_usd=ln['value_usd'], weight=ln['weight'],
                                          weight_basis=ln['weight_basis'], security_id=ln['sec'],
                                          issuer_id=issuer_id(cik) if cik else None,
                                          map_method=ln['map'][0], map_confidence=ln['map'][1],
                                          unmapped_reason='' if cik else ln.get('why', ''), source_id=src))
                if cik:
                    obs.setdefault((cik, ln['raw_name']), []).append(s['report_date'])
                    if ln['sec']:
                        sec_info.setdefault(ln['sec'], dict(issuer=cik, cls=ln['cls'], name=ln['raw_name'],
                                                            title='', isin='', lei='', cusip=ln['cusip_reported'],
                                                            src=src))
                        if ln['title']:
                            sec_info[ln['sec']].update(title=ln['title'], isin=ln['isin'], lei=ln['lei'], src=src)
                        elif ln['sec'].startswith('CUSIP'):
                            sec_info[ln['sec']]['src'] = src
        for (cik, name), dates in sorted(obs.items()):
            t['issuer_names'].append(dict(issuer_id=issuer_id(cik), name=name, name_kind='holdings_observed',
                                          first_seen=min(dates), last_seen=max(dates), source_id=''))
        for sec, d in sorted(sec_info.items()):
            cik = d['issuer']
            sic = info[cik]['sic'] if cik in info else ''
            t['securities'].append(dict(security_id=sec, issuer_id=issuer_id(cik), class_code=d['cls'],
                                        security_type=security_type(d['name'], d['title'], sic), name=d['name'],
                                        id_scheme='CUSIP' if sec.startswith('CUSIP:') else 'CIKCLS',
                                        notes='', source_id=d['src']))
            if sec.startswith('CUSIP:'):
                t['security_identifiers'].append(dict(security_id=sec, id_type='cusip', id_value=d['cusip'],
                                                      valid_from='', valid_to='', source_id=d['src']))
                if d['isin']:
                    t['security_identifiers'].append(dict(security_id=sec, id_type='isin', id_value=d['isin'],
                                                          valid_from='', valid_to='', source_id=d['src']))
                if d['lei']:
                    t['security_identifiers'].append(dict(security_id=sec, id_type='lei', id_value=d['lei'],
                                                          valid_from='', valid_to='', source_id=d['src']))
                tk = (self.v05_sec.get(d['cusip']) or {}).get('ticker', '')
                if tk:
                    t['security_identifiers'].append(dict(security_id=sec, id_type='ticker', id_value=tk,
                                                          valid_from='', valid_to='', source_id=d['src']))
            elif d['cls'] == 'COM' and cik in info:
                tks = [k for k, v in self.tickers.items() if v['cik'] == cik]
                if len(tks) == 1:
                    t['security_identifiers'].append(dict(security_id=sec, id_type='ticker', id_value=tks[0],
                                                          valid_from='', valid_to='', source_id=info[cik]['source_sha'] and
                                                          source_id(info[cik]['source_sha']) or ''))
        t['membership_spans'] = self.spans(primary, universe)
        t['review_items'] = []
        for n, r in enumerate(sorted(self.review, key=lambda r: (r['review_type'], r.get('subject_id', ''),
                                                                 r.get('snapshot_id', ''), r.get('detail', ''))), 1):
            t['review_items'].append(dict(review_id=n, review_type=r['review_type'], subject_id=r.get('subject_id', ''),
                                          snapshot_id=r.get('snapshot_id', ''), detail=r.get('detail', ''),
                                          source_id=''))
        t['_extra'] = dict(bridge=self.bridge_stats, universe=len(universe), trust_filings=len(trust_rows),
                           requests=self.store.stats.requests, retries=self.store.stats.retries,
                           failures=self.store.stats.failures, cache_reads=self.store.stats.cache_reads,
                           status_as_of=status_as_of, steps=self.steps)
        return t

    @staticmethod
    def spans(primary: list[dict], universe: list[str]) -> list[dict]:
        """Membership spans at issuer and security level. A subject that leaves and returns starts a new span.
        A quarter with no complete snapshot ends every span (its membership is unknown)."""
        presence: dict[tuple[str, str, str], list[int]] = {}
        for i, s in enumerate(primary):
            for ln in s['lines']:
                if not ln['issuer']:
                    continue
                presence.setdefault(('issuer', ln['issuer'], ln['issuer']), []).append(i)
                if ln['sec']:
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


def _quarter_index(d: str) -> int:
    return int(d[:4]) * 4 + (int(d[5:7]) - 1) // 3


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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.building')
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding='utf-8'))
        for name, cols in TABLE_COLUMNS.items():
            rows = tables.get(name, [])
            if not rows:
                continue
            sql = f'INSERT INTO {name} ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})'
            conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        conn.executemany('INSERT INTO build_log (step_no, ts_utc, step, detail) VALUES (?,?,?,?)',
                         [(i, utc_now_iso(), s, d) for i, (s, d) in enumerate(steps, 1)])
        conn.executemany('INSERT INTO meta (key, value) VALUES (?,?)',
                         [('schema_version', str(SCHEMA_VERSION)), ('built_at_utc', utc_now_iso())]
                         + [(k, str(v)) for k, v in sorted(meta.items())])
        conn.commit()
        digest = content_hash(conn)
        conn.execute('INSERT INTO meta (key, value) VALUES (?,?)', ('content_sha256', digest))
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
    digest = write_db(t, db_path, b.steps + [('write', f'{db_path.name}')],
                      dict(content_sha256_scope='all tables and views except meta and build_log'))
    counts = {k: len(v) for k, v in t.items() if isinstance(v, list)}
    return dict(content_sha256=digest, network_requests=store.stats.requests, retries=store.stats.retries,
                failures=store.stats.failures, offline=offline, counts=counts, extra=extra)
