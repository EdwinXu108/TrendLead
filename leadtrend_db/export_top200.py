"""CSV exports of the Top 200 pool (rule_version top200_v1), UTF-8, written to data/db/exports/top200_v1/.

Every file is sorted by (report_period, rank or subject id) so that repeated exports are byte-identical. Field
definitions are in docs/LeadTrend_数据库_v1.md (section 'CSV 字段').
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from . import rules as R
from . import sources as S

OUT_NAME = 'top200_v1'


def _write(path: Path, header: list[str], rows: list[tuple]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(header)
        for r in rows:
            w.writerow(['' if v is None else v for v in r])
    return len(rows)


def _usd(cents: int | None) -> float | None:
    return None if cents is None else round(cents / 100.0, 2)


def export_all(db_path: Path = S.DB_PATH, out_dir: Path | None = None, log=print) -> dict:
    out_dir = out_dir or (S.EXPORT_DIR / OUT_NAME)
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    q = lambda sql, a=(): conn.execute(sql, a).fetchall()
    counts: dict[str, int] = {}

    versions = q("""select version_id, report_period, available_at_utc, form, completeness, reconciled, is_canonical,
                    quarter_state, quarter_reason, supersedes_version_id, version_note, source_id
                    from pool_versions order by report_period, available_at_utc, version_id""")
    counts['pool_versions.csv'] = _write(
        out_dir / 'pool_versions.csv',
        ['version_id', 'report_period', 'available_at_utc', 'form', 'completeness', 'reconciled', 'is_canonical',
         'quarter_state', 'quarter_reason', 'supersedes_version_id', 'version_note', 'source_id'], versions)

    checks = q("""select p.report_period, c.version_id, c.check_name, c.status, c.detail
                  from quarter_checks c join pool_versions p using(version_id)
                  order by p.report_period, c.version_id, c.check_name""")
    counts['quarter_checks.csv'] = _write(out_dir / 'quarter_checks.csv',
                                          ['report_period', 'version_id', 'check_name', 'status', 'detail'], checks)

    hist = q("""select p.report_period, t.version_id, p.available_at_utc, p.is_canonical, t.rank, t.issuer_id,
                       substr(t.issuer_id, 5), t.issuer_name_at_period, i.name, t.amount_cents, t.fund_weight_raw,
                       t.tie_group, t.eligible_securities, t.blocked_members, t.rule_version, p.quarter_state
                from top200_companies t join pool_versions p using(version_id) join issuers i using(issuer_id)
                order by p.report_period, p.available_at_utc, t.version_id, t.rank""")
    hist_rows = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], _usd(r[9]), r[9], r[10], r[11], r[12], r[13],
                  r[14], r[15]) for r in hist]
    counts['historical_top200_companies.csv'] = _write(
        out_dir / 'historical_top200_companies.csv',
        ['report_period', 'version_id', 'available_at_utc', 'is_canonical', 'rank', 'issuer_id', 'cik',
         'issuer_name_at_period', 'sec_current_name', 'amount_usd', 'amount_cents', 'fund_weight_raw', 'tie_group',
         'eligible_securities', 'blocked_member', 'rule_version', 'quarter_state'], hist_rows)

    sec = q("""select p.report_period, t.version_id, rk.rank, t.issuer_id, t.security_id, t.class_code, t.security_type,
                      t.ticker_at_period,
                      (select count(*) from holdings h where h.snapshot_id=t.version_id and h.security_id=t.security_id
                         and h.issuer_id=t.issuer_id and h.line_kind='stock') as holding_lines,
                      t.amount_cents, t.fund_weight_raw, t.candidate_eligible, t.candidate_reason,
                      p.snapshot_id, p.source_id, s.name
               from top200_securities t join pool_versions p using(version_id)
                    join company_ranks rk on rk.version_id=t.version_id and rk.issuer_id=t.issuer_id
                    join securities s on s.security_id=t.security_id
               order by p.report_period, t.version_id, rk.rank, t.security_id""")
    sec_rows = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], _usd(r[9]), r[9], r[10], r[11], r[12], r[13],
                 r[14], r[15]) for r in sec]
    counts['historical_top200_securities.csv'] = _write(
        out_dir / 'historical_top200_securities.csv',
        ['report_period', 'version_id', 'rank', 'issuer_id', 'security_id', 'class_code', 'security_type',
         'ticker_v05_current', 'holding_lines', 'amount_usd', 'amount_cents', 'fund_weight_raw', 'candidate_eligible',
         'candidate_reason', 'snapshot_id', 'source_id', 'security_name'], sec_rows)

    strat = q("""select p.report_period, sc.version_id, th.historical_rank, sc.issuer_id, hi.name, sc.status_cutoff,
                       sc.current_status, sc.resolved_current_issuer, ri.name, sc.current_ticker, sc.strategy_eligible,
                       sc.blocked_reason, sc.excluded_reason
                from strategy_companies sc join pool_versions p using(version_id)
                     join strategy_companies th on th.version_id=sc.version_id and th.issuer_id=sc.issuer_id
                     join issuers hi on hi.issuer_id=sc.issuer_id join issuers ri on ri.issuer_id=sc.resolved_current_issuer
                order by p.report_period, sc.historical_rank""")
    counts['strategy_top200_companies.csv'] = _write(
        out_dir / 'strategy_top200_companies.csv',
        ['report_period', 'version_id', 'historical_rank', 'issuer_id', 'historical_sec_name', 'status_cutoff',
         'current_status', 'resolved_current_issuer', 'current_sec_name', 'current_ticker', 'strategy_eligible',
         'blocked_reason', 'excluded_reason'], strat)

    ssec = q("""select p.report_period, ss.version_id, ss.issuer_id, ss.security_id, ss.class_code,
                      ss.current_security_id, ss.current_ticker, ss.strategy_eligible, ss.reason
               from strategy_securities ss join pool_versions p using(version_id)
               order by p.report_period, ss.issuer_id, ss.security_id""")
    counts['strategy_top200_securities.csv'] = _write(
        out_dir / 'strategy_top200_securities.csv',
        ['report_period', 'version_id', 'issuer_id', 'security_id', 'class_code', 'current_security_id',
         'current_ticker', 'strategy_eligible', 'reason'], ssec)

    bound = q("""select p.report_period, rk.version_id, rk.rank, rk.issuer_id, i.name, rk.amount_cents, rk.in_top200,
                        rk.boundary_status, rk.tie_group, rk.unresolved_lines, rk.candidate_bound_cents
                 from company_ranks rk join pool_versions p using(version_id) join issuers i using(issuer_id)
                 where p.is_canonical=1 and rk.rank between ? and ?
                 order by p.report_period, rk.rank""", (R.BOUNDARY_RANGE[0], R.BOUNDARY_RANGE[1]))
    counts['rank_boundary_180_220.csv'] = _write(
        out_dir / 'rank_boundary_180_220.csv',
        ['report_period', 'version_id', 'rank', 'issuer_id', 'sec_current_name', 'amount_usd', 'amount_cents',
         'in_top200', 'boundary_status', 'tie_group', 'unresolved_lines', 'candidate_bound_cents'],
        [(r[0], r[1], r[2], r[3], r[4], _usd(r[5]), r[5], r[6], r[7], r[8], r[9], r[10]) for r in bound])

    excl = q("""select p.report_period, sc.version_id, 'company', sc.issuer_id, '', sc.historical_rank,
                       sc.excluded_reason, sc.blocked_reason
                from strategy_companies sc join pool_versions p using(version_id)
                where sc.strategy_eligible=0 and p.is_canonical=1
                union all
                select p.report_period, ss.version_id, 'security', ss.issuer_id, ss.security_id, '', ss.reason, ''
                from strategy_securities ss join pool_versions p using(version_id)
                where ss.strategy_eligible=0 and p.is_canonical=1
                order by 1, 3, 4, 5""")
    counts['exclusions.csv'] = _write(
        out_dir / 'exclusions.csv',
        ['report_period', 'version_id', 'scope', 'issuer_id', 'security_id', 'historical_rank', 'reason',
         'blocked_reason'], excl)

    blocked = q("""select p.report_period, p.version_id, 'candidate_company', rk.issuer_id, i.name, rk.amount_cents,
                          rk.candidate_bound_cents, rk.unresolved_lines, p.quarter_reason
                   from pool_versions p join company_ranks rk using(version_id) join issuers i using(issuer_id)
                   where p.is_canonical=1 and p.quarter_state='blocked' and rk.unresolved_lines > 0
                   union all
                   select p.report_period, p.version_id, 'no_candidate_group', '', '', 0,
                          sum(cast(round(h.value_usd*100) as integer)), count(*), p.quarter_reason
                   from pool_versions p join snapshots s on s.snapshot_id=p.version_id
                        join holdings h on h.snapshot_id=s.snapshot_id
                   where p.is_canonical=1 and p.quarter_state='blocked' and h.line_kind='stock'
                     and h.issuer_id is null and h.candidate_issuers='' and h.security_type in ('common','adr','unknown')
                   group by p.report_period, p.version_id
                   order by 1, 3, 4""")
    counts['blocking_review.csv'] = _write(
        out_dir / 'blocking_review.csv',
        ['report_period', 'version_id', 'scope', 'issuer_id', 'sec_current_name', 'mapped_amount_cents',
         'unresolved_cents', 'unresolved_lines', 'quarter_reason'], blocked)

    unresolved = q("""select s.holdings_as_of, h.snapshot_id, h.line_no, h.raw_name, h.raw_class_text, h.cusip_reported,
                             h.title_reported, h.value_usd, h.security_type, h.unmapped_reason, h.candidate_issuers
                      from holdings h join snapshots s using(snapshot_id)
                      where h.line_kind='stock' and h.issuer_id is null
                      order by s.holdings_as_of, h.value_usd desc, h.snapshot_id, h.line_no""")
    counts['unresolved_lines.csv'] = _write(
        out_dir / 'unresolved_lines.csv',
        ['report_period', 'snapshot_id', 'line_no', 'raw_name', 'raw_class_text', 'cusip_reported', 'title_reported',
         'value_usd', 'security_type', 'unmapped_reason', 'candidate_issuers'], unresolved)

    union = q("""select ss.security_id, s.issuer_id, s.class_code, ss.current_security_id, ss.current_ticker,
                        min(p.report_period), max(p.report_period), count(distinct p.report_period),
                        group_concat(distinct p.report_period), group_concat(distinct ss.issuer_id)
                 from strategy_securities ss join pool_versions p using(version_id) join securities s using(security_id)
                 where ss.strategy_eligible=1 and p.is_canonical=1
                 group by ss.security_id order by ss.security_id""")
    counts['security_union.csv'] = _write(
        out_dir / 'security_union.csv',
        ['security_id', 'security_issuer_id', 'class_code', 'current_security_id', 'current_ticker',
         'first_report_period', 'last_report_period', 'quarters_count', 'quarters', 'historical_issuers'], union)

    # 2019-12-31 read from both filings with the same company rule: N-CSR (non-canonical) against N-PORT (canonical)
    cmp_rows = []
    ncsr = q("""select rk.issuer_id, rk.amount_cents, rk.rank from company_ranks rk join pool_versions p using(version_id)
                where p.report_period='2019-12-31' and p.form='N-CSR' and rk.in_top200=1""")
    nport = q("""select rk.issuer_id, rk.amount_cents, rk.rank from company_ranks rk join pool_versions p using(version_id)
                 where p.report_period='2019-12-31' and p.form='NPORT-P' and rk.in_top200=1""")
    a = {r[0]: (r[1], r[2]) for r in ncsr}
    b = {r[0]: (r[1], r[2]) for r in nport}
    names = dict(q('select issuer_id, name from issuers'))
    for iid in sorted(set(a) | set(b)):
        ca, ra = a.get(iid, (None, None))
        cb, rb = b.get(iid, (None, None))
        if ca is None or cb is None:
            cmp_rows.append((iid, names.get(iid, ''), 'only_in_N-CSR' if cb is None else 'only_in_N-PORT',
                             ra or '', rb or '', _usd(ca), _usd(cb), ''))
        else:
            cmp_rows.append((iid, names.get(iid, ''), 'both', ra, rb, _usd(ca), _usd(cb), _usd(cb - ca)))
    counts['bridge_2019_12_31_company_compare.csv'] = _write(
        out_dir / 'bridge_2019_12_31_company_compare.csv',
        ['issuer_id', 'sec_current_name', 'status', 'rank_ncsr', 'rank_nport', 'amount_ncsr_usd', 'amount_nport_usd',
         'difference_nport_minus_ncsr_usd'], cmp_rows)

    conn.close()
    log(f'exports written to {out_dir}: ' + ', '.join(f'{k} {v}' for k, v in counts.items()))
    return counts
