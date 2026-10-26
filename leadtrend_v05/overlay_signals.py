"""Cross-sectional score overlays for the round-1 signals (PEER, G, VOLA, SIZE, WGT).

Each function takes one signal week's momentum cells and returns cells in the same tuple layout as signals.momentum_cell:
(score, tri_end, tri_start, window_sessions, unscorable_reason). Percentiles are P = (n - i) / (n - 1) over the names
scored in the set named in each docstring; a lone name gets 1. Sector keys come from sectors.sector_key.
"""
import math
from statistics import median

from .selection_structured import percentile_by_rank
from .selection import Candidate

MIN_PEERS = 3          # default minimum number of peers; the signal dict carries min_peers
PEER_WEIGHT = 0.25     # default weight of the peer median; the signal dict carries weight
SIZE_WEIGHT = 0.1      # default weight of the issuer-size percentile; the signal dict carries weight


def _candidates(cells):
    """Scored names of a week as candidates, best first; the keys are the security ids."""
    return sorted((Candidate(sid, '', '', cell[0]) for sid, cell in cells.items() if cell[0] is not None),
                  key=lambda c: c.sort_key)


def scored_percentiles(cells):
    """P_L over the scored names of one cell map."""
    return percentile_by_rank(_candidates(cells))


def _out(cells, scores, base_reason):
    """Cells for one overlay: the momentum window fields are kept; unscored names keep their own reason."""
    out = {}
    for sid, cell in cells.items():
        if sid in scores and scores[sid] is not None:
            out[sid] = (scores[sid], cell[1], cell[2], cell[3], '')
        else:
            out[sid] = (None, None, None, None, cell[4] if cell[0] is None else base_reason)
    return out


def peer(cells, cells2, sectors, weight=PEER_WEIGHT, min_peers=MIN_PEERS):
    """PEER: S = (1 - weight) * P_L + weight * median P_L2 of same-sector peers that are scored at both L and L2.

    Peers are the other names of the same mapped sector in U_L and U_L2. With fewer than min_peers peers, or an unmapped
    sector, S = P_L.
    """
    p_base = scored_percentiles(cells)
    p2 = scored_percentiles(cells2)
    scores = {}
    for sid in p_base:
        peers = [t for t in p_base if t != sid and t in p2 and sectors[t][1] and sectors[t][0] == sectors[sid][0]]
        if not sectors[sid][1] or len(peers) < min_peers:
            scores[sid] = p_base[sid]
        else:
            scores[sid] = (1 - weight) * p_base[sid] + weight * median(p2[t] for t in peers)
    return _out(cells, scores, 'insufficient_history')


def sector_demeaned(cells, sectors):
    """G: M_L minus the median M_L of its sector in U_L (the name itself included); fewer than three members use the U_L median."""
    m_base = {sid: cell[0] for sid, cell in cells.items() if cell[0] is not None}
    overall = median(m_base.values()) if m_base else 0.0
    scores = {}
    for sid in m_base:
        members = [t for t in m_base if sectors[t][0] == sectors[sid][0] and sectors[t][1]]
        if not sectors[sid][1] or len(members) < MIN_PEERS:
            scores[sid] = m_base[sid] - overall
        else:
            scores[sid] = m_base[sid] - median(m_base[t] for t in members)
    return _out(cells, scores, 'insufficient_history')


def vol_adjusted(cells, volatility):
    """VOLA: M_L divided by the sample standard deviation of the last 60 daily TRI log returns; unavailable when that is missing or zero."""
    scores = {}
    for sid, cell in cells.items():
        if cell[0] is None:
            continue
        sigma = volatility.get(sid)
        scores[sid] = cell[0] / sigma if sigma else None
    return _out(cells, scores, 'vol_unavailable')


def size_tilt(cells, issuer_weight, weight=SIZE_WEIGHT):
    """SIZE: (1 - weight) * P_L + weight * Q, Q the percentile of the issuer's VOO weight over U_L (ties by security id)."""
    p_base = scored_percentiles(cells)
    order = sorted(p_base, key=lambda sid: (-issuer_weight.get(sid, 0.0), sid))
    n = len(order)
    q = {sid: ((n - i) / (n - 1) if n > 1 else 1.0) for i, sid in enumerate(order, 1)}
    scores = {sid: (1 - weight) * p_base[sid] + weight * q[sid] for sid in p_base}
    return _out(cells, scores, 'insufficient_history')


def voo_weight(cells, issuer_weight):
    """WGT: the issuer's summed VOO weight in the snapshot used that week; a reference signal only."""
    return _out(cells, {sid: issuer_weight.get(sid, 0.0) for sid, cell in cells.items() if cell[0] is not None},
                'insufficient_history')


def daily_log_volatility(tri, sessions, index, window=60):
    """Sample standard deviation of the last `window` daily log returns of a TRI series, or None if any value is missing.

    tri: date -> TRI value; sessions: all sessions in order; index: position of the signal session in sessions.
    """
    if index < window:
        return None
    values = []
    for j in range(index - window, index + 1):
        v = tri.get(sessions[j])
        if v is None or v <= 0:
            return None
        values.append(v)
    returns = [math.log(values[i] / values[i - 1]) for i in range(1, len(values))]
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sigma = math.sqrt(var)
    return sigma if sigma > 1e-12 else None  # below float noise a series is flat
