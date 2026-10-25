"""Fast scores against ScoreEngine on the real development data: every fast cell equal in value and repr."""
from pathlib import Path

import pytest

from leadtrend_v05.perf_cache import build_context

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = [10, 20, 40, 60, 90, 126]


@pytest.mark.skipif(not (ROOT / 'research/v05/registry/stage4_registry.json').exists(),
                    reason='real development data not present')
def test_fast_path_equals_score_engine_on_real_development_data():
    ctx = build_context(ROOT, 'uniform', check_fast=True)  # raises on the first differing cell
    stats = ctx.cell_stats
    cells = sum(len(frame['chosen']) for frame in ctx.frames.values()) * len(WINDOWS)
    assert stats['fast'] + stats['fallback'] == cells  # every signal week x chosen security x window was scored
    assert stats['fast'] > 0 and stats['fallback'] > 0
