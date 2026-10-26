"""Robustness runs for round 2 (plan 8.2): start offsets and issuer exclusion, on committed configurations only.

Neither kind of run is a registration, adds an attempt or selects a configuration. Every run keeps the configuration
as registered, the costs (5 and 10 bp), the account (100,000 USD, 1 USD a trade) and the development end (2024-06-28).
An offset run starts at the k-th signal week of the development segment; k = 0 is the segment start, used for the
equivalence check, and k = 1 to 4 are the robustness starts. An issuer run removes every security of one issuer from
each week's chosen set, candidates and universe. Authorisation is diagnostic: a configuration must be in stage 4 or in
a committed registration. Outputs go to work/runs/robustness/ only.

    python -X utf8 -m leadtrend_v05.robustness equivalence --out work/runs/robustness/equivalence
    python -X utf8 -m leadtrend_v05.robustness offsets --ids ID,ID --offsets 0,1,2,3,4 --costs 10 --out work/runs/robustness/offsets
    python -X utf8 -m leadtrend_v05.robustness issuer --ids ID,ID --issuer LEI:... --costs 5,10 --out work/runs/robustness/issuer
"""
import argparse
import json
import multiprocessing
from datetime import date
from pathlib import Path

from . import paths
from .experiments import A90_ID, ExperimentContext, _anchor_point
from .perf_artifacts import read_table
from .perf_bench import Timer, available_physical, dump
from .perf_cache import ensure_signal_tables, identity, load_cache, worker_cache
from .perf_runner import accepted, auto_workers, run_key, safe_output, WORKER_PEAK_BYTES
from .stage4_registry import verify

OFFSETS = (0, 1, 2, 3, 4)  # signal weeks after the development start; 0 is the start itself
COSTS = (5, 10)
EXPERIMENT_ID = 'R2_ROBUSTNESS'


class _ExcludedWeek:
    def __init__(self, week, excluded):
        self.week = week
        self.excluded = excluded

    def __getitem__(self, key):
        if key == 'chosen':
            return frozenset(s for s in self.week['chosen'] if s not in self.excluded)
        if key == 'universe':
            return [r for r in self.week['universe'] if r['security_id'] not in self.excluded]
        if key == 'windows':
            return _ExcludedWindows(self.week['windows'], self.excluded)
        raise KeyError(key)


class _ExcludedWindows:
    def __init__(self, windows, excluded):
        self.windows = windows
        self.excluded = excluded

    def __getitem__(self, L):
        w = self.windows[L]
        return dict(scores=w['scores'], ranked=w['ranked'],
                    candidates=[c for c in w['candidates'] if c.security_id not in self.excluded],
                    unscored={s: r for s, r in w['unscored'].items() if s not in self.excluded})


class IssuerExcludedFrames:
    """Signal-week frames with the excluded securities removed everywhere a selection rule can see them."""

    def __init__(self, frames, excluded):
        self.frames = frames
        self.excluded = set(excluded)

    def weeks(self):
        return self.frames.weeks()

    def __getitem__(self, d):
        return _ExcludedWeek(self.frames[d], self.excluded)


class RobustnessContext(ExperimentContext):
    """Admits registered configurations at the development start or at one of the first four signal weeks."""

    def signal_weeks(self):
        return self.frames.weeks()

    def authorize(self, raw, end_override):
        weeks = self.signal_weeks()
        if weeks[0] != self.segment['start']:
            raise ValueError('the first signal week is not the development start')
        start = raw.get('start_date')
        if start not in weeks[:len(OFFSETS)]:
            raise ValueError('start must be the development start or one of the first four signal weeks')
        tranche = raw.get('tranche')
        if tranche is not None and not (isinstance(tranche.get('K'), int) and 1 <= tranche['K'] <= 12
                                        and isinstance(tranche.get('j'), int) and 0 <= tranche['j'] < tranche['K']):
            raise ValueError('tranche needs K from 1 to 12 and j from 0 to K-1')
        cap = raw.get('cap')
        if cap is not None and not 0 < cap <= 1:
            raise ValueError('single-stock cap must be in (0, 1]')
        super().authorize(dict(raw, start_date=self.segment['start']), end_override)
        return date.fromisoformat(start), self.end


def _context(root, doc, payload, excluded=None):
    ctx, _ = load_cache(root, doc)
    ctx.__class__ = RobustnessContext
    if excluded is not None:
        ctx.frames = IssuerExcludedFrames(ctx.frames, excluded)
    ctx.configure(**payload)
    return ctx


def _init_worker(cache_path, cache_key, payload, excluded):
    from . import perf_runner
    ctx = worker_cache(cache_path, cache_key)
    ctx.__class__ = RobustnessContext
    if excluded is not None:
        ctx.frames = IssuerExcludedFrames(ctx.frames, excluded)
    ctx.configure(**payload)
    perf_runner._TIMER = Timer()
    perf_runner._TIMER.instrument()
    perf_runner._CTX = ctx


def _run_jobs(jobs, ctx, workers, payload, excluded):
    from . import perf_runner
    todo = [j for j in jobs if not accepted(j[2], run_key(ctx.cache_key, j[0], j[1], j[3], j[4], j[5]))]
    if not todo:
        return 0
    if workers == 1:
        perf_runner._CTX = ctx
        perf_runner._TIMER = Timer()
        perf_runner._TIMER.instrument()
        for job in todo:
            perf_runner.one_job(job)
        return len(todo)
    spawn = multiprocessing.get_context('spawn')
    cache_path = str(ctx.cache_folder / 'prepared.pkl')
    with spawn.Pool(workers, initializer=_init_worker, initargs=(cache_path, ctx.cache_key, payload, excluded)) as pool:
        list(pool.imap_unordered(perf_runner.one_job, todo))
    return len(todo)


