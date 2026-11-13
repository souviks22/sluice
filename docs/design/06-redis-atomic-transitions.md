# Redis atomic transitions (Step 6)

`RedisBackend.check()` now dispatches exactly one script call. The scripts read
Redis server time internally and perform one complete state transition.

| Algorithm | Redis state | Weighted admission |
|---|---|---|
| Token bucket | One hash (`tokens`, `last_refill_ms`) | Consume `cost` only if the refilled balance covers it |
| Sliding-window log | One sorted set | Insert `cost` timestamped members only if cardinality + cost fits |
| Sliding-window counter | One hash (`bucket_id`, `current`, `previous`) | Add `cost` only if the interpolated estimate + cost fits |

All scripts use a single key, so the operation does not require multiple Redis
Cluster hash slots. Redis `TIME` replaces application-side clock retrieval.
The legacy `now_ms()` method remains for compatibility but is not used by the
new admission path.

A cost greater than the configured capacity/limit is rejected during operation
construction because it can never be admitted. `FallbackBackend.check()` now
implements the same all-or-nothing weighted admission semantics under a
process-local lock. The legacy `now_ms()` and `evalsha()` compatibility methods
remain available, but new algorithm paths use the complete `check()` operation.

Each script accesses one Redis key, so its key is inherently in one Cluster
hash slot. This is a necessary property for Cluster execution, not by itself
proof of Redis Cluster support: cluster-aware script loading, redirection,
failover, and integration behavior still need validation.
