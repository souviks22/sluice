from __future__ import annotations

import pytest

from benchmarks.runner import percentile, run_single
from benchmarks.traffic_patterns import uniform


def test_percentile_uses_nearest_rank_and_empty_input() -> None:
    assert percentile([], 0.99) == 0.0
    assert percentile([4, 1, 3, 2], 0.50) == 2
    assert percentile([4, 1, 3, 2], 0.95) == 4


def test_percentile_rejects_invalid_quantile() -> None:
    with pytest.raises(ValueError):
        percentile([1], 1.1)


@pytest.mark.asyncio
async def test_local_benchmark_returns_consistent_counts_and_metadata() -> None:
    requests = uniform(rate_per_sec=2, duration_sec=2)
    result = await run_single(
        "sliding_window_log", "uniform", requests,
        limit=2, window_ms=1_000,
    )
    assert result.total_requests == len(requests)
    assert result.allowed + result.rejected == result.total_requests
    assert 0 <= result.latency_p50_us <= result.latency_p99_us
    assert result.throughput_ops_sec > 0
    assert result.meta["backend"] == "FallbackBackend"
    assert result.to_dict()["allow_rate"] == result.allow_rate
