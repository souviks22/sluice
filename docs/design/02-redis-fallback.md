# Redis Fallback with Circuit Breaker

## Why?

Every algorithm in **sluice** depends on Redis for shared state. That's what
allows limits to be enforced consistently across a fleet, but it also places
Redis directly in the request path of every rate-limited call.

When Redis becomes unavailable, two naive strategies exist:

* **Fail open** — allow every request. This removes rate limiting entirely,
  often during the exact period when abusive traffic is most likely.
* **Fail closed** — reject every request. A Redis outage becomes a full
  outage for services that otherwise have nothing to do with Redis.

Neither is acceptable for infrastructure in the critical path.

---

## The Problem

Wrapping every Redis call in `try/except` and falling back to local state
sounds simple, but it breaks down in practice:

1. **Slow isn't the same as failed.** A hung Redis or exhausted connection
   pool may never raise an exception—it simply blocks every request.

2. **Transient failures shouldn't change global behavior.** One dropped
   connection shouldn't immediately switch the system into degraded mode.
   The breaker should react only to sustained failures.

3. **Recovery introduces races.** Requests issued before recovery may finish
   afterward. Without careful state tracking, these stale completions can
   incorrectly re-open an already healthy circuit.

---

## The Solution

`HybridBackend` wraps `RedisBackend` with a **three-state circuit breaker**
and a bounded in-memory **FallbackBackend** implementing the same algorithms.
`HybridBackend(failure_policy="local_fallback")` selects per-process local
fallback semantics and remains the compatibility default. Set
`failure_policy="fail_closed"` to raise `BackendUnavailableError` after a Redis
failure or while the circuit is open. `fail_open` is an application-boundary
choice: the middleware supports it and skips enforcement when backend
evaluation fails, without fabricating quota headers or a successful result.

```text
CLOSED ──(N failures)──▶ OPEN ──(timeout)──▶ HALF_OPEN
   ▲                                           │
   └──────────────(probe succeeds)─────────────┘
                       │
                       └──(probe fails)──▶ OPEN
```

* **CLOSED** — requests try Redis. If an individual Redis call fails or times
  out, that operation immediately falls back locally. The breaker stays closed
  until `failure_threshold` consecutive Redis failures have occurred, so the
  next operation still tries Redis before falling back.
* **OPEN** — requests use the local fallback under `local_fallback`, or raise
  `BackendUnavailableError` under `fail_closed`.
* **HALF_OPEN** — one request probes Redis; other requests follow the selected
  failure policy while that probe is in flight.

Under `local_fallback`, the fallback is intentionally weaker than distributed
enforcement: each node limits independently, so a fleet of *N* nodes can allow
up to *N × limit* during an outage. It trades perfect consistency for
continued protection and availability.

### Separate state machine

`CircuitBreaker` is a synchronous, I/O-free state machine. `HybridBackend`
only asks whether Redis should be used and reports the outcome afterward.

Because its methods contain no `await`, state transitions execute atomically
under asyncio without requiring locks, making correctness easier to maintain.

### Generation fencing

Requests can complete under a different circuit state than the one in which
they started. Each state transition increments a `generation` counter, and
every request records the generation at dispatch. Results are applied only if
their generation still matches the current one; otherwise they're discarded.
This prevents stale completions from corrupting a recovered circuit.

### Retry and timeout semantics

Every Redis operation—including the recovery probe—is wrapped with
`asyncio.wait_for()`. A timeout counts as a Redis failure. `HybridBackend` does
not retry a failed admission against Redis: it sends that operation to the
local backend once. The Redis backend has its own narrow recovery behavior for
`NOSCRIPT`: it reloads the Lua script and retries that Redis command once.

A timeout has an ambiguous outcome. Redis may have executed and committed the
Lua admission before the response was lost or before cancellation took effect.
The local fallback cannot determine that outcome, so one logical request may
consume quota in both Redis and local state. Sluice does not currently provide
operation IDs or cross-backend deduplication; do not treat hybrid admission as
exactly once.

The breaker counts consecutive Redis failures and resets the count after a
successful Redis call. At the threshold it opens and sends requests directly
to local state until the recovery deadline. In half-open, one caller probes
Redis while the others use local state. A successful probe closes the breaker;
a failed probe opens it again with a new recovery delay. The threshold controls
when the breaker stops trying Redis; it does not delay local fallback for a
call that already failed.

### Recovery jitter

Without jitter, every node would probe Redis at nearly the same moment after
an outage, creating a recovery spike. Randomizing
`recovery_timeout_sec` by `± recovery_jitter_frac` spreads probes across the
fleet.

### Fallback failures

Under `local_fallback`, `FallbackBackend` is the final layer. If it fails, the
exception propagates rather than silently allowing the request. Under
`fail_closed`, local admission is never attempted after a Redis failure.

---

## Scope

The circuit breaker controls backend routing; the selected policy determines
what happens when Redis cannot evaluate a request. `local_fallback` improves
availability by continuing local quota checks. `fail_closed` preserves the
requirement that every accepted request has a Redis decision, at the cost of
availability. `fail_open` is available at the middleware boundary and skips
the check. None of these options makes a timed-out operation exactly once. In
particular, local fallback does not guarantee:

* Exact fleet-wide limits during a Redis outage (fallback is per-node).
* Redis Cluster failover or topology management.
* Persistence of fallback state across process restarts.
