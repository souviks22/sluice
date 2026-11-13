# Development and verification

## Supported baseline

- Python: 3.11, 3.12, and 3.13.
- FastAPI: `>=0.116,<1`; Starlette is resolved through FastAPI to avoid an independently pinned incompatible version.
- Redis Python client: `>=6.2,<8`.
- Redis server integration baseline: Redis 7.4.
- Unit tests use `fakeredis`; real Redis and fault-injection tests provision
  disposable Docker services when run locally.

These are the project's tested compatibility targets, not a claim that every possible combination has been certified.

## Install

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Full test suite

```bash
python -m pytest -q
```

The integration test module starts a disposable Redis 7.4 container when
`SLUICE_TEST_REDIS_URL` is unset, then starts separate Redis/Toxiproxy
containers for restart, network-partition, latency, and packet-loss cases.
Pytest fixtures stop the containers and remove networks after each run. Docker
must be available to the test process, and localhost ports `6379`, `6380`–`6383`,
and `8474` must be free. To use an existing disposable Redis service for the
ordinary Redis integration cases, set `SLUICE_TEST_REDIS_URL`; the four
fault-injection tests still create their own isolated Docker resources.

## Static checks and package build

```bash
ruff check sluice tests
mypy sluice
python -m build
```

Ruff and mypy run as blocking CI checks. Fix or explicitly document any new findings before merging.

## CI gates

- Unit tests run on Python 3.11, 3.12, and 3.13.
- A separate job exercises a real Redis 7.4 service.
- Distribution build verifies packaging metadata and package-data inclusion.
- Package CI installs the built wheel into a clean environment and imports it
  with all bundled Lua scripts present.
- Ruff and mypy are required CI gates.


## Local algorithm benchmark

From the repository root:

```bash
python -m benchmarks.main --duration 30 --json benchmark-results.json
```

The harness uses deterministic synthetic timestamps and the in-process
`FallbackBackend`. It measures local algorithm overhead only; it is not a
substitute for load testing against a real Redis deployment. Install the
optional `bench` extra and pass `--charts ./charts` to generate PNG charts.

For concurrent Redis-backed measurements against a test or staging Redis URL:

```bash
SLUICE_BENCH_REDIS_URL=redis://localhost:6379/15 \
  python -m benchmarks.redis_main --clients 8 --requests-per-client 2000 \
  --cardinality 1 --json redis-benchmark.json
```

The Redis runner reports throughput, p50/p95/p99 latency, and memory for its
unique benchmark keys, then deletes only those keys. Cardinality `1` measures
hot-key contention; increase it to measure state growth across identifiers.
Run it only against a disposable test or staging database with production-like
network topology, and pass `--topology` to record a non-sensitive label in the
report. The runner targets standalone Redis URLs; local results do not certify
deployment capacity.
