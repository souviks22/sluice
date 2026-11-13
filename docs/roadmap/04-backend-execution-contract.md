# Step 5 — Operation-oriented backend execution contract

**Status: Complete — contract and routing boundary established; atomic backend
internals intentionally deferred.**

Delivered:
- Immutable `RateLimitOperation` with algorithm, fully namespaced key,
  validated config type, and positive weighted cost.
- New `RateLimitBackend.check(operation) -> RateLimitResult` protocol.
- Separate optional lifecycle protocol and named legacy script protocol.
- Algorithms migrated to submit one operation instead of independently
  requesting time and invoking scripts.
- Redis and local implementations adapted behind one centralized transitional
  bridge.
- Hybrid backend routes the entire `check` operation through one circuit
  breaker decision.
- Domain tests cover operation immutability and algorithm/config mismatch.

Explicitly not delivered in this step: server-side time, atomic weighted
admission, and local all-or-nothing weighted admission. The bridge retains the
old two-step execution behavior. Those semantics are the focus of Steps 6 and 7.

Verification: `compileall` passed; isolated domain suite passed (30 tests).
Full backend/integration suite remains blocked in this environment by missing
`redis` and `fakeredis` dependencies.
