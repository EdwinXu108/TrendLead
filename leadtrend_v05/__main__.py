"""CLI: python -m leadtrend_v05 <command>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .dataio import load_inputs
from .fixtures import generate as generate_fixtures
from .pipeline import run_pipeline
from .report import write_outputs
from .sec_nport import MissingSecUserAgent
from . import paths


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def cmd_demo(args: argparse.Namespace) -> int:
    root = Path(args.project_root) if args.project_root else _project_root()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    output_dir = Path(args.output)
    if not output_dir.is_absolute():
        output_dir = root / output_dir

    cfg = load_config(config_path)
    inputs = load_inputs(cfg, root)
    result = run_pipeline(inputs, run_id=args.run_id)
    written = write_outputs(result, output_dir)

    print(f"status={result.status}")
    print(f"run_id={result.run_id}")
    print(f"dataset_kind={result.dataset_kind}")
    print(f"inputs_hash={result.inputs_hash}")
    print(f"output_dir={output_dir}")
    for name, p in written.items():
        print(f"  {name}: {p}")
    if result.status != "ok":
        print(f"failure={result.summary.get('failure_message')}", file=sys.stderr)
        return 2
    print(f"n_trades={result.summary.get('n_trades')}")
    print(f"final_equity={result.summary.get('final_equity')}")
    return 0


def cmd_generate_fixtures(args: argparse.Namespace) -> int:
    root = Path(args.project_root) if args.project_root else _project_root()
    output_dir = Path(args.output)
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    generate_fixtures(output_dir)
    return 0


def cmd_stage2_prepare(args: argparse.Namespace) -> int:
    from .stage2_prepare import run_stage2_prepare

    root = Path(args.project_root) if args.project_root else _project_root()
    try:
        coverage = run_stage2_prepare(
            root,
            stage2_config_path=Path(args.stage2_config) if args.stage2_config else None,
            max_securities=args.max_securities,
        )
    except MissingSecUserAgent as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    printable = {k: v for k, v in coverage.items() if k != "opend_errors"}
    print(json.dumps(printable, indent=2, ensure_ascii=False))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .stage2_validate import run_validate

    root = Path(args.project_root) if args.project_root else _project_root()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    out_dir = Path(args.output)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    return int(run_validate(root, config_path, out_dir))


def cmd_stage2_enrich(args: argparse.Namespace) -> int:
    from .stage2_enrich import run_enrich
    run_enrich(_project_root())
    return 0


def cmd_run_dev(args):
    from .stage3 import run_dev
    root=_project_root()
    try:
        if args.artifacts=='lean':
            from .perf_runner import fixed_seed_main,run_single_legacy
            seeded=fixed_seed_main(sys.argv[1:])
            if seeded is not None:return seeded
            return run_single_legacy(root,root/args.config,args.output,args.stress,args.end_date)
        from .perf_runner import safe_output
        return run_dev(root,root/args.config,safe_output(root,args.output),end_override=args.end_date,stress=args.stress)
    except ValueError as exc:
        print(str(exc),file=sys.stderr)
        return 2


def cmd_stage2_repair(args):
    from .stage2_repair import run_repair
    run_repair(_project_root())
    return 0


def cmd_run_grid(args):
    from .stage4_runner import run_grid
    from .perf_runner import fixed_seed_main
    seeded=fixed_seed_main(sys.argv[1:])
    if seeded is not None:return seeded
    output=args.output or (paths.RUNS+'/explore_grid' if args.verify=='explore' else paths.RUNS+'/grid')
    result=run_grid(_project_root(),output=output,workers=args.workers,artifacts=args.artifacts,force=args.force,verify_level=args.verify)
    return 0 if result['status']=='PASS' else 2

def cmd_compare_grid(args):
    from .perf_compare import compare_grid
    root=_project_root()
    summary=compare_grid(root,args.grid,args.reference,args.output,workers=args.workers,expect=args.expect)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0 if summary['status']=='PASS' else 1

def cmd_bench(args):
    from .perf_bench import bench
    if args.mode!='baseline':
        from .perf_runner import fixed_seed_main
        seeded=fixed_seed_main(sys.argv[1:])
        if seeded is not None:return seeded
    return bench(_project_root(),args.mode,workers=args.workers,artifacts=args.artifacts,force=args.force,output=args.output,cold_cache=args.cold_cache,verify_level=args.verify)

def cmd_export_artifacts(args):
    from .perf_runner import safe_output
    from .perf_artifacts import export_full
    output=safe_output(_project_root(),args.output)
    if output.exists():raise ValueError('export requires a new directory; existing artifacts preserved')
    source=Path(args.input)
    if not source.is_absolute():source=_project_root()/source
    export_full(source,output)
    return 0


def cmd_run_holdout(args):
    from .stage4_summary import run_holdout
    try:return run_holdout(_project_root())
    except ValueError as exc:
        print(str(exc),file=sys.stderr)
        return 2


def cmd_summarize_grid(args):
    from .stage4_summary import aggregate,heatmaps
    from .perf_runner import safe_output
    root=_project_root()
    grid=root/args.grid
    out=safe_output(root,args.output or (paths.RUNS+'/explore_summary' if args.exploration else paths.RUNS+'/grid_summary'))
    decision_path=safe_output(root,args.decision)
    for p in (grid).glob('*/cost_*bps/completion.json'):
        doc=json.loads(p.read_text(encoding='utf-8'))
        if doc.get('verification_level')=='explore':
            if not args.exploration:
                print('refused: explore-level runs are screening results, not formal outcomes; pass --exploration to summarize them',file=sys.stderr)
                return 1
            if doc['status']!='completed':return 1
        elif doc['status']!='completed' or doc['acceptance']['status']!='PASS':return 1
    rows,criteria,decision=aggregate(root,grid_dir=grid,output_dir=out,decision_path=decision_path,exploration=args.exploration)
    heatmaps(root,rows,criteria,output_dir=out,exploration=args.exploration)
    print(json.dumps(decision,ensure_ascii=False,indent=2))
    return 0


def _workers_arg(text: str):
    return 'auto' if text == 'auto' else int(text)


def cmd_run_experiment(args):
    import os
    from .experiments import run_experiment
    from .perf_runner import WORKER_PEAK_BYTES, auto_workers
    from .perf_bench import available_physical
    root = _project_root()
    spec = Path(args.spec)
    if not spec.is_absolute():
        spec = root / spec
    workers = args.workers
    if workers == 'auto':
        workers = auto_workers(available_physical(), WORKER_PEAK_BYTES['explore'], os.cpu_count() or 1)
    try:
        out = run_experiment(root, spec, workers=workers, null_runs=args.null)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(out)
    return 0


def cmd_diagnose(args):
    import os
    from .diagnostics import run_diagnostics
    from .perf_runner import WORKER_PEAK_BYTES, auto_workers
    from .perf_bench import available_physical
    root = _project_root()
    workers = args.workers
    if workers == 'auto':
        workers = auto_workers(available_physical(), WORKER_PEAK_BYTES['explore'], os.cpu_count() or 1)
    try:
        only = [x.strip() for x in args.only.split(',')] if args.only else None
        out = run_diagnostics(root, args.output, workers=workers, only=only)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leadtrend_v05")
    sub = p.add_subparsers(dest="command", required=True)
    bench=sub.add_parser('bench',help='Engineering-only development benchmarks; never holdout')
    bench.add_argument('--mode',default='baseline')
    bench.add_argument('--workers',type=_workers_arg,default=1,help='worker processes 1..8, or auto (memory, CPU and measured-speed limits)')
    bench.add_argument('--verify',choices=['daily','regression','explore'],default='daily',help='Independent audit always; regression additionally checks the frozen oracle; explore screens without the replay audit')
    bench.add_argument('--artifacts',choices=['full','lean'],default='lean')
    bench.add_argument('--force',action='store_true')
    bench.add_argument('--output',default=None)
    bench.add_argument('--cold-cache',action='store_true',help='Build a fresh cache namespace; preserve all existing caches')
    export=sub.add_parser('export-artifacts',help='Losslessly expand compressed/shared artifacts to a new directory')
    export.add_argument('--input',required=True);export.add_argument('--output',required=True)
    export.set_defaults(func=cmd_export_artifacts)
    bench.set_defaults(func=cmd_bench)
    grid=sub.add_parser('run-grid',help='Execute only the sealed Stage4 development grid')
    grid.add_argument('--output',default=None)
    grid.add_argument('--workers',type=_workers_arg,default=1,help='worker processes 1..8, or auto (memory, CPU and measured-speed limits)')
    grid.add_argument('--verify',choices=['daily','regression','explore'],default='daily')
    grid.add_argument('--artifacts',choices=['full','lean'],default='lean')
    grid.add_argument('--force',action='store_true')
    grid.set_defaults(func=cmd_run_grid)
    compare=sub.add_parser('compare-grid',help='Byte-exact comparison of a new grid against the reviewed reference; never holdout')
    compare.add_argument('--grid',required=True)
    compare.add_argument('--reference',default=None,help='defaults to research/v05/results/stage4_unified/runs')
    compare.add_argument('--output',required=True,help='JSON report inside work/')
    compare.add_argument('--workers',type=int,default=1)
    compare.add_argument('--expect',choices=['daily','explore'],default='daily',help='verification level every new run must carry; other levels FAIL')
    compare.set_defaults(func=cmd_compare_grid)
    summary_grid=sub.add_parser('summarize-grid',help='Apply the immutable regional rule and generate heatmaps; never run holdout')
    summary_grid.add_argument('--grid',default=paths.STAGE4_RUNS)
    summary_grid.add_argument('--output',default=None)
    summary_grid.add_argument('--decision',default=paths.RUNS+'/grid_summary/holdout_decision.json')
    summary_grid.add_argument('--exploration',action='store_true',help='Allow explore-level runs; outputs are marked as screening only and no holdout decision is written')
    summary_grid.set_defaults(func=cmd_summarize_grid)
    exp=sub.add_parser('run-experiment',help='Run a committed development-only strategy experiment spec; never the holdout')
    exp.add_argument('--spec',required=True,help='research/v05/experiments/<ID>.json, committed with a clean tree')
    exp.add_argument('--workers',type=_workers_arg,default=1,help='worker processes 1..8, or auto')
    exp.add_argument('--null',type=int,default=None,help='override null_runs in the spec')
    exp.set_defaults(func=cmd_run_experiment)
    diag = sub.add_parser('diagnose', help='Diagnostics D0 on the development segment only; never the holdout; not an attempt')
    diag.add_argument('--output', required=True, help='directory under work/runs, e.g. work/runs/diagnostics/D0')
    diag.add_argument('--workers', type=_workers_arg, default=1, help='worker processes 1..8, or auto')
    diag.add_argument('--only', default=None, help='recompute only these items, comma-separated, e.g. D3 (default: all)')
    diag.set_defaults(func=cmd_diagnose)
    hold=sub.add_parser('run-holdout',help='One-shot, rule-selected Stage4 holdout; refuses without authorization')
    hold.set_defaults(func=cmd_run_holdout)
    repair=sub.add_parser('stage2-repair',help='Claude A1--A3 identity, child quote and official dividend repairs')
    repair.set_defaults(func=cmd_stage2_repair)
    dev = sub.add_parser('run-dev', help='Registered development only; holdout end dates rejected')
    dev.add_argument('--config', default='config/v05_stage3.json')
    dev.add_argument('--output', default=paths.RUNS+'/dev')
    dev.add_argument('--end-date', default=None)
    dev.add_argument('--stress', action='store_true')
    dev.add_argument('--artifacts',choices=['full','lean'],default='full')
    dev.set_defaults(func=cmd_run_dev)

    demo = sub.add_parser("demo", help="Run offline synthetic stage-1 demo")
    demo.add_argument("--config", required=True)
    demo.add_argument("--output", required=True)
    demo.add_argument("--project-root", default=None)
    demo.add_argument("--run-id", default=None)
    demo.set_defaults(func=cmd_demo)

    gen = sub.add_parser("generate-fixtures", help="Regenerate synthetic fixtures")
    gen.add_argument("--output", default=paths.FIXTURES)
    gen.add_argument("--project-root", default=None)
    gen.set_defaults(func=cmd_generate_fixtures)

    s2p = sub.add_parser(
        "stage2-prepare",
        help="Download/normalize real VOO N-PORT + OpenD data (requires sec_user_agent)",
    )
    s2p.add_argument("--project-root", default=None)
    s2p.add_argument("--stage2-config", default=None)
    s2p.add_argument("--max-securities", type=int, default=None)
    s2p.set_defaults(func=cmd_stage2_prepare)
    enrich = sub.add_parser("stage2-enrich", help="Score-only spinoffs, dividend reconciliation, QFQ diagnostics")
    enrich.set_defaults(func=cmd_stage2_enrich)

    val = sub.add_parser(
        "validate",
        help="Load inputs + sample selection only (no equity/returns)",
    )
    val.add_argument("--config", required=True)
    val.add_argument("--output", required=True)
    val.add_argument("--project-root", default=None)
    val.set_defaults(func=cmd_validate)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
