# Step 4 — Core domain model and validation

**Status:** Domain model delivered; full algorithm execution migration intentionally staged for the backend/algorithm steps.

## Delivered

- Added backend/framework-independent `sluice.domain`.
- Added immutable common, token-bucket, and sliding-window configuration dataclasses.
- Added immutable `RateLimitResult` with validation.
- Added `SluiceError`, configuration, identifier, and cost exceptions.
- Added shared strict validation helpers.
- Added `Clock` protocol and `SystemClock`.
- Added constructor-time validation to all three existing algorithm classes.
- Added check-time identifier and cost validation.
- Preserved `RateLimitResult` import compatibility from `sluice.algorithms.base`.
- Exported domain configuration and exception types from the package root.
- Added pure domain tests and domain design documentation.

## Important staged limitation

The current legacy sliding-window algorithms still execute weighted costs as sequential script calls. This is not an acceptable final atomicity guarantee. The new domain contract explicitly requires all-or-nothing weighted admission; Step 5 replaces the backend operation contract, and Step 6 implements the atomic Redis state transitions. The legacy behavior is not represented as production-ready.

## Verification

- Static syntax compilation and TOML validation are used for this step.
- Full pytest execution remains blocked in this environment because `redis` and `fakeredis` are not installed; no behavioral test pass is claimed.
- CI from Step 3 will install dependencies and run the pure-domain and full unit suites.

## Exit criteria

- [x] Immutable domain configuration value objects.
- [x] Immutable and validated result model.
- [x] Domain exception hierarchy.
- [x] Shared validation for cost, identifier, and key namespace.
- [x] Clock abstraction.
- [x] Constructor validation on existing algorithms.
- [x] Pure domain tests added.
- [ ] Weighted admission fully atomic — Step 6.
- [ ] Algorithm configs fully consumed by algorithm constructors — follow-up during API migration.
