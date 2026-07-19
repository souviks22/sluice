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

```text
CLOSED ──(N failures)──▶ OPEN ──(timeout)──▶ HALF_OPEN
   ▲                                           │
   └──────────────(probe succeeds)─────────────┘
                       │
                       └──(probe fails)──▶ OPEN
```

* **CLOSED** — all requests use Redis.
* **OPEN** — all requests use the local fallback.
* **HALF_OPEN** — one request probes Redis while all others continue using
  the fallback.

The fallback is intentionally weaker than distributed enforcement: each node
limits independently, so a fleet of *N* nodes can allow up to *N × limit*
during an outage. It trades perfect consistency for continued protection and
availability.

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

### Per-call timeouts

Every Redis operation—including the recovery probe—is wrapped with
`asyncio.wait_for()`. Timeouts count as failures just like connection errors,
allowing hung Redis instances to trip the breaker instead of blocking every
caller indefinitely.

### Recovery jitter

Without jitter, every node would probe Redis at nearly the same moment after
an outage, creating a recovery spike. Randomizing
`recovery_timeout_sec` by `± recovery_jitter_frac` spreads probes across the
fleet.

### Fallback failures

`FallbackBackend` is the final layer. If it fails, the exception is allowed
to propagate rather than silently failing open or closed, ensuring the error
is visible to callers and logs.

---

## Scope

The circuit breaker improves **availability**, not **consistency**. It does
not guarantee:

* Exact fleet-wide limits during a Redis outage (fallback is per-node).
* Redis Cluster failover or topology management.
* Persistence of fallback state across process restarts.
