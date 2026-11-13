# Changelog

All notable changes to Sluice are documented here. This file begins with the current hardening work; historical entries should be added from verified release notes rather than reconstructed from memory.

## Unreleased — production hardening

- Established the operation-oriented backend contract and retained the legacy `now_ms` / `evalsha` protocol for migration compatibility.
- Added Redis Lua admission paths using server time and atomic weighted quota transitions.
- Added bounded process-local fallback and explicit hybrid circuit-breaker degradation behavior.
- Added FastAPI route dependencies, optional pure-ASGI middleware, trusted-proxy-aware identity helpers, and HMAC identifier pseudonymization.
- Added low-cardinality observability interfaces and integration/concurrency/failure-mode tests.
- Made hybrid failure policy explicit, added typed fail-closed backend errors, and documented timeout ambiguity.
- Bounded algorithm numeric inputs for exact Lua arithmetic and corrected weighted sliding-window retry estimates.
- Stabilized route-based identifiers to use resolved route templates and exposed local fallback capacity-eviction counts.
- Reworked the local benchmark harness and added a concurrent real-Redis benchmark for latency percentiles, throughput, and per-key memory.
- Added dependency compatibility coverage, a clean wheel import/resource smoke check, and modern license metadata.
- Cleared the current Ruff/mypy findings and made both static checks blocking CI gates.
- Rewrote the public README and added migration and release-readiness documentation.

No package version bump is implied by this unreleased entry.
