"""Command line for leadtrend_db.

  python -m leadtrend_db build [--offline]   fetch what is missing (unless --offline), then write the database
  python -m leadtrend_db check               run the checks; writes data/db/exports/checks.* and work/ rebuild
  python -m leadtrend_db stats [--summary P] counts for research/db_v1/summary.md (counts only)
  python -m leadtrend_db export              CSV exports to data/db/exports/
"""
from __future__ import annotations

import argparse
import json
import sys


def _log(message: str) -> None:
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='leadtrend_db')
    sub = parser.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('--offline', action='store_true', help='read only the raw archive; no network request')
    c = sub.add_parser('check')
    c.add_argument('--skip-rebuild', action='store_true', help='skip the offline rebuild and hash comparison')
    s = sub.add_parser('stats')
    s.add_argument('--summary', help='write the counts summary to this path')
    sub.add_parser('export')
    args = parser.parse_args(argv)

    if args.cmd == 'build':
        from .build import build
        result = build(offline=args.offline, log=_log)
        print(json.dumps({k: v for k, v in result.items() if k != 'extra'}, ensure_ascii=False, indent=2,
                         sort_keys=True), flush=True)
        return 0
    if args.cmd == 'check':
        from .checks import run_checks
        run_checks(log=_log, rebuild=not args.skip_rebuild)
        return 0
    if args.cmd == 'stats':
        from .export import write_stats
        write_stats(summary_path=args.summary, log=_log)
        return 0
    if args.cmd == 'export':
        from .export import export_all
        export_all(log=_log)
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