def configurations(root, ids):
    """The committed configurations named by ID, in the order given."""
    return [_anchor_point(root, pid, []) for pid in ids]


def run_offsets(root, base, ids, offsets=OFFSETS, costs=(10,), workers=1):
    root, base = Path(root), Path(base)
    doc = identity(root, 'uniform')
    points = configurations(root, ids)
    ensure_signal_tables(root, doc, [p['signal'] for p in points])
    payload = dict(points=points, costs=list(COSTS), experiment=dict(id=EXPERIMENT_ID, kind='start_offset'))
    ctx = _context(root, doc, payload)
    weeks = ctx.signal_weeks()
    jobs = []
    for k in offsets:
        if k not in range(len(OFFSETS)):
            raise ValueError('offset must be one of 0 to 4: ' + str(k))
        for p in points:
            for cost in costs:
                job = dict(p, start_date=weeks[k], experiment=dict(id=EXPERIMENT_ID, kind='start_offset', offset=k))
                jobs.append((job, cost, str(base / f'offset_{k}' / p['id'] / f'cost_{cost}bps'), 'explore', False, 'explore'))
    executed = _run_jobs(jobs, ctx, workers, payload, None)
    return dict(starts={k: weeks[k] for k in offsets}, executed=executed)


def run_issuer(root, base, ids, issuer, costs=COSTS, workers=1):
    return run_issuers(root, base, ids, [issuer], costs=costs, workers=workers)


def run_issuers(root, base, ids, issuers, costs=COSTS, workers=1):
    """Remove every security of the given issuers (one or more) from every week, then run the configurations."""
    root, base = Path(root), Path(base)
    doc = identity(root, 'uniform')
    issuers = list(issuers)
    excluded = sorted({r['security_id'] for r in read_table(root / paths.NORMALIZED / 'securities.csv') if r.get('issuer_id') in set(issuers)})
    if not excluded:
        raise ValueError('no security carries the given issuers: ' + ','.join(issuers))
    points = configurations(root, ids)
    ensure_signal_tables(root, doc, [p['signal'] for p in points])
    experiment = dict(id=EXPERIMENT_ID, kind='issuer_exclusion', excluded_issuers=issuers, note='robustness run; not an attempt; not for selection')
    payload = dict(points=points, costs=list(COSTS), experiment=experiment)
    ctx = _context(root, doc, payload, excluded)
    jobs = []
    for p in points:
        for cost in costs:
            jobs.append((dict(p, experiment=experiment), cost, str(base / p['id'] / f'cost_{cost}bps'), 'explore', False, 'explore'))
    executed = _run_jobs(jobs, ctx, workers, payload, excluded)
    return dict(issuers=issuers, excluded_securities=len(excluded), executed=executed)


def equivalence(root, base, workers=1):
    """A90 started at the development start must reproduce the reviewed stage 4 summary byte for byte."""
    root, base = Path(root), Path(base)
    ran = run_offsets(root, base, [A90_ID], offsets=(0,), costs=(10,), workers=workers)
    new = base / 'offset_0' / A90_ID / 'cost_10bps' / 'summary.json'
    ref = root / paths.STAGE4_RUNS / A90_ID / 'cost_10bps' / 'summary.json'
    same = new.read_bytes() == ref.read_bytes()
    result = dict(configuration=A90_ID, start=ran['starts'][0], new=str(new), reference=str(ref),
                  status='PASS' if same else 'FAIL')
    dump(base / 'equivalence.json', result)
    return result


def _workers(text):
    if text != 'auto':
        return int(text)
    import os
    return auto_workers(available_physical(), WORKER_PEAK_BYTES['explore'], os.cpu_count() or 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Round 2 robustness runs (not registrations, not attempts)')
    sub = ap.add_subparsers(dest='mode', required=True)
    eq = sub.add_parser('equivalence', help='k = 0 A90 run against the stage 4 reference')
    eq.add_argument('--out', required=True)
    off = sub.add_parser('offsets', help='start offsets k = 0..4 of registered configurations')
    off.add_argument('--ids', required=True)
    off.add_argument('--offsets', default='0,1,2,3,4')
    off.add_argument('--costs', default='10')
    off.add_argument('--out', required=True)
    off.add_argument('--workers', default='1')
    iss = sub.add_parser('issuer', help='remove one issuer from every week of registered configurations')
    iss.add_argument('--ids', required=True)
    iss.add_argument('--issuer', required=True)
    iss.add_argument('--costs', default='5,10')
    iss.add_argument('--out', required=True)
    iss.add_argument('--workers', default='1')
    args = ap.parse_args(argv)
    root = Path('.').resolve()
    verify(root)  # sealed stage 4 inputs
    base = safe_output(root, args.out)
    if args.mode == 'equivalence':
        result = equivalence(root, base, _workers('1'))
    elif args.mode == 'offsets':
        result = run_offsets(root, base, args.ids.split(','), tuple(int(x) for x in args.offsets.split(',')),
                             tuple(int(x) for x in args.costs.split(',')), _workers(args.workers))
    else:
        result = run_issuer(root, base, args.ids.split(','), args.issuer, tuple(int(x) for x in args.costs.split(',')),
                            _workers(args.workers))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
