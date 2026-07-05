# Atomic Lua Scripts

## Why?

A distributed rate limiter maintains shared state in Redis. Every request updates the same bucket or window, potentially from multiple application instances.

Regardless of the algorithm (Token Bucket, Sliding Window Log, Sliding Window Counter, Fixed Window), every request follows the same logical operation:

```text
Read state
    ↓
Compute new state
    ↓
Validate request
    ↓
Persist updated state
```

If these steps are executed as separate Redis commands, they are vulnerable to a **read-modify-write race condition**.

---

## The Problem

Assume a token bucket contains one remaining token.

Two application nodes receive requests simultaneously.

```text
Time →

Node A: GET tokens → 1
Node B: GET tokens → 1

Node A: SET tokens = 0 ✓
Node B: SET tokens = 0 ✓
```

Both nodes observed the same state before either wrote its update.

As a result, **both requests are allowed**, even though only one token existed.

The final Redis state appears correct (`tokens = 0`), but two requests have been admitted.

This race exists whenever reading, computing, and writing occur as independent operations.

---

## The Solution

sluice executes every rate limiting algorithm as a Redis Lua script.

Redis guarantees that a Lua script executes atomically: once execution begins, no other client command can run until the script completes.

Each script performs the complete state transition:

1. Read the current state.
2. Refill or update the algorithm state.
3. Decide whether the request should be allowed.
4. Persist the updated state.
5. Refresh the key expiration.

Since these steps execute as a single Redis operation, no other client can observe or modify the state between the read and the write.

Revisiting the previous example:

```text
Time →

Node A:
EVALSHA
    read tokens = 1
    write tokens = 0
→ allowed

Node B:
(wait)

EVALSHA
    read tokens = 0
→ rejected
```

Only one request is admitted, preserving the configured rate limit.

---

## Why `EVALSHA`?

Scripts are loaded during backend initialization using `SCRIPT LOAD`, and their SHA-1 digests are cached.

Subsequent executions use `EVALSHA` instead of sending the entire script source on every request. If Redis loses its script cache (for example, after a restart or `SCRIPT FLUSH`), the backend transparently reloads the script and retries the operation.

---

## Scope

Lua atomicity guarantees correctness only within a single Redis node.

It does **not** address:

* Redis Cluster hash-slot constraints for multi-key scripts.
* Clock skew between application nodes.
* Redis availability or failover.

