# Sluice transformation roadmap

This roadmap tracks the staged transformation of Sluice into a production-grade, FastAPI-first rate limiter.

| Step | Deliverable | Status |
|---|---|---|
| 1 | Production architecture contract | Complete |
| 2 | Baseline inventory and compatibility map | Complete (runtime test baseline blocked by missing dependencies) |
| 3 | Reproducible engineering environment and baseline harness | Complete (static setup; runtime verification pending in this environment) |
| 4 | Core domain model and validation | Complete (domain models and validation added; algorithm execution migration remains staged) |
| 5 | Operation-oriented backend execution contract | Complete (typed operation API and whole-operation hybrid routing; atomic internals deferred to Steps 6–7) |
| 6 | Correct Redis algorithms and scripts | Complete (Redis TIME, one-script admission, weighted all-or-nothing transitions) |
| 7 | Local backend and explicit degradation semantics | Complete (process-local atomic weighted admission, bounded state, and documented failover trade-offs) |
| 8 | FastAPI route dependencies and policy registry | Complete (route-template-aware dependency, immutable snapshots, isolated key namespaces, explicit backend-error mode) |
| 9 | Optional ASGI middleware and HTTP standards | Complete (pure ASGI implementation, standards headers, explicit failure mode, and focused tests) |
| 10 | Identity, proxy trust, and key design | Complete (trusted-proxy allowlist, authenticated-principal extraction, HMAC identifiers) |
| 11 | Observability and operational controls | Complete (low-cardinality observer API, metrics collector, admission latency, hybrid degradation signals) |
| 12 | Integration, concurrency, and chaos testing | Complete (pytest provisions disposable Redis/Toxiproxy services; full local suite passed) |
| 13 | Benchmarks, docs, migration, and release hardening | Implemented (local benchmark harness, corrected public docs, migration/release guidance; release validation remains deployment-dependent) |

**Working agreement:** one step at a time. Each step produces a reviewable artifact, runs its relevant verification, and records any blocker before the next step begins.
