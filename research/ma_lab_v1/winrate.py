"""Trade-level statistics for the moving-average lab: one run of long states is one trade.

Kept outside leadtrend_bt.ma_lab so the registered module stays untouched; the dashboard and the leaderboard
builder both use this file, so the numbers cannot drift apart.
"""
from __future__ import annotations

import numpy as np


def runs(state: list[bool]) -> list[tuple[int, int, bool]]:
    """[(first index, last index, still open)] for every contiguous block of long states."""
    out = []
    entry = None
    for k, on in enumerate(state):
        if on and entry is None:
            entry = k
        elif not on and entry is not None:
            out.append((entry, k, False))
            entry = None
    if entry is not None:
        out.append((entry, len(state) - 1, True))
    return out


def segment_returns(days: list[str], values: np.ndarray, signal_days: list[str],
                    state: list[bool]) -> list[dict]:
    """Total-return change of every long run; an open run is marked to the last stored session."""
    index = {d: i for i, d in enumerate(days)}
    out = []
    for first, last, is_open in runs(state):
        i = index[signal_days[first]]
        j = len(days) - 1 if is_open else index[signal_days[last]]
        if not values[i]:
            continue
        out.append(dict(entry=signal_days[first], exit=days[j], open_end=is_open,
                        bars=last - first + (1 if is_open else 0),
                        ret=float(values[j] / values[i] - 1)))
    return out


def stats(segments: list[dict]) -> dict:
    """Win rate over completed and open trades; profit factor = winning returns / |losing returns|."""
    returns = [item['ret'] for item in segments]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    total_loss = abs(sum(losses))
    return dict(segments=len(returns), wins=len(wins),
                win_rate=(len(wins) / len(returns)) if returns else None,
                avg_win=(sum(wins) / len(wins)) if wins else None,
                avg_loss=(sum(losses) / len(losses)) if losses else None,
                profit_factor=(sum(wins) / total_loss) if total_loss > 0 else None)


def combo_stats(days: list[str], values: np.ndarray, signal_days: list[str], state: list[bool]) -> dict:
    return stats(segment_returns(days, values, signal_days, state))
