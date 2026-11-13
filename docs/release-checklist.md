# Release readiness checklist

This checklist is intentionally separate from implementation completion. A completed roadmap step does not by itself certify a production release.

## Source and metadata

- [ ] Confirm the release version in `pyproject.toml` and `sluice.__version__` is identical.
- [ ] Review `CHANGELOG.md` and migration notes for externally visible changes.
- [ ] Confirm `LICENSE`, `README.md`, typing marker, and Lua scripts are included in the source distribution/wheel as intended.
- [ ] Build both wheel and sdist from a clean checkout; inspect their contents.
- [x] CI installs the built wheel into a clean environment, imports the public API, and checks bundled Lua scripts.

## Correctness and compatibility

- [ ] Run unit tests on Python 3.11, 3.12, and 3.13.
- [ ] Run Redis integration tests against Redis 7.4 (or the declared deployment baseline).
- [ ] Verify atomic weighted admission and NOSCRIPT recovery.
- [ ] Verify hybrid timeout, circuit-open, half-open recovery, and local-fallback failure behavior.
- [ ] Review FastAPI/Starlette compatibility with the supported dependency range.
- [x] Ruff and mypy pass locally and are required CI gates.

## Operational review

- [ ] Decide fail-open, fail-closed, or local-fallback behavior for each deployment.
- [ ] Validate trusted proxy CIDRs and authenticated principal population.
- [ ] Review Redis ACLs, TLS, network access, connection lifecycle, timeouts, and capacity.
- [ ] Export low-cardinality metrics and alert on backend errors/fallback/circuit transitions.
- [ ] Load-test against a real Redis deployment; do not extrapolate local benchmark throughput.
- [ ] Retain benchmark output with Redis version/topology, client count, key cardinality, p50/p95/p99 latency, throughput, and per-key memory results.
- [ ] Review key cardinality, TTL/eviction effects, and Sliding Window Log memory under peak traffic.
- [ ] Document rollback and quota namespace/version compatibility.

## Publication

- [ ] Review changelog and migration guide.
- [ ] Tag the reviewed commit and publish artifacts from CI, not from an untracked local tree.
- [ ] Verify package index metadata, artifact hashes, and install instructions.
- [ ] Announce known limitations and supported runtime/dependency versions.
