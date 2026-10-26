"""Command line: python -m leadtrend_bt run --config config/bt_xxx.json"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .runner import run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='leadtrend_bt')
    sub = parser.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--config', required=True, help='JSON file with one configuration')
    r.add_argument('--out', default='work/bt_runs')
    args = parser.parse_args(argv)
    if args.cmd == 'run':
        cfg = json.loads(Path(args.config).read_text(encoding='utf8'))
        out = run(cfg, out_root=args.out)
        print(json.dumps({k: v for k, v in out['summary'].items() if k in ('strategy', 'benchmark', 'excess_cagr')},
                         indent=2, default=str))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
