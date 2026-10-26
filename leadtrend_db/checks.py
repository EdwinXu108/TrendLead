"""Checks of leadtrend_db v2 (rule_version top200_v1). run_checks() returns a result dict and a list of failures; the CLI
exits non-zero when any check fails, so a failed check can never be mistaken for a passed build.

The ranking is recomputed here with SQL from the holdings table, not from the Python ranking module, so that the two
paths are independent: the database's company amounts, ranks and boundary values must agree with the SQL recount for
every canonical quarter.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path

from . import rules as R
from . import sources as S

PRESENT_META = ('content_sha256', 'schema_version', 'rule_version', 'build_id', 'code_commit', 'raw_manifest_sha256',
                'status_cutoff', 'build_cutoff_utc')
RESERVED = ('prices_daily', 'corporate_actions', 'price_coverage', 'index_changes')


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    return conn.execute(sql, args).fetchall()


def check_archive(out: dict, baseline: Path, manifest: Path) -> list[str]:
    fails = []
    recs = [json.loads(l) for l in manifest.read_text(encoding='utf-8').splitlines() if l.strip()]
    bad = []
    for r in recs:
        p = S.RAW_DIR / r['path']
        if not p.exists():
            bad.append(r['path'])
            continue
        data = p.read_bytes()
        if len(data) != r['bytes'] or hashlib.sha256(data).hexdigest() != r['sha256']:
            bad.append(r['path'])
    out['archive'] = dict(manifest_lines=len(recs), files_verified=len(recs) - len(bad), bad_files=bad[:20])
    if bad:
        fails.append(f'archive: {len(bad)} raw files missing or not matching the manifest')
    if baseline.exists():
        base = [l for l in baseline.read_text(encoding='utf-8').splitlines() if l.strip()]
        cur = manifest.read_text(encoding='utf-8').splitlines()[:len(base)]
        out['archive']['baseline_lines'] = len(base)
        out['archive']['baseline_is_prefix'] = cur == [l for l in base]
        if cur != base:
            fails.append('archive: the manifest does not begin with the round-1 baseline (lines were rewritten)')
    return fails


def check_snapshots(conn, out: dict) -> list[str]:
    fails = []
    rows = _rows(conn, "select snapshot_id, form, holdings_as_of, completeness, reconciled, residual_usd, "
                       "rounding_bound_usd, unparsed_rows, row_count from snapshots")
    htm_bad = [r[0] for r in rows if r[1] != 'NPORT-P' and (r[5] is None or abs(r[5]) > r[6] or r[7])]
    out['snapshots'] = dict(count=len(rows), htm_not_reconciled=htm_bad,
                            primary=_rows(conn, 'select count(*), sum(completeness="full") from snapshots where is_primary=1')[0])
    if htm_bad:
        fails.append(f'snapshots: {len(htm_bad)} htm schedules do not reconcile within rounding')
    quarters = [r[0] for r in _rows(conn, 'select distinct holdings_as_of from snapshots where is_primary=1 order by 1')]
    gap = []
    for a, b in zip(quarters, quarters[1:]):
        if b[:4] + b[5:7] != '' and _next_quarter(a) != b:
            gap.append((a, b))
    out['snapshots']['quarters'] = len(quarters)
    out['snapshots']['first_quarter'] = quarters[0] if quarters else ''
    out['snapshots']['last_quarter'] = quarters[-1] if quarters else ''
    if gap:
        fails.append(f'snapshots: quarters not continuous {gap[:3]}')
    if quarters and quarters[0] != R.EARLIEST_QUARTER:
        fails.append('snapshots: the first quarter is not 2013-12-31')
    return fails


def _next_quarter(d: str) -> str:
    y, m = int(d[:4]), int(d[5:7]) + 3
    if m > 12:
        y, m = y + 1, m - 12
    from datetime import date, timedelta
    return (date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)).isoformat()


def check_ranking(conn, out: dict) -> list[str]:
    """Independent SQL recount of every canonical quarter: company amounts (cents), order, the 200th and 201st."""
    fails = []
    canon = _rows(conn, "select version_id, report_period from pool_versions where is_canonical=1 order by 2")
    mismatches = []
    boundary = {}
    for vid, period in canon:
        sql_rows = _rows(conn, """
            select h.issuer_id, sum(cast(round(h.value_usd*100) as integer)) as cents
            from holdings h join snapshots s on s.snapshot_id=h.snapshot_id
            where s.snapshot_id=? and h.line_kind='stock' and h.issuer_id is not null
              and h.security_type in ('common','adr')
            group by h.issuer_id
            order by cents desc, h.issuer_id""", (vid,))
        db_rows = _rows(conn, "select issuer_id, amount_cents from company_ranks where version_id=? order by rank", (vid,))
        if [r[0] for r in sql_rows] != [r[0] for r in db_rows] or [r[1] for r in sql_rows] != [r[1] for r in db_rows]:
            mismatches.append(vid)
        top = _rows(conn, "select issuer_id, amount_cents from top200_companies where version_id=? order by rank", (vid,))
        if [t[0] for t in top] != [r[0] for r in sql_rows[:R.TOP_N]]:
            mismatches.append(vid + ':top200')
        if len(sql_rows) > R.TOP_N:
            boundary[vid] = (sql_rows[R.TOP_N - 1][1], sql_rows[R.TOP_N][1])
    out['ranking'] = dict(canonical_quarters=len(canon), independent_mismatches=mismatches[:20],
                          boundary_cents_200_201={k: list(v) for k, v in list(boundary.items())[:3]})
    if mismatches:
        fails.append(f'ranking: {len(mismatches)} quarters differ from the independent SQL recount')
    return fails


def check_bound(conn, out: dict) -> list[str]:
    """The unresolved candidate amounts recomputed from holdings (candidate_issuers) match company_ranks."""
    fails = []
    bad = []
    for vid, in _rows(conn, "select version_id from pool_versions where is_canonical=1"):
        cands = _rows(conn, """select h.candidate_issuers, h.value_usd from holdings h join snapshots s using(snapshot_id)
                               where s.snapshot_id=? and h.line_kind='stock' and h.issuer_id is null
                                 and h.candidate_issuers <> '' and h.security_type in ('common','adr','unknown')""", (vid,))
        per: dict[str, int] = {}
        for text, v in cands:
            for c in text.split():
                per[c] = per.get(c, 0) + int(round(v * 100))
        db = dict(_rows(conn, 'select issuer_id, candidate_bound_cents from company_ranks where version_id=?', (vid,)))
        for c, cents in per.items():
            key = f'CIK:{c}'
            if key in db and db[key] != cents:
                bad.append((vid, key))
    out['bound'] = dict(mismatches=len(bad))
    if bad:
        fails.append(f'bound: {len(bad)} candidate amounts differ from the SQL recount')
    return fails


def check_security_boundary(conn, out: dict) -> list[str]:
    fails = []
    bad_types = _rows(conn, "select count(*) from top200_securities where candidate_eligible=1 and security_type not in ('common','adr')")
    brka = _rows(conn, "select count(*) from top200_securities where candidate_eligible=1 and security_id like 'CUSIP:084670108%'")
    ended_elig = _rows(conn, """select count(*) from top200_securities t join securities s using(security_id)
                                where t.candidate_eligible=1 and s.notes like '%ended%'""")
    strat_brka = _rows(conn, "select count(*) from strategy_securities where strategy_eligible=1 and security_id like 'CUSIP:084670108%'")
    out['security_boundary'] = dict(eligible_non_common=bad_types[0][0], brka_candidates=brka[0][0],
                                    ended_candidates=ended_elig[0][0], strategy_brka=strat_brka[0][0])
    if bad_types[0][0] or brka[0][0] or strat_brka[0][0]:
        fails.append('security boundary: an ineligible security is a candidate')
    return fails


def check_strategy(conn, out: dict) -> list[str]:
    fails = []
    delisted_ok = _rows(conn, """select count(*) from strategy_companies where strategy_eligible=1 and current_status='delisted'""")
    no_reason = _rows(conn, """select count(*) from strategy_companies where strategy_eligible=0 and blocked_reason='' and excluded_reason=''""")
    unknown_ok = _rows(conn, """select count(*) from strategy_companies where strategy_eligible=1 and current_status='unknown'""")
    n_hist = _rows(conn, 'select count(*) from top200_companies where version_id in (select version_id from pool_versions where is_canonical=1)')
    n_strat = _rows(conn, 'select count(*) from strategy_companies where version_id in (select version_id from pool_versions where is_canonical=1)')
    out['strategy'] = dict(historical_rows=n_hist[0][0], strategy_rows=n_strat[0][0],
                           eligible_delisted=delisted_ok[0][0], unexplained_exclusions=no_reason[0][0],
                           eligible_unknown=unknown_ok[0][0])
    if delisted_ok[0][0] or no_reason[0][0] or unknown_ok[0][0]:
        fails.append('strategy: a delisted or unknown company is eligible, or an exclusion has no reason')
    if n_hist[0][0] != n_strat[0][0]:
        fails.append('strategy: the strategy list does not keep one row per historical member')
    return fails


def check_v05(conn, out: dict, v05_members: Path) -> None:
    """Informational comparison with the v05 snapshot members of the same N-PORT snapshots: the raw pctVal of each
    CUSIP (percent of net assets) against v05's weight, which is the same percent; securities only on one side are
    counted. Not a pass or fail gate: v05 has known defects."""
    if not v05_members.exists():
        out['v05'] = dict(status='v05 members not archived')
        return
    members: dict[str, dict[str, float]] = {}
    with v05_members.open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            members.setdefault(r['snapshot_id'], {})[r['security_id']] = float(r['weight'] or 0.0)
    summary = dict(snapshots=0, common=0, only_v05=0, only_db=0, raw_weight_differences=0, max_abs_difference=0.0)
    for sid, v5 in sorted(members.items()):
        if not _rows(conn, 'select 1 from snapshots where snapshot_id=?', (sid,)):
            continue
        db = {r[0]: r[1] for r in _rows(conn, """select security_id, pct_val_percent from holdings
                                                 where snapshot_id=? and line_kind='stock' and security_id is not null
                                                   and pct_val_percent is not null""", (sid,))}
        summary['snapshots'] += 1
        common = set(db) & set(v5)
        summary['common'] += len(common)
        summary['only_v05'] += len(set(v5) - set(db))
        summary['only_db'] += len(set(db) - set(v5))
        for k in common:
            diff = abs(db[k] - v5[k])          # both in percent of net assets (v05 weight = N-PORT pctVal)
            summary['max_abs_difference'] = max(summary['max_abs_difference'], diff)
            if diff > 1e-9:
                summary['raw_weight_differences'] += 1
    summary['status'] = 'informational'
    out['v05'] = summary


def check_integrity(conn, out: dict) -> list[str]:
    fails = []
    fk = _rows(conn, 'pragma foreign_key_check')
    ic = _rows(conn, 'pragma integrity_check')[0][0]
    meta = dict(_rows(conn, 'select key, value from meta'))
    missing = [k for k in PRESENT_META if k not in meta]
    reserved = {t: _rows(conn, f'select count(*) from {t}')[0][0] for t in RESERVED}
    blank = _rows(conn, "select count(*) from security_identifiers where id_value is null or id_value=''")[0][0]
    out['integrity'] = dict(foreign_key_violations=len(fk), integrity_check=ic, missing_meta=missing,
                            reserved_rows=reserved, blank_identifiers=blank)
    if fk:
        fails.append(f'integrity: {len(fk)} foreign key violations')
    if ic != 'ok':
        fails.append('integrity: integrity_check failed')
    if missing:
        fails.append(f'integrity: meta keys missing {missing}')
    if any(reserved.values()):
        fails.append('integrity: a reserved table is not empty')
    if blank:
        fails.append('integrity: blank identifiers stored')
    return fails


def check_rebuild(out: dict, digest: str, work_db: Path) -> list[str]:
    from .build import build
    res = build(offline=True, log=lambda _: None, db_path=work_db)
    out['rebuild'] = dict(content_sha256=res['content_sha256'], identical=res['content_sha256'] == digest,
                          network_requests=res['network_requests'])
    fails = []
    if res['content_sha256'] != digest:
        fails.append('rebuild: offline rebuild content hash differs')
    if res['network_requests']:
        fails.append('rebuild: the offline rebuild made network requests')
    return fails


def run_checks(db_path: Path = S.DB_PATH, rebuild: bool = True, work_db: Path | None = None,
               baseline: Path | None = None, log=print) -> tuple[dict, list[str]]:
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    out: dict = {}
    fails: list[str] = []
    fails += check_archive(out, baseline or Path('work/fix_round2/baseline/manifest.jsonl'), S.RAW_DIR / 'manifest.jsonl')
    fails += check_snapshots(conn, out)
    fails += check_ranking(conn, out)
    fails += check_bound(conn, out)
    fails += check_security_boundary(conn, out)
    fails += check_strategy(conn, out)
    fails += check_integrity(conn, out)
    check_v05(conn, out, S.RAW_DIR / 'reference' / 'v05' / 'snapshot_members.csv')
    digest = _rows(conn, "select value from meta where key='content_sha256'")[0][0]
    out['content_sha256'] = digest
    conn.close()
    if rebuild:
        fails += check_rebuild(out, digest, work_db or Path('work/top200_v1/rebuild_check.sqlite'))
    out['failures'] = fails
    for f in fails:
        log(f'CHECK FAILED: {f}')
    log('checks: ' + ('all passed' if not fails else f'{len(fails)} failed'))
    return out, fails
