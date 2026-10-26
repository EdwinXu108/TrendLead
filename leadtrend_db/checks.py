"""Verification of data/db/leadtrend_db_v1.sqlite (brief section 6). Writes data/db/exports/checks.json and checks.md.

Checks run on the database only, plus two archived references: the v05 snapshot_members copy (check 5) and an
offline rebuild that must reproduce the content hash (check 7). Nothing here changes the database.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from . import sources as S
from .build import BRIDGE_DATE, build  # build is used by check 7

STOCK_ROWS = (480, 520)
COVERAGE = (0.98, 1.0)
ENTER_EXIT_FLAG = 15
V05_TOL = 1e-9


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator='\n', extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def check_snapshots(conn: sqlite3.Connection, out: dict) -> None:
    snaps = _rows(conn, 'SELECT * FROM snapshots WHERE is_primary = 1 ORDER BY holdings_as_of')
    per = []
    for s in snaps:
        wsum = _rows(conn, 'SELECT COALESCE(SUM(weight),0) AS w FROM holdings WHERE snapshot_id = ?',
                     (s['snapshot_id'],))[0]['w']
        reasons = []
        if not STOCK_ROWS[0] <= s['row_count'] <= STOCK_ROWS[1]:
            reasons.append(f'stock rows {s["row_count"]} outside {STOCK_ROWS}')
        cov = s['coverage_ratio'] or 0.0
        if not COVERAGE[0] <= cov <= COVERAGE[1]:
            reasons.append(f'coverage {cov:.4f} outside {COVERAGE}')
        per.append(dict(snapshot_id=s['snapshot_id'], form=s['form'], holdings_as_of=s['holdings_as_of'],
                        available_at_utc=s['available_at_utc'], row_count=s['row_count'],
                        coverage_ratio=round(cov, 6), weight_sum=round(wsum, 6), completeness=s['completeness'],
                        weight_basis=_rows(conn, 'SELECT weight_basis FROM holdings WHERE snapshot_id = ? LIMIT 1',
                                           (s['snapshot_id'],))[0]['weight_basis'] if s['row_count'] else '',
                        reasons='; '.join(reasons)))
    out['1_snapshots'] = dict(primary_snapshots=len(snaps), out_of_range=[p for p in per if p['reasons']],
                              all=per)


def check_mapping(conn: sqlite3.Connection, out: dict, export: Path) -> None:
    per = []
    unmapped_rows = []
    for s in _rows(conn, 'SELECT snapshot_id, holdings_as_of, is_primary FROM snapshots ORDER BY holdings_as_of'):
        tot = _rows(conn, 'SELECT COUNT(*) AS n, SUM(issuer_id IS NOT NULL) AS m FROM holdings WHERE snapshot_id = ?',
                    (s['snapshot_id'],))[0]
        if not tot['n']:
            continue
        ratio = (tot['m'] or 0) / tot['n']
        per.append(dict(snapshot_id=s['snapshot_id'], as_of=s['holdings_as_of'], rows=tot['n'],
                        mapped=tot['m'] or 0, mapped_ratio=round(ratio, 6)))
        unmapped_rows += _rows(conn, 'SELECT snapshot_id, line_no, raw_name, raw_class_text, cusip_reported, shares, '
                                     'value_usd, unmapped_reason FROM holdings WHERE snapshot_id = ? AND '
                                     'issuer_id IS NULL ORDER BY line_no', (s['snapshot_id'],))
    _write_csv(export / 'checks_unmapped_rows.csv', unmapped_rows,
               ['snapshot_id', 'line_no', 'raw_name', 'raw_class_text', 'cusip_reported', 'shares', 'value_usd',
                'unmapped_reason'])
    out['2_mapping'] = dict(per_snapshot=per, unmapped_rows_total=len(unmapped_rows),
                            unmapped_rows_file='data/db/exports/checks_unmapped_rows.csv',
                            full_mapping_snapshots=sum(1 for p in per if p['mapped_ratio'] == 1.0),
                            snapshots=len(per))


def check_bridge(conn: sqlite3.Connection, out: dict, build_steps: dict) -> None:
    rows = _rows(conn, "SELECT review_type, subject_id, detail FROM review_items WHERE review_type LIKE 'bridge%'")
    nport = _rows(conn, "SELECT h.raw_name, h.shares, h.security_id, h.issuer_id FROM holdings h JOIN snapshots s "
                        "ON s.snapshot_id = h.snapshot_id WHERE s.form = 'NPORT-P' AND s.holdings_as_of = ?",
                  (BRIDGE_DATE,))
    ncsr = _rows(conn, "SELECT h.raw_name, h.shares, h.value_usd, h.security_id, h.issuer_id, h.map_method, "
                       "h.map_confidence FROM holdings h JOIN snapshots s ON s.snapshot_id = h.snapshot_id "
                       "WHERE s.form <> 'NPORT-P' AND s.holdings_as_of = ?", (BRIDGE_DATE,))
    shares_nport = {r['shares']: r for r in nport}
    same_shares = sum(1 for r in ncsr if r['shares'] in shares_nport)
    issuers_n = {r['issuer_id'] for r in nport if r['issuer_id']}
    issuers_c = {r['issuer_id'] for r in ncsr if r['issuer_id']}
    out['3_bridge_2019_12_31'] = dict(
        ncsr_rows=len(ncsr), nport_rows=len(nport), ncsr_rows_with_same_share_count_in_nport=same_shares,
        bridge_match_counts=build_steps.get('bridge_2019_12_31', ''),
        ncsr_issuers=len(issuers_c), nport_issuers=len(issuers_n),
        issuers_only_in_ncsr=sorted(issuers_c - issuers_n), issuers_only_in_nport=sorted(issuers_n - issuers_c),
        unmatched_or_ambiguous_rows=rows)


def check_entries(conn: sqlite3.Connection, out: dict) -> None:
    snaps = _rows(conn, 'SELECT snapshot_id, holdings_as_of FROM snapshots WHERE is_primary = 1 '
                        'ORDER BY holdings_as_of')
    members = {}
    for s in snaps:
        members[s['holdings_as_of']] = {r['issuer_id'] for r in _rows(
            conn, 'SELECT DISTINCT issuer_id FROM holdings WHERE snapshot_id = ? AND issuer_id IS NOT NULL',
            (s['snapshot_id'],))}
    dates = [s['holdings_as_of'] for s in snaps]
    flow = []
    for prev, cur in zip(dates, dates[1:]):
        enter = members[cur] - members[prev]
        leave = members[prev] - members[cur]
        flow.append(dict(quarter=cur, previous=prev, entering=len(enter), leaving=len(leave),
                         flagged=len(enter) > ENTER_EXIT_FLAG or len(leave) > ENTER_EXIT_FLAG,
                         entering_ids=sorted(enter), leaving_ids=sorted(leave)))
    out['4_entries_exits'] = dict(quarters=len(flow), median_entering=sorted(f['entering'] for f in flow)[len(flow) // 2],
                                  max_entering=max(f['entering'] for f in flow),
                                  max_leaving=max(f['leaving'] for f in flow),
                                  flagged=[{k: v for k, v in f.items() if k not in ('entering_ids', 'leaving_ids')}
                                           for f in flow if f['flagged']],
                                  all=[{k: v for k, v in f.items() if k not in ('entering_ids', 'leaving_ids')}
                                       for f in flow])


def check_v05(conn: sqlite3.Connection, out: dict, raw_dir: Path) -> None:
    path = raw_dir / 'reference' / 'v05' / 'snapshot_members.csv'
    with path.open(encoding='utf-8', newline='') as f:
        v05 = [r for r in csv.DictReader(f) if r['snapshot_id'].startswith('NPORT_')]
    # v05 stores the N-PORT pctVal (percent of net assets); the database stores the fraction (pctVal / 100)
    v05_map = {(r['snapshot_id'], r['security_id']): float(r['weight'] or 0) / 100.0 for r in v05}
    db = {}
    for r in _rows(conn, "SELECT h.snapshot_id, h.security_id, h.weight FROM holdings h JOIN snapshots s "
                         "ON s.snapshot_id = h.snapshot_id WHERE s.form = 'NPORT-P' AND s.is_primary = 1 "
                         "AND h.security_id IS NOT NULL"):
        db[(r['snapshot_id'], r['security_id'])] = r['weight']
    only_v05 = sorted(set(v05_map) - set(db))
    only_db = sorted(set(db) - set(v05_map))
    diff = [(k, v05_map[k], db[k]) for k in sorted(set(v05_map) & set(db)) if abs(v05_map[k] - db[k]) > V05_TOL]
    snaps_v05 = {r['snapshot_id'] for r in v05}
    snaps_db = {k[0] for k in db}
    out['5_v05_agreement'] = dict(
        v05_rows=len(v05_map), db_rows=len(db), snapshots_v05=len(snaps_v05), snapshots_db=len(snaps_db),
        rows_only_in_v05=len(only_v05), rows_only_in_db=len(only_db), weight_differences=len(diff),
        tolerance=V05_TOL, examples_only_v05=[list(k) for k in only_v05[:20]],
        examples_only_db=[list(k) for k in only_db[:20]],
        examples_weight=[[list(k), a, b] for k, a, b in diff[:20]])


def check_status(conn: sqlite3.Connection, out: dict, export: Path) -> None:
    leavers = _rows(conn, "SELECT DISTINCT m.issuer_id FROM membership_spans m WHERE m.level = 'issuer' "
                          "AND m.next_absent_as_of <> ''")
    ids = {r['issuer_id'] for r in leavers}
    status = {r['issuer_id']: r for r in _rows(conn, 'SELECT * FROM listing_status')}
    missing = sorted(i for i in ids if i not in status)
    by_status = {}
    for i in ids:
        if i in status:
            by_status[status[i]['status']] = by_status.get(status[i]['status'], 0) + 1
    delisted_reasons = {}
    for i in ids:
        if i in status and status[i]['status'] == 'delisted':
            delisted_reasons[status[i]['reason']] = delisted_reasons.get(status[i]['reason'], 0) + 1
    review = [r for r in status.values() if r['needs_review'] or r['status'] == 'unknown']
    out['6_status'] = dict(leaving_issuers=len(ids), with_status=len(ids) - len(missing), missing=missing,
                           by_status=dict(sorted(by_status.items())), delisted_by_reason=dict(sorted(delisted_reasons.items())),
                           needs_review_or_unknown=len(review))
    _write_csv(export / 'checks_status_review.csv', review,
               ['issuer_id', 'status', 'status_as_of', 'delisting_date', 'reason', 'evidence', 'method',
                'needs_review'])


def check_rebuild(out: dict, digest: str, work_db: Path) -> None:
    result = build(offline=True, log=lambda m: None, db_path=work_db)
    out['7_rebuild'] = dict(content_sha256_first=digest, content_sha256_offline=result['content_sha256'],
                            identical=result['content_sha256'] == digest,
                            offline_network_requests=result['network_requests'])


def run_checks(log=print, rebuild: bool = True) -> dict:
    export = S.EXPORT_DIR
    export.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f'file:{S.DB_PATH}?mode=ro', uri=True)
    try:
        out: dict = dict(database=str(S.DB_PATH.relative_to(S.ROOT)).replace('\\', '/'))
        meta = {r['key']: r['value'] for r in _rows(conn, 'SELECT key, value FROM meta')}
        out['content_sha256'] = meta.get('content_sha256')
        steps = {r['step']: r['detail'] for r in _rows(conn, 'SELECT step, detail FROM build_log')}
        check_snapshots(conn, out)
        check_mapping(conn, out, export)
        check_bridge(conn, out, steps)
        check_entries(conn, out)
        check_v05(conn, out, S.RAW_DIR)
        check_status(conn, out, export)
    finally:
        conn.close()
    if rebuild:
        check_rebuild(out, out['content_sha256'], S.ROOT / 'work' / 'db_rebuild' / 'leadtrend_db_v1.sqlite')
    (export / 'checks.json').write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                                        encoding='utf-8')
    log(f'checks written to {export / "checks.json"}')
    return out
