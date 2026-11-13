# Step 13 — Benchmarks, docs, migration, and release hardening

## Delivered

- Reworked the benchmark runner to use a deterministic simulated clock with the current operation-oriented `FallbackBackend` API. It reports p50/p95/p99/mean check latency, local throughput, admission counts, approximate local memory, and Python/platform metadata.
- Added a separate concurrent real-Redis runner with multiple client pools, hot-key/cardinality controls, p50/p95/p99 latency, throughput, Redis version, per-key memory, and cleanup scoped to a unique benchmark namespace.
- Added CLI controls for workload duration, quota, window, optional charts, and JSON output.
- Corrected public README statements that described the retired low-level backend protocol and sequential weighted window admission as current behavior.
- Documented FastAPI route dependency usage, optional ASGI middleware, lifecycle ownership, identity/proxy trust, weighted operations, observability, and local fallback limitations.
- Added a backend/API/identity migration guide, release checklist, and initial verified-scope changelog.
- Added a clean-wheel CI smoke check for public API imports and bundled Lua resources; updated package metadata to current SPDX license fields.
- Marked Step 13 implemented in the roadmap while keeping release sign-off explicitly separate.

## Benchmark interpretation

The benchmark is a deterministic, in-process algorithm comparison. It is useful for regression comparisons on the same machine/runtime and for understanding algorithmic admission behavior. It is not a Redis benchmark: it does not include network latency, Redis server load, persistence, cluster topology, or concurrent multi-client contention. Use a dedicated real-Redis load test for deployment sizing.

## Verification

- `compileall` passed for `sluice`, `benchmarks`, and `tests`.
- Focused regression suite: 29 passed.
- Benchmark smoke run completed all 12 algorithm/workload combinations and emitted valid JSON.
- Wheel built successfully; inspection confirmed `py.typed` and all three Lua scripts are included.
- Source distribution built successfully; inspection confirmed docs, benchmarks, tests, and `MANIFEST.in` are included.
- Full pytest collection is blocked in this environment because `fakeredis` is not installed; the two Redis integration modules also skip because `redis` is not installed.
- `python -m build` could not run because the `build` frontend is not installed. The wheel and sdist were nevertheless built directly through the configured setuptools backend.

Live Redis verification, static analysis in an environment with Ruff/mypy installed, and publication are operational release gates, not implied by this documentation step.
