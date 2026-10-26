"""Command line for leadtrend_db (schema 2, rule_version top200_v1).

  python -m leadtrend_db build [--offline] [--db PATH]   build a database (default work/top200_v1/candidate.sqlite)
  python -m leadtrend_db check [--db PATH] [--skip-rebuild]
                                                        run the checks; exit 1 if any check fails
  python -m leadtrend_db export [--db PATH]              CSV exports to data/db/exports/top200_v1/
  python -m leadtrend_db query --quarter YYYY-MM-DD      canonical Top 200 of one report period
  python -m leadtrend_db query --as-of YYYY-MM-DDTHH:MM:SSZ
                                                        the list that was public at that UTC time
  python -m leadtrend_db promote [--from PATH]           replace data/db/ with a checked build (checks must pass)
  python -m leadtrend_db summary [--out PATH]            counts-only summary (research/db_v1/summary.md)

Exit status is non-zero for any failed check, a refused promotion or a query with no answer.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import sources as S

WORK_DB = Path('work/top200_v1/candidate.sqlite')


def _log(message: str) -> None:
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='leadtrend_db')
    sub = parser.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('--offline', action='store_true', help='read only the raw archive; no network request')
    b.add_argument('--db', default=str(WORK_DB), help='database path to write (default: work/top200_v1/candidate.sqlite)')
    c = sub.add_parser('check')
    c.add_argument('--db', default=str(S.DB_PATH))
    c.add_argument('--skip-rebuild', action='store_true', help='skip the offline rebuild and hash comparison')
    e = sub.add_parser('export')
    e.add_argument('--db', default=str(S.DB_PATH))
    q = sub.add_parser('query')
    q.add_argument('--db', default=str(S.DB_PATH))
    q.add_argument('--quarter')
    q.add_argument('--as-of')
    p = sub.add_parser('promote')
    p.add_argument('--from', dest='source', default=str(WORK_DB))
    s = sub.add_parser('summary')
    s.add_argument('--db', default=str(S.DB_PATH))
    s.add_argument('--out', default='research/db_v1/summary.md')
    args = parser.parse_args(argv)

    if args.cmd == 'build':
        from .build import build
        result = build(offline=args.offline, log=_log, db_path=Path(args.db))
        print(json.dumps({k: v for k, v in result.items() if k != 'extra'}, ensure_ascii=False, indent=2,
                         sort_keys=True), flush=True)
        return 0
    if args.cmd == 'check':
        from .checks import run_checks
        out, fails = run_checks(db_path=Path(args.db), rebuild=not args.skip_rebuild, log=_log)
        Path('work/top200_v1').mkdir(parents=True, exist_ok=True)
        Path('work/top200_v1/checks.json').write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                                                      encoding='utf-8')
        return 1 if fails else 0
    if args.cmd == 'export':
        from .export_top200 import export_all
        export_all(db_path=Path(args.db), log=_log)
        return 0
    if args.cmd == 'query':
        from .query import main_query
        if bool(args.quarter) == bool(args.as_of):
            print('give exactly one of --quarter or --as-of', file=sys.stderr)
            return 2
        return main_query(Path(args.db), args.quarter, args.as_of)
    if args.cmd == 'promote':
        from .checks import run_checks
        src = Path(args.source)
        out, fails = run_checks(db_path=src, rebuild=True, work_db=Path('work/top200_v1/rebuild_check.sqlite'), log=_log)
        if fails:
            _log('promotion refused: checks failed; data/db was not changed')
            return 1
        dst = S.DB_PATH
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.copy2(dst, Path('work/top200_v1/previous_db_v1.sqlite'))
        tmp = dst.with_suffix('.promoting')
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        _log(f'promoted {src} to {dst}')
        return 0
    if args.cmd == 'summary':
        from .summary import write_summary
        write_summary(Path(args.db), Path(args.out), log=_log)
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
