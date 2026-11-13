"""Run concurrent Redis-backed benchmarks against an explicit Redis URL."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from benchmarks.redis_runner import ALGORITHMS, run_redis_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description="Sluice real-Redis benchmark")
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("SLUICE_BENCH_REDIS_URL"),
        help="Redis URL; defaults to SLUICE_BENCH_REDIS_URL",
    )
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--requests-per-client", type=int, default=2_000)
    parser.add_argument(
        "--cardinality", type=int, default=1,
        help="number of identifiers shared by all clients (1 measures hot-key contention)",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--window-ms", type=int, default=60_000)
    parser.add_argument("--cost", type=int, default=1)
    parser.add_argument(
        "--topology", default="unspecified",
        help="descriptive deployment label included in the report (no credentials)",
    )
    parser.add_argument(
        "--algorithm", choices=(*ALGORITHMS, "all"), default="all"
    )
    parser.add_argument("--json", metavar="FILE", help="write machine-readable results")
    args = parser.parse_args()
    if not args.redis_url:
        parser.error("provide --redis-url or set SLUICE_BENCH_REDIS_URL")
    if min(
        args.clients, args.requests_per_client, args.cardinality,
        args.limit, args.window_ms, args.cost,
    ) <= 0:
        parser.error("all counts, limits, window, and cost must be positive")
    if args.cost > args.limit:
        parser.error("--cost cannot exceed --limit")

    algorithms = ALGORITHMS if args.algorithm == "all" else (args.algorithm,)
    reports = asyncio.run(run_redis_benchmark(
        redis_url=args.redis_url,
        algorithms=algorithms,
        clients=args.clients,
        requests_per_client=args.requests_per_client,
        cardinality=args.cardinality,
        limit=args.limit,
        window_ms=args.window_ms,
        cost=args.cost,
        topology=args.topology,
    ))
    print(json.dumps(reports, indent=2))
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
