"""Verification of data/db/leadtrend_db_v1.sqlite (brief section 6 and the repair round, section 5).

Writes data/db/exports/checks.json and checks.md (or the path given). Reads the database read-only, the archived v05
copies (raw/reference/v05) and the archive manifest. The offline rebuild writes to work/ and must reproduce the
content hash with zero network requests. Nothing here changes the database.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path

from . import sources as S
from .build import BRIDGE_DATE, build

STOCK_ROWS = (480, 520)
COVERAGE = (0.98, 1.0)
ENTER_EXIT_FLAG = 15
WEIGHT_TOL = 1e-9            # weights are float ratios of values; their sum is 1 to ~1e-15
V05_TOL = 1e-9
RESERVED = ('prices_daily', 'corporate_actions', 'price_coverage', 'index_changes')
EXCLUDED = ('right', 'warrant', 'cvr', 'etf', 'other')


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


def check_archive(out: dict, baseline: Path) -> None:
    """The manifest is append-only: the baseline manifest (before the repair round) must be a prefix of the current."""
    current = (S.RAW_DIR / 'manifest.jsonl').read_text(encoding='utf-8').splitlines()
    if not baseline.exists():
        out['0_archive'] = dict(baseline_found=False, lines=len(current))
        return
    old = baseline.read_text(encoding='utf-8').splitlines()
    out['0_archive'] = dict(baseline_found=True, baseline_lines=len(old), current_lines=len(current),
                            baseline_is_prefix=current[:len(old)] == old,
                            current_manifest_sha256=hashlib.sha256('\n'.join(current).encode()).hexdigest()[:16])


def check_snapshots(conn, out: dict) -> None:
    snaps = _rows(conn, 'SELECT * FROM snapshots ORDER BY holdings_as_of, form')
    per = []
    for s in snaps:
        w = _rows(conn, 'SELECT COALESCE(SUM(weight),0) AS w, COUNT(weight) AS n FROM holdings '
                        'WHERE snapshot_id = ? AND weight IS NOT NULL', (s['snapshot_id'],))[0]
        reasons = []
        if s['is_primary'] and not STOCK_ROWS[0] <= s['row_count'] <= STOCK_ROWS[1]:
            reasons.append(f'stock rows {s["row_count"]} outside {STOCK_ROWS}')
        cov = s['coverage_ratio'] or 0.0
        if s['is_primary'] and not COVERAGE[0] <= cov <= COVERAGE[1]:
            reasons.append(f'coverage {cov:.4f} outside {COVERAGE}')
        if s['is_primary'] and s['reported_common_stock_total_usd'] is not None:
            diff = abs(s['stock_total_usd'] + 0 - s['reported_common_stock_total_usd'])
            if diff > 1e-3 * s['reported_common_stock_total_usd']:
                reasons.append(f'stock total differs from reported total by {diff:.0f}')
        if w['n'] and abs(w['w'] - 1.0) > WEIGHT_TOL:
            reasons.append(f'normalised weights sum to {w["w"]!r}, not 1 (tolerance {WEIGHT_TOL})')
        per.append(dict(snapshot_id=s['snapshot_id'], form=s['form'], holdings_as_of=s['holdings_as_of'],
                        is_primary=s['is_primary'], completeness=s['completeness'], reconciled=s['reconciled'],
                        row_count=s['row_count'], stock_total_usd=s['stock_total_usd'],
                        reported_common_stock_total_usd=s['reported_common_stock_total_usd'],
                        coverage_ratio=round(cov, 6), weight_count=w['n'], weight_sum=w['w'],
                        start_rule=s['start_rule'], reasons='; '.join(reasons)))
    out['1_snapshots'] = dict(primary=sum(1 for p in per if p['is_primary']), snapshots=len(per),
                              out_of_range=[p for p in per if p['reasons']], all=per,
                              primary_quarters_complete=sorted(p['holdings_as_of'] for p in per if p['is_primary']))


def check_mapping(conn, out: dict, export: Path) -> None:
    per, unmapped = [], []
    for s in _rows(conn, 'SELECT snapshot_id, holdings_as_of, form FROM snapshots ORDER BY holdings_as_of, form'):
        tot = _rows(conn, "SELECT COUNT(*) AS n, SUM(issuer_id IS NOT NULL) AS m FROM holdings "
                          "WHERE snapshot_id = ? AND line_kind = 'stock'", (s['snapshot_id'],))[0]
        if not tot['n']:
            continue
        per.append(dict(snapshot_id=s['snapshot_id'], as_of=s['holdings_as_of'], stock_lines=tot['n'],
                        mapped=tot['m'] or 0, mapped_ratio=round((tot['m'] or 0) / tot['n'], 6)))
        unmapped += _rows(conn, "SELECT snapshot_id, line_no, raw_name, raw_class_text, cusip_reported, isin_reported, "
                                "shares, value_usd, unmapped_reason FROM holdings WHERE snapshot_id = ? AND "
                                "line_kind = 'stock' AND issuer_id IS NULL ORDER BY line_no", (s['snapshot_id'],))
    reasons = {}
    for u in unmapped:
        key = u['unmapped_reason'].split(':')[0][:60]
        reasons[key] = reasons.get(key, 0) + 1
    _write_csv(export / 'checks_unmapped_rows.csv', unmapped,
               ['snapshot_id', 'line_no', 'raw_name', 'raw_class_text', 'cusip_reported', 'isin_reported', 'shares',
                'value_usd', 'unmapped_reason'])
    out['2_mapping'] = dict(snapshots=len(per), full_mapping_snapshots=sum(1 for p in per if p['mapped_ratio'] == 1.0),
                            unmapped_stock_lines=len(unmapped), unmapped_by_reason=dict(sorted(reasons.items())),
                            min_ratio=min(p['mapped_ratio'] for p in per), per_snapshot=per,
                            unmapped_rows_file='data/db/exports/checks_unmapped_rows.csv')


def check_bridge(conn, out: dict, steps: dict) -> None:
    rows = _rows(conn, "SELECT review_type, subject_id, snapshot_id, detail FROM review_items "
                       "WHERE review_type LIKE 'bridge%'")
    ncsr = _rows(conn, "SELECT h.raw_name, h.shares, h.value_usd, h.security_id, h.issuer_id, h.map_method "
                       "FROM holdings h JOIN snapshots s ON s.snapshot_id = h.snapshot_id "
                       "WHERE s.form <> 'NPORT-P' AND s.holdings_as_of = ? AND h.line_kind = 'stock'", (BRIDGE_DATE,))
    nport = _rows(conn, "SELECT h.raw_name, h.shares, h.security_id, h.issuer_id FROM holdings h JOIN snapshots s "
                        "ON s.snapshot_id = h.snapshot_id WHERE s.form = 'NPORT-P' AND s.holdings_as_of = ? "
                        "AND h.line_kind = 'stock'", (BRIDGE_DATE,))
    issuers_c = {r['issuer_id'] for r in ncsr if r['issuer_id']}
    issuers_n = {r['issuer_id'] for r in nport if r['issuer_id']}
    out['3_bridge_2019_12_31'] = dict(
        ncsr_stock_lines=len(ncsr), nport_stock_lines=len(nport), steps=steps.get('bridge_2019_12_31', ''),
        unmatched_or_ambiguous=rows, issuers_only_in_ncsr=sorted(issuers_c - issuers_n),
        issuers_only_in_nport=sorted(issuers_n - issuers_c),
        ncsr_lines_without_issuer=sum(1 for r in ncsr if not r['issuer_id']))


def check_entries(conn, out: dict) -> None:
    snaps = _rows(conn, 'SELECT snapshot_id, holdings_as_of FROM snapshots WHERE is_primary = 1 ORDER BY holdings_as_of')
    members = {s['holdings_as_of']: {r['issuer_id'] for r in _rows(
        conn, "SELECT DISTINCT issuer_id FROM holdings WHERE snapshot_id = ? AND issuer_id IS NOT NULL AND "
              "line_kind = 'stock'", (s['snapshot_id'],))} for s in snaps}
    dates = [s['holdings_as_of'] for s in snaps]
    flow = []
    for prev, cur in zip(dates, dates[1:]):
        enter, leave = members[cur] - members[prev], members[prev] - members[cur]
        flow.append(dict(quarter=cur, previous=prev, entering=len(enter), leaving=len(leave),
                         flagged=len(enter) > ENTER_EXIT_FLAG or len(leave) > ENTER_EXIT_FLAG))
    out['4_entries_exits'] = dict(transitions=len(flow), median_entering=sorted(f['entering'] for f in flow)[len(flow) // 2],
                                  max_entering=max(f['entering'] for f in flow), max_leaving=max(f['leaving'] for f in flow),
                                  flagged=[f for f in flow if f['flagged']])


def check_v05(conn, out: dict, raw_dir: Path, export: Path | None = None) -> None:
    path = raw_dir / 'reference' / 'v05' / 'snapshot_members.csv'
    with path.open(encoding='utf-8', newline='') as f:
        v05 = [r for r in csv.DictReader(f) if r['snapshot_id'].startswith('NPORT_')]
    v = {(r['snapshot_id'], r['security_id']): float(r['weight'] or 0) for r in v05}      # percent of net assets
    db_rows = _rows(conn, "SELECT h.snapshot_id, h.security_id, h.pct_val_percent, h.weight, h.value_usd, s.security_type "
                          "FROM holdings h JOIN securities s ON s.security_id = h.security_id "
                          "JOIN snapshots n ON n.snapshot_id = h.snapshot_id "
                          "WHERE n.form = 'NPORT-P' AND n.is_primary = 1 AND h.line_kind = 'stock'")
    db = {(r['snapshot_id'], r['security_id']): r for r in db_rows}
    raw_diff = [(k, v[k], db[k]['pct_val_percent']) for k in sorted(set(v) & set(db))
                if abs(v[k] - (db[k]['pct_val_percent'] or 0)) > V05_TOL]
    # normalised basis: both sides over the same set of securities, each divided by its own stock total
    # both sides normalised over the same securities of each snapshot (stock lines of included types that appear in
    # v05 and in the database): database on its value, v05 on its pctVal; v05 has no other basis to compare with
    norm_diff, max_norm = [], 0.0
    for sid in sorted({k[0] for k in db}):
        keys = [k for k in db if k[0] == sid and k in v and db[k]['weight'] is not None]
        tv = sum(db[k]['value_usd'] for k in keys)
        tp = sum(v[k] for k in keys)
        for k in keys:
            d = abs(db[k]['value_usd'] / tv - v[k] / tp)
            max_norm = max(max_norm, d)
            if d > V05_TOL:
                norm_diff.append((k, v[k] / tp, db[k]['value_usd'] / tv))
    only_v05 = sorted(set(v) - set(db))
    only_db = sorted(set(db) - set(v))
    if export is not None:
        v05_only_rows = [dict(snapshot_id=k[0], security_id=k[1], v05_pct=v[k], db_reason=_v05_only_status(conn, [k]))
                         for k in only_v05]
        db_only_rows = [dict(snapshot_id=k[0], security_id=k[1], security_type=db[k]['security_type'],
                             pct_val_percent=db[k]['pct_val_percent']) for k in only_db]
        _write_csv(export / 'checks_v05_only.csv', v05_only_rows,
                   ['snapshot_id', 'security_id', 'v05_pct', 'db_reason'])
        _write_csv(export / 'checks_v05_db_only.csv', db_only_rows,
                   ['snapshot_id', 'security_id', 'security_type', 'pct_val_percent'])
    out['5_v05_agreement'] = dict(
        v05_rows=len(v), db_stock_rows_with_security=len(db),
        raw_pct_basis=dict(compared=len(set(v) & set(db)), differences=len(raw_diff), tolerance=V05_TOL,
                           examples=[[list(k), a, b] for k, a, b in raw_diff[:10]]),
        normalised_basis=dict(compared=len(set(v) & set(db)), differences=len(norm_diff), max_abs_difference=max_norm, tolerance=V05_TOL,
                              examples=[[list(k), a, b] for k, a, b in norm_diff[:10]]),
        only_in_v05=len(only_v05), only_in_db=len(only_db),
        only_in_v05_by_db_status=_v05_only_status(conn, only_v05),
        only_in_db_examples=[[list(k), db[k]['security_type']] for k in only_db[:20]])


def _v05_only_status(conn, keys: list) -> dict:
    out = {}
    for sid, sec in keys:
        r = _rows(conn, 'SELECT h.issuer_id, h.unmapped_reason, h.line_kind FROM holdings h WHERE h.snapshot_id = ? '
                        'AND (h.security_id = ? OR h.cusip_reported = ?) LIMIT 1', (sid, sec, sec[6:]))
        reason = r[0]['unmapped_reason'][:40] if r and r[0]['issuer_id'] is None else (
            'present with other line kind' if r else 'no DB line')
        out[reason] = out.get(reason, 0) + 1
    return out


def check_status(conn, out: dict, export: Path) -> None:
    leavers = {r['issuer_id'] for r in _rows(conn, "SELECT DISTINCT issuer_id FROM membership_spans "
                                                   "WHERE level = 'issuer' AND next_absent_as_of <> ''")}
    status = {r['issuer_id']: r for r in _rows(conn, 'SELECT * FROM listing_status')}
    review = [r for r in status.values() if r['needs_review'] or r['status'] == 'unknown']
    by_status, reasons = {}, {}
    for i in leavers:
        st = status[i]['status']
        by_status[st] = by_status.get(st, 0) + 1
        if st == 'delisted':
            reasons[status[i]['reason']] = reasons.get(status[i]['reason'], 0) + 1
    contradictions = [r for r in review if r['status'] == 'delisted' and 'still in the latest' in r['evidence']]
    _write_csv(export / 'checks_status_review.csv', review,
               ['issuer_id', 'status', 'status_as_of', 'delisting_date', 'reason', 'evidence', 'method', 'needs_review'])
    links = _rows(conn, 'SELECT predecessor_issuer_id, successor_issuer_id, confidence, via_override, effective_date '
                        'FROM issuer_links')
    out['6_status'] = dict(leaving_issuers=len(leavers), with_status=sum(1 for i in leavers if i in status),
                           missing=sorted(i for i in leavers if i not in status), by_status=by_status,
                           delisted_by_reason=reasons, needs_review_or_unknown=len(review),
                           contradicting_latest_holdings=[[r['issuer_id'], r['evidence'][:120]] for r in contradictions],
                           links=links, successor_candidates=_rows(conn, "SELECT COUNT(*) AS n FROM review_items "
                                                                      "WHERE review_type = 'successor_candidate'")[0]['n'])


def check_types(conn, out: dict) -> None:
    types = _rows(conn, 'SELECT security_type, COUNT(*) AS n FROM securities GROUP BY 1 ORDER BY 1')
    eligible = _rows(conn, 'SELECT eligible_v1, COUNT(*) AS n FROM universe_v1 GROUP BY 1')
    no_ticker = _rows(conn, "SELECT COUNT(*) AS n FROM universe_v1 WHERE eligible_v1 = 1 AND (ticker IS NULL OR ticker = '')")[0]['n']
    excluded_lines = _rows(conn, 'SELECT security_type, COUNT(*) AS n FROM holdings h JOIN securities s USING(security_id) '
                                 'WHERE h.line_kind = "stock" GROUP BY 1')
    unidentified = _rows(conn, "SELECT COUNT(*) AS n FROM holdings WHERE security_id IS NULL AND line_kind = 'stock' "
                               "AND snapshot_id LIKE 'NPORT%'")[0]['n']
    out['7_types'] = dict(securities_by_type={r['security_type']: r['n'] for r in types},
                          universe_eligible={str(r['eligible_v1']): r['n'] for r in eligible},
                          eligible_without_ticker=no_ticker,
                          excluded_type_lines_by_type={r['security_type']: r['n'] for r in excluded_lines},
                          nport_lines_unmapped_identity=unidentified)


def check_integrity(conn, out: dict) -> None:
    fk = conn.execute('PRAGMA foreign_key_check').fetchall()
    reserved = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in RESERVED}
    blank_ids = conn.execute("SELECT COUNT(*) FROM security_identifiers WHERE id_value = ''").fetchone()[0]
    meta = {r['key']: r['value'] for r in _rows(conn, 'SELECT key, value FROM meta')}
    out['8_integrity'] = dict(foreign_key_violations=len(fk), reserved_table_rows=reserved,
                              blank_identifier_values=blank_ids,
                              meta_keys_present=all(k in meta for k in ('build_id', 'code_commit', 'raw_manifest_sha256')),
                              meta_build_id=meta.get('build_id'), meta_code_commit=meta.get('code_commit'),
                              meta_raw_manifest_sha256=meta.get('raw_manifest_sha256'),
                              content_sha256=meta.get('content_sha256'))


def check_rebuild(out: dict, digest: str, work_db: Path) -> None:
    result = build(offline=True, log=lambda m: None, db_path=work_db)
    out['9_rebuild'] = dict(content_sha256_first=digest, content_sha256_offline=result['content_sha256'],
                            identical=result['content_sha256'] == digest,
                            offline_network_requests=result['network_requests'])


def run_checks(log=print, rebuild: bool = True, db_path: Path = S.DB_PATH, export: Path | None = None,
               rebuild_db: Path | None = None, baseline: Path | None = None) -> dict:
    export = export or S.EXPORT_DIR
    export.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        out: dict = dict(database=str(db_path))
        meta = {r['key']: r['value'] for r in _rows(conn, 'SELECT key, value FROM meta')}
        out['content_sha256'] = meta.get('content_sha256')
        steps = {r['step']: r['detail'] for r in _rows(conn, 'SELECT step, detail FROM build_log')}
        check_archive(out, baseline or S.ROOT / 'work' / 'fix' / 'baseline' / 'manifest.jsonl')
        check_snapshots(conn, out)
        check_mapping(conn, out, export)
        check_bridge(conn, out, steps)
        check_entries(conn, out)
        check_v05(conn, out, S.RAW_DIR, export)
        check_status(conn, out, export)
        check_types(conn, out)
        check_integrity(conn, out)
    finally:
        conn.close()
    if rebuild:
        check_rebuild(out, out['content_sha256'], rebuild_db or S.ROOT / 'work' / 'db_rebuild' / 'leadtrend_db_v1.sqlite')
    (export / 'checks.json').write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True, default=str) + '\n',
                                        encoding='utf-8')
    log(f'checks written to {export / "checks.json"}')
    return out
