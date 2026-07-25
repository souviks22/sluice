"""
Run all benchmark scenarios and print a comparison report.

Usage::
    python -m benchmarks.main

    # Or with output directory for charts:
    python -m benchmarks.main --charts ./charts
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import os

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from benchmarks.runner import ALGORITHMS, BenchmarkResult, run_single
from benchmarks.report import print_table, save_charts
from benchmarks.traffic_patterns import (
    uniform,
    burst_then_idle,
    window_boundary_hammer,
    poisson,
)


# ── Benchmark configuration ────────────────────────────────────────────────
LIMIT       = 60        # requests per window
WINDOW_MS   = 60_000    # 60-second window
CAPACITY    = 60        # token bucket capacity (= limit for fair comparison)
REFILL_RATE = 1.0       # tokens/sec (= LIMIT / (WINDOW_MS/1000))
DURATION    = 120       # seconds of simulated traffic


async def run_all(charts_dir: str | None = None) -> list[BenchmarkResult]:
    results: list[BenchmarkResult] = []

    patterns = {
        "uniform":    uniform(rate_per_sec=0.8, duration_sec=DURATION),
        "burst":      burst_then_idle(burst_size=40, burst_at_ms=5000,
                                      idle_rate_per_sec=0.5, duration_sec=DURATION),
        "boundary":   window_boundary_hammer(limit=LIMIT // 2, window_ms=WINDOW_MS,
                                             duration_sec=DURATION),
        "poisson":    poisson(avg_rate_per_sec=0.9, duration_sec=DURATION),
    }

    total = len(ALGORITHMS) * len(patterns)
    done = 0

    for pat_name, pat_requests in patterns.items():
        for algo in ALGORITHMS:
            print(f"  [{done+1}/{total}] {algo} × {pat_name} "
                  f"({len(pat_requests)} requests)...", end=" ", flush=True)
            r = await run_single(
                algo, pat_name, pat_requests,
                limit=LIMIT, window_ms=WINDOW_MS,
                capacity=CAPACITY, refill_rate=REFILL_RATE,
            )
            results.append(r)
            print(f"allowed={r.allowed} rejected={r.rejected} "
                  f"mem={r.memory_bytes}B p50={r.latency_p50_us:.0f}µs")
            done += 1

    print()
    print_table(results)

    if charts_dir:
        save_charts(results, charts_dir)

    _print_analysis(results)
    return results


def _print_analysis(results: list[BenchmarkResult]) -> None:
    print("\n" + "=" * 70)
    print("ANALYSIS SUMMARY")
    print("=" * 70)

    # Burst tolerance
    burst_res = {r.algorithm: r for r in results if r.pattern_name == "burst"}
    if burst_res:
        print("\n▶ Burst Tolerance (40-request burst at t=5s, then 0.5 req/s):")
        for algo, r in burst_res.items():
            from benchmarks.report import ALGO_LABELS
            print(f"   {ALGO_LABELS[algo]:<28}: {r.allowed:3d} allowed / "
                  f"{r.rejected:3d} rejected  ({r.allow_rate*100:.1f}%)")
        tb = burst_res.get("token_bucket")
        swl = burst_res.get("sliding_window_log")
        if tb and swl:
            delta = tb.allowed - swl.allowed
            print(f"   → Token Bucket admitted {delta:+d} more than Sliding Window Log")
            print(f"     (bucket absorbs burst; log enforces exact per-window count)")

    # Boundary spike
    boundary_res = {r.algorithm: r for r in results if r.pattern_name == "boundary"}
    if boundary_res:
        print("\n▶ Window Boundary Hammer (2× limit around each boundary):")
        for algo, r in boundary_res.items():
            from benchmarks.report import ALGO_LABELS
            print(f"   {ALGO_LABELS[algo]:<28}: {r.allowed:3d} allowed  ({r.allow_rate*100:.1f}%)")
        tb  = boundary_res.get("token_bucket")
        swl = boundary_res.get("sliding_window_log")
        swc = boundary_res.get("sliding_window_counter")
        if swl and swc:
            print(f"   → Log is exact; Counter approximates (can admit "
                  f"~{abs(swl.allowed - swc.allowed)} extra across run)")

    # Memory
    print("\n▶ Memory Footprint (peak Redis bytes, single key):")
    for algo in ["token_bucket", "sliding_window_log", "sliding_window_counter"]:
        algo_res = [r for r in results if r.algorithm == algo]
        peak = max(r.memory_bytes for r in algo_res) if algo_res else 0
        from benchmarks.report import ALGO_LABELS
        print(f"   {ALGO_LABELS[algo]:<28}: {peak:6d} bytes")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="distrl benchmark suite")
    parser.add_argument("--charts", metavar="DIR", default="./charts",
                        help="Directory to save chart PNGs (default: ./charts)")
    args = parser.parse_args()

    print("=" * 70)
    print("sluice — Distributed Rate Limiter Benchmark")
    print("=" * 70)
    print(f"Config: limit={LIMIT} window={WINDOW_MS}ms duration={DURATION}s\n")

    asyncio.run(run_all(charts_dir=args.charts))
