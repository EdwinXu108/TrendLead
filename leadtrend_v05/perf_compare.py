"""compare-grid: byte-exact comparison of a whole grid against a reviewed reference.

Compares every registered configuration at every registered cost: the 17 logical
tables (expanded from their lossless encodings), the entire summary.json bytes
and all acceptance.json fields. Explore-level runs are compared on the subset
they actually write. Any unequal item, or any read failure, is a FAIL; nothing
is skipped silently. Output is a single JSON under work/.
"""
import hashlib, json, multiprocessing, time
from pathlib import Path
from . import paths
from .perf_bench import dump, sha
from .perf_artifacts import expanded_bytes
from .perf_runner import safe_output
from .stage4_registry import verify

TABLES = ['benchmark', 'cash_flows', 'corporate_actions', 'daily_contributions', 'daily_equity',
          'data_issues', 'decisions', 'fractional_cash', 'positions', 'receivables',
          'return_contributions', 'rolling_relative', 'scores', 'trades', 'universe',
          'weekly_checks', 'weekly_coverage']
EXPLORE_TABLES = ['daily_equity', 'benchmark', 'trades', 'weekly_checks']


def _level(folder):
    p = Path(folder) / 'completion.json'
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8')).get('verification_level')
    except ValueError:
        return None


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _bytes_equal(a, b):
    return a == b, dict(ref_sha256=_sha(a), new_sha256=_sha(b), ref_bytes=len(a), new_bytes=len(b), equal=a == b)


def compare_pair(job):
    grid, reference, cid, cost, expect = job
    suffix = Path(cid) / f'cost_{cost}bps'
    new_dir, ref_dir = Path(grid) / suffix, Path(reference) / suffix
    level = _level(new_dir)
    # Records written before the level field existed are daily runs.
    explore = (level or 'daily') == 'explore'
    entry = dict(configuration_id=cid, cost_bps=cost, verification_level=level, files={}, problems=[])
    if (level or 'daily') != expect:
        entry['problems'].append('verification_level')
    for name in (EXPLORE_TABLES if explore else TABLES):
        try:
            ref_raw = expanded_bytes(ref_dir / (name + '.csv'))
            new_raw = expanded_bytes(new_dir / (name + '.csv'))
            equal, rec = _bytes_equal(ref_raw, new_raw)
        except Exception as exc:
            equal, rec = False, dict(error=f'{type(exc).__name__}: {exc}', equal=False)
        entry['files'][name + '.csv'] = rec
        if not equal:
            entry['problems'].append(name + '.csv')
    try:
        ref_sum = (ref_dir / 'summary.json').read_bytes()
        new_sum = (new_dir / 'summary.json').read_bytes()
        equal, rec = _bytes_equal(ref_sum, new_sum)
    except Exception as exc:
        equal, rec = False, dict(error=f'{type(exc).__name__}: {exc}', equal=False)
    entry['files']['summary.json'] = rec
    if not equal:
        entry['problems'].append('summary.json')
    if explore:
        # The independent replay acceptance is not run at explore level.
        entry['acceptance_all_fields_equal'] = None
    else:
        try:
            ref_acc = json.loads((ref_dir / 'acceptance.json').read_text(encoding='utf-8'))
            new_acc = json.loads((new_dir / 'acceptance.json').read_text(encoding='utf-8'))
            entry['acceptance_all_fields_equal'] = ref_acc == new_acc
        except Exception as exc:
            entry['acceptance_all_fields_equal'] = False
            entry['acceptance_error'] = f'{type(exc).__name__}: {exc}'
        if not entry['acceptance_all_fields_equal']:
            entry['problems'].append('acceptance.json')
    entry['status'] = 'PASS' if not entry['problems'] else 'FAIL'
    return entry


def compare_grid(root, grid, reference=None, output=None, workers=1, expect='daily'):
    """Compare <grid> against <reference> (default: the reviewed unified matrix).

    ``expect`` is the verification level every new run must carry; a run at any other level fails its pair.
    """
    root = Path(root); grid = Path(grid)
    reference = Path(reference) if reference else root / paths.STAGE4_RUNS
    if expect not in {'daily', 'explore'}:
        raise ValueError('expect must be daily or explore')
    if not grid.is_dir():
        raise ValueError('grid directory not found: ' + str(grid))
    if not reference.is_dir():
        raise ValueError('reference directory not found: ' + str(reference))
    output = safe_output(root, output) if output else None
    if output is None:
        raise ValueError('compare-grid requires --output inside ' + paths.WORK + '/')
    reg = verify(root)
    jobs = [(str(grid), str(reference), p['id'], c, expect) for p in reg['configurations'] for c in reg['costs_bps']]
    start = time.perf_counter()
    if workers == 1:
        results = [compare_pair(j) for j in jobs]
    else:
        if not 1 <= workers <= 8:
            raise ValueError('workers must be 1..8')
        ctx = multiprocessing.get_context('spawn')
        with ctx.Pool(workers) as pool:
            results = list(pool.imap_unordered(compare_pair, jobs))
    order = {(p['id'], c): i * len(reg['costs_bps']) + j
             for i, p in enumerate(reg['configurations']) for j, c in enumerate(reg['costs_bps'])}
    results.sort(key=lambda r: order[(r['configuration_id'], r['cost_bps'])])
    failures = [dict(configuration_id=r['configuration_id'], cost_bps=r['cost_bps'], problems=r['problems'])
                for r in results if r['status'] == 'FAIL']
    summary = dict(status='PASS' if not failures else 'FAIL', expect=expect, pairs=len(results), failures=len(failures),
        comparison_items=sum(len(r['files']) + (0 if r['acceptance_all_fields_equal'] is None else 1) for r in results),
        explore_pairs=sum(1 for r in results if r['verification_level'] == 'explore'),
        tables=TABLES, explore_tables=EXPLORE_TABLES, summary_json_included=True,
        grid=str(grid.relative_to(root)) if grid.is_relative_to(root) else str(grid),
        reference=str(reference.relative_to(root)) if reference.is_relative_to(root) else str(reference),
        workers=workers, wall_seconds=round(time.perf_counter() - start, 3), failed_pairs=failures,
        method='logical bytes expanded from lossless encodings, SHA-256 compared; no field selection')
    dump(output, dict(summary=summary, comparisons=results))
    return summary
