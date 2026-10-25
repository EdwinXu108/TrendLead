"""Engineering measurements: process memory is read, recorded outside completion.json, and in bytes."""
import json
import os
import sys

import pytest

from leadtrend_v05 import perf_runner
from leadtrend_v05.perf_bench import peak_working_set, working_sets


@pytest.mark.skipif(sys.platform != 'win32', reason='GetProcessMemoryInfo is Windows-only')
def test_working_sets_read_current_and_peak_in_bytes():
    current, peak = working_sets()
    assert current > 1_000_000 and peak >= current
    ballast = bytearray(b'x') * (64 * 1024 * 1024)  # written, not just reserved, so the pages count toward the working set
    grown, grown_peak = working_sets()
    assert grown >= current + 32 * 1024 * 1024
    assert grown_peak >= grown and peak_working_set() >= grown_peak
    del ballast


def test_worker_record_adds_process_fields_and_leaves_the_result_unchanged():
    result = dict(configuration_id='L020_N10_b2_R1', cost_bps=5, status='completed')
    record = perf_runner.worker_record(result)
    assert record['worker_pid'] == os.getpid()
    assert record['status'] == 'completed' and record['cost_bps'] == 5
    assert 'worker_pid' not in result  # the caller's dict is not mutated
    assert json.loads(json.dumps(record))['worker_pid'] == os.getpid()


@pytest.mark.skipif(sys.platform != 'win32', reason='GlobalMemoryStatusEx is Windows-only')
def test_available_physical_memory_and_sampler_record_the_minimum():
    import time
    from leadtrend_v05.perf_bench import available_physical
    value = available_physical()
    assert isinstance(value, int) and value > 0
    sampler = perf_runner._MemorySampler()
    sampler.start()
    time.sleep(0.6)
    sampler.stop()
    assert sampler.low is not None and sampler.low >= 0


def test_auto_workers_respects_reserve_memory_cpus_and_cap():
    from leadtrend_v05.perf_runner import AUTO_RESERVE_BYTES, AUTO_WORKER_CAP, WORKER_PEAK_BYTES, auto_workers
    gib = 2 ** 30
    explore, daily = WORKER_PEAK_BYTES['explore'], WORKER_PEAK_BYTES['daily']
    assert auto_workers(None, explore, 20) == 1  # unknown memory: serial
    assert auto_workers(20 * gib, explore, 20) == AUTO_WORKER_CAP  # plenty of memory: the measured speed cap
    assert auto_workers(20 * gib, explore, 2) == 2  # CPU-limited
    assert auto_workers(AUTO_RESERVE_BYTES + gib, daily, 20) == 1  # only one daily worker fits beside the reserve
    assert auto_workers(AUTO_RESERVE_BYTES + 3 * daily, daily, 20) == 3  # memory-limited
    assert auto_workers(AUTO_RESERVE_BYTES // 2, daily, 20) == 1  # never below one worker


def test_cli_accepts_auto_and_integer_workers():
    from leadtrend_v05.__main__ import build_parser
    assert build_parser().parse_args(['run-grid', '--verify', 'explore', '--workers', 'auto']).workers == 'auto'
    assert build_parser().parse_args(['run-grid', '--workers', '4']).workers == 4
    assert build_parser().parse_args(['bench', '--mode', 'small_check', '--workers', 'auto']).workers == 'auto'
