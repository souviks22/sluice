# Atomic Redis execution

Each Redis-backed admission is a single Lua script invocation. The script reads
its state, obtains the authoritative clock from `redis.call('TIME')`, decides
whether the full weighted cost fits, and commits state only on admission (plus
normal expiry maintenance). No application-node clock participates in a Redis
admission decision.

## Algorithm transitions

- **Token bucket:** refill from Redis time, compare the available token balance
  with the complete requested cost, and subtract the cost only when it fits.
  The last-refill timestamp is monotonic per key to tolerate a backwards host
  clock adjustment.
- **Sliding-window log:** prune expired sorted-set members, compare cardinality
  plus cost with the limit, and insert all cost units in the same script only
  when the complete request fits.
- **Sliding-window counter:** keep current and previous bucket counts in one
  hash key, interpolate the previous bucket's contribution, and add the entire
  cost only when the weighted estimate remains within the limit.

The scripts use one Redis key apiece, avoiding cross-slot multi-key operations
in Redis Cluster. The counter stores both buckets as fields in one hash rather
than dynamically generating separate keys.

## Script lifecycle

Scripts are loaded during backend initialization with `SCRIPT LOAD`; normal
execution uses `EVALSHA`. If Redis has flushed its script cache, the backend
reloads the script and retries. A script-cache miss is a recovery path, not a
separate admission operation.

## Guarantees and boundaries

Redis serializes each Lua invocation, so concurrent callers cannot interleave
within one admission. Weighted admission is all-or-nothing for the Redis
backend. Sliding-window counter remains intentionally approximate by design;
atomicity does not remove its interpolation error. Redis failover durability,
replication consistency, local fallback semantics, and operational policy are
separate concerns handled by later roadmap steps.
