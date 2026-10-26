"""Counts-only summary of the database for research/db_v1/summary.md. No names, no per-security values."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import rules as R


def write_summary(db_path: Path, out: Path, log=print) -> None:
    c = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    q = lambda s: c.execute(s).fetchall()[0]
    meta = dict(q_ for q_ in c.execute('select key, value from meta').fetchall())
    lines = [
        f'# leadtrend_db schema {meta.get("schema_version")} counts (rule_version {meta.get("rule_version")})',
        '',
        f'- schema_version {meta.get("schema_version")}, rule_version {meta.get("rule_version")}, '
        f'build_id {meta.get("build_id")}, content_sha256 {meta.get("content_sha256")}',
        f'- status cutoff {meta.get("status_cutoff")}; build cutoff {meta.get("build_cutoff_utc")}',
        '',
        '## Versions and quarters',
        f'- pool versions: {q("select count(*) from pool_versions")[0]}; canonical quarters: '
        f'{q("select count(*) from pool_versions where is_canonical=1")[0]}',
        f'- canonical quarter state: ' + ', '.join(f'{s} {n}' for s, n in c.execute(
            "select quarter_state, count(*) from pool_versions where is_canonical=1 group by 1 order by 1").fetchall()),
        f'- alternate (later filing of the same period) versions: {q("select count(*) from pool_versions where is_canonical=0")[0]}',
        '',
        '## Historical top 200 (canonical versions)',
        f'- company rows: {q("select count(*) from top200_companies t join pool_versions p using(version_id) where p.is_canonical=1")[0]}',
        f'- security rows: {q("select count(*) from top200_securities t join pool_versions p using(version_id) where p.is_canonical=1")[0]}',
        f'- security rows with candidate_eligible=1: '
        f'{q("select count(*) from top200_securities t join pool_versions p using(version_id) where p.is_canonical=1 and t.candidate_eligible=1")[0]}',
        '',
        '## Strategy list after the status cutoff (canonical versions)',
        f'- strategy company rows: {q("select count(*) from strategy_companies sc join pool_versions p using(version_id) where p.is_canonical=1")[0]}',
        f'- eligible company rows: {q("select count(*) from strategy_companies sc join pool_versions p using(version_id) where p.is_canonical=1 and sc.strategy_eligible=1")[0]}',
        f'- excluded (delisted at cutoff): {q("select count(*) from strategy_companies sc join pool_versions p using(version_id) where p.is_canonical=1 and sc.excluded_reason<>\'\'")[0]}',
        f'- blocked (status unknown or unresolved): {q("select count(*) from strategy_companies sc join pool_versions p using(version_id) where p.is_canonical=1 and sc.blocked_reason<>\'\'")[0]}',
        '',
        '## Holdings and identity',
        f'- securities: {q("select count(*) from securities")[0]}; issuers: {q("select count(*) from issuers")[0]}',
        f'- holding lines: {q("select count(*) from holdings")[0]}; stock lines without issuer: '
        f'{q("select count(*) from holdings where line_kind=\'stock\' and issuer_id is null")[0]}',
        f'- security issuer spans: {q("select count(*) from security_issuer_spans")[0]}',
        f'- issuer links: {q("select count(*) from issuer_links")[0]}; of which via override: '
        f'{q("select count(*) from issuer_links where via_override=1")[0]}',
        f'- review items: {q("select count(*) from review_items")[0]}',
        '',
        '## Reserved (empty in v2)',
        f'- prices_daily {q("select count(*) from prices_daily")[0]}, corporate_actions {q("select count(*) from corporate_actions")[0]}, '
        f'price_coverage {q("select count(*) from price_coverage")[0]}, index_changes {q("select count(*) from index_changes")[0]}',
        '',
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(lines), encoding='utf-8', newline='\n')
    log(f'summary written to {out}')
