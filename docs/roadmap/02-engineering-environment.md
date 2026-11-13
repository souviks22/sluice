# Step 3 — Reproducible engineering environment

**Status:** Configuration and CI harness added. Local runtime verification is limited by the current execution environment; CI is configured to perform the authoritative matrix runs.

## Changes

- Corrected the development test client dependency from `httpx2` to `httpx`.
- Aligned runtime `__version__` with project metadata at `0.1.3`.
- Removed the independent Starlette lower-bound dependency; FastAPI owns its compatible Starlette range.
- Added bounded supported dependency ranges for Redis, FastAPI, cachetools, and development tools.
- Added `sluice/py.typed` and included it in package data.
- Added pytest markers for live Redis and slow tests.
- Added disposable Redis 7.4 Docker Compose service.
- Added a real Redis ping/Lua smoke test, with Redis provisioned by pytest when
  no `SLUICE_TEST_REDIS_URL` is supplied.
- Added CI jobs for Python 3.11–3.13 unit tests, real Redis integration, static analysis, and distribution builds.
- Added local development and verification instructions.

## Gate policy

Unit tests, Redis integration, package build, Ruff, and mypy are blocking CI gates. The initial static-analysis findings have been resolved and both checks now run as required gates.

## Verification history

At this roadmap step, the container lacked `redis` and `fakeredis`, so pytest
could not collect the suite. The current integration harness provisions its own
disposable Redis and chaos services inside pytest. A later full local run passed
all 212 collected tests; CI continues to cover the declared Python matrix.

## Exit criteria

- [x] Dependency metadata corrected and bounded.
- [x] Python compatibility matrix declared.
- [x] Local disposable Redis service defined.
- [x] Real Redis smoke test added.
- [x] CI workflows added.
- [x] Package typing marker included.
- [x] Development commands documented.
- [ ] Full test matrix green — pending CI execution.
- [ ] Commit a generated `uv.lock` — blocked by unavailable package registry/network in this execution environment.
- [ ] Static analysis promoted to blocking — intentionally deferred until legacy cleanup.

## Next

**Step 4 — Core domain model and validation.** Introduce immutable configuration and result models, strict positive/finite validation, normalized cost semantics, and deterministic clock abstractions. No FastAPI surface redesign in that step.
