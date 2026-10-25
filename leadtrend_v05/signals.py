"""Signal definitions for strategy experiments.

A signal is a plain dict. Its canonical JSON gives a stable key. Momentum with skip 0 is the existing rule and keeps
the existing score tables; every other signal gets its own table named by that key.

Kinds:
- momentum: TRI(t-skip) / TRI(t-skip-L) - 1, with availability checked over the whole window [t-skip-L, t].
- composite: weighted average of cross-sectional rank percentiles of momentum components (rank_mean only).
The random null control is a selection rule (selection.select_random_matched), not a signal.
"""
import hashlib
import json
import math

SCORE_FIELDS = ['signal_session_date', 'security_id', 'issuer_id', 'industry', 'L', 'window_sessions',
                'tri_start', 'tri_end', 'score', 'unscorable_reason', 'rank']


def _int(value, name, minimum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{name} must be an integer')
    if value < minimum:
        raise ValueError(f'{name} must be at least {minimum}')
    return value


def normalize(sig):
    """Canonical form: integers and floats only, lists in their given order, defaults filled in."""
    kind = sig.get('kind')
    if kind == 'momentum':
        return {'kind': 'momentum', 'L': _int(sig['L'], 'L', 1), 'skip': _int(sig.get('skip', 0), 'skip', 0)}
    if kind == 'composite':
        components = sig.get('components')
        if not isinstance(components, list) or not components:
            raise ValueError('composite needs a non-empty components list')
        parts = [normalize(c) for c in components]
        if any(p['kind'] != 'momentum' for p in parts):
            raise ValueError('composite components must be momentum signals')
        weights = sig.get('weights', [1.0] * len(parts))
        if len(weights) != len(parts):
            raise ValueError('composite weights must match components')
        values = [float(w) for w in weights]
        if any(not math.isfinite(w) or w <= 0 for w in values):
            raise ValueError('composite weights must be positive and finite')
        if sig.get('combine', 'rank_mean') != 'rank_mean':
            raise ValueError('only rank_mean is implemented')
        return {'kind': 'composite', 'components': parts, 'weights': values, 'combine': 'rank_mean'}
    if kind == 'peer':
        return {'kind': 'peer', 'L': _int(sig['L'], 'L', 1), 'L2': _int(sig.get('L2', 60), 'L2', 1)}
    if kind == 'vol_adjusted':
        return {'kind': 'vol_adjusted', 'L': _int(sig['L'], 'L', 1), 'vol_L': _int(sig.get('vol_L', 60), 'vol_L', 2)}
    if kind in OVERLAY_KINDS:
        return {'kind': kind, 'L': _int(sig['L'], 'L', 1)}
    raise ValueError(f'unknown signal kind {kind!r}')


def canonical(sig):
    return json.dumps(normalize(sig), sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def signal_key(sig):
    return hashlib.sha256(canonical(sig).encode('utf-8')).hexdigest()[:12]


OVERLAY_KINDS = {'peer', 'sector_demeaned', 'vol_adjusted', 'size_tilt', 'voo_weight'}


def is_default_momentum(sig):
    n = normalize(sig)
    return n['kind'] == 'momentum' and n['skip'] == 0


def main_L(sig):
    """The window length a configuration reports as its L: the first momentum component's L."""
    n = normalize(sig)
    if n['kind'] == 'composite':
        return main_L(n['components'][0])
    return n['L']


def describe(sig):
    n = normalize(sig)
    if n['kind'] == 'momentum':
        return f"动量 L={n['L']}" + (f" 跳过{n['skip']}日" if n['skip'] else '')
    if n['kind'] in OVERLAY_KINDS:
        names = {'peer': '板块同伴', 'sector_demeaned': '板块中性', 'vol_adjusted': '波动调整',
                 'size_tilt': '规模倾斜', 'voo_weight': 'VOO权重'}
        extra = f" 同伴L={n['L2']}" if n['kind'] == 'peer' else (f" 波动窗口={n['vol_L']}" if n['kind'] == 'vol_adjusted' else '')
        return f"{names[n['kind']]} L={n['L']}{extra}"
    parts = ', '.join(describe(c) for c in n['components'])
    return f"复合(rank_mean; 权重 {','.join(str(w) for w in n['weights'])}): {parts}"


def momentum_cell(engine, sid, d, L, skip):
    """(score, tri_end, tri_start, window_sessions, reason) for one security on one signal date."""
    from .scoring import momentum_score
    if skip == 0:
        return engine.score(sid, d, L)
    if d > engine.return_end:
        raise ValueError('holdout signal/return calculation forbidden')
    reason = engine.availability(sid, d, L + skip)
    if reason:
        return (None, None, None, 0, reason)
    result = engine.tri.get(sid)
    if result is None:
        return (None, None, None, 0, 'no_price_data')
    if result.invalid_from is not None and d >= result.invalid_from:
        return (None, None, None, 0, 'unresolved_event_price_gap')
    end = engine.sessions[engine.pos[d] - skip]
    return momentum_score(result.tri, end, L, engine.sessions, session_index=engine.pos,
                          first_valid_price_date=engine.first_price_date[sid], unresolved_from=None)


def composite_cells(sids, component_cells, weights):
    """rank_mean over the securities scorable on every component.

    component_cells: one dict per component (listed order) mapping sid -> cell tuple.
    Returns sid -> (score, None, None, None, reason); the reason of an unscorable security is that of the first
    listed component that cannot score it.
    """
    scorable = [s for s in sids if all(cell[s][0] is not None for cell in component_cells)]
    total = sum(weights)
    normalized = [w / total for w in weights]
    n = len(scorable)
    percentiles = []
    for cell in component_cells:
        order = sorted(scorable, key=lambda s: (-cell[s][0], s))
        percentiles.append({s: ((n - i) / (n - 1) if n > 1 else 1.0) for i, s in enumerate(order, 1)})
    out = {}
    for s in sids:
        if all(cell[s][0] is not None for cell in component_cells):
            score = 0.0
            for w, pct in zip(normalized, percentiles):
                score += w * pct[s]
            out[s] = (score, None, None, None, '')
        else:
            reason = next(cell[s][4] for cell in component_cells if cell[s][0] is None)
            out[s] = (None, None, None, None, reason)
    return out


def ranked(cells):
    """Ranks 1..n over scored securities by (-score, security_id); unscored get None."""
    order = sorted(((cell[0], sid) for sid, cell in cells.items() if cell[0] is not None), key=lambda x: (-x[0], x[1]))
    result = {sid: None for sid in cells}
    for i, (_, sid) in enumerate(order, 1):
        result[sid] = i
    return result


def week_rows(iso, sids, cells, info, L, key=None):
    """Score-table rows for one signal date in the same column order as the existing tables."""
    ranks = ranked(cells)
    rows = []
    for s in sids:
        score, tend, tstart, n, reason = cells[s]
        issuer, industry = info[s]
        row = dict(signal_session_date=iso, security_id=s, issuer_id=issuer, industry=industry, L=L,
                   window_sessions=n, tri_start=tstart, tri_end=tend, score=score, unscorable_reason=reason,
                   rank=ranks[s])
        if key is not None:
            row['signal_key'] = key
        rows.append(row)
    return rows
