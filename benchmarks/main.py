"""Run deterministic local algorithm benchmarks.

Examples:
    python -m benchmarks.main
    python -m benchmarks.main --duration 30 --limit 100 --json benchmark.json
    python -m benchmarks.main --charts ./benchmark-charts
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from benchmarks.report import print_table, save_charts
from benchmarks.runner import ALGORITHMS, BenchmarkResult, run_single
from benchmarks.traffic_patterns import (
    burst_then_idle, poisson, uniform, window_boundary_hammer,
)


async def run_all(
    *, duration: int, limit: int, window_ms: int,
    charts_dir: str | None = None,
) -> list[BenchmarkResult]:
    patterns = {
        "uniform": uniform(rate_per_sec=0.8, duration_sec=duration),
        "burst": burst_then_idle(
            burst_size=max(1, limit // 2), burst_at_ms=5_000,
            idle_rate_per_sec=0.5, duration_sec=duration,
        ),
        "boundary": window_boundary_hammer(
            limit=max(1, limit // 2), window_ms=window_ms, duration_sec=duration,
        ),
        "poisson": poisson(avg_rate_per_sec=0.9, duration_sec=duration),
    }
    results: list[BenchmarkResult] = []
    for pattern_name, requests in patterns.items():
        for algorithm in ALGORITHMS:
            result = await run_single(
                algorithm, pattern_name, requests,
                limit=limit, window_ms=window_ms, capacity=limit,
                refill_rate=limit / (window_ms / 1000),
            )
            results.append(result)
            print(
                f"{algorithm} × {pattern_name}: "
                f"{result.allowed} allowed / {result.rejected} rejected; "
                f"p50={result.latency_p50_us:.1f}µs, "
                f"p95={result.latency_p95_us:.1f}µs, "
                f"p99={result.latency_p99_us:.1f}µs"
            )
    print()
    print_table(results)
    if charts_dir:
        save_charts(results, charts_dir)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Sluice local algorithm benchmark")
    parser.add_argument("--duration", type=int, default=120, help="synthetic workload duration in seconds")
    parser.add_argument("--limit", type=int, default=60, help="quota used for comparison")
    parser.add_argument("--window-ms", type=int, default=60_000, help="window size in milliseconds")
    parser.add_argument("--charts", metavar="DIR", help="optional directory for PNG charts")
    parser.add_argument("--json", metavar="FILE", help="optional path for machine-readable results")
    args = parser.parse_args()
    if args.duration <= 0 or args.limit <= 0 or args.window_ms <= 0:
        parser.error("--duration, --limit, and --window-ms must be positive")

    print("Sluice — deterministic local algorithm benchmark")
    print(
        f"Configuration: limit={args.limit}, window={args.window_ms}ms, "
        f"synthetic duration={args.duration}s"
    )
    results = asyncio.run(run_all(
        duration=args.duration, limit=args.limit, window_ms=args.window_ms,
        charts_dir=args.charts,
    ))
    if args.json:
        output = {
            "benchmark": "sluice-local-algorithms",
            "configuration": vars(args),
            "results": [result.to_dict() for result in results],
            "interpretation": (
                "Latency and throughput describe in-process FallbackBackend execution, "
                "not Redis network or server performance."
            ),
        }
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        print(f"\nJSON results written to {target}")


if __name__ == "__main__":
    main()
