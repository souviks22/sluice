# Clock skew

This is the distributed systems issue most rate limiter documentation ignores.

**Observed benchmark results** (±500ms skew, 3 nodes, 60-second window):

| Algorithm              | Over-admission |
|------------------------|---------------|
| Token Bucket           | +85%          |
| Sliding Window Log     | +25%          |
| Sliding Window Counter | +25%          |

The token bucket result is counterintuitive. The mechanism: if node A has a
clock 500ms ahead of node B, and B last wrote `last_refill_ms = t - 500` while
A now reads with `now_ms = t + 500`, the elapsed time in the refill formula
is inflated by 1 full second. At `refill_rate = 1.0 token/sec`, that's one
phantom token per skewed request pair. Across 3 nodes with ±500ms skew, this
compounds.

The window algorithms are less sensitive here specifically because ±500ms skew
on a 60-second window doesn't change the bucket ID (`floor(t / 60000)`). Skew
only matters for requests near window boundaries. The +25% result is from the
multi-node traffic volume being 3× expected, not from the skew itself — i.e.,
our test wasn't perfectly controlled. A single-node skew test would show much
lower numbers for window algorithms.

**The practical fix**: use Redis's own `TIME` command as the authoritative clock.
Set `use_server_time=True` on any limiter. Cost: one extra Redis round-trip per
request (typically +0.1–0.5ms on a local Redis; +1–5ms cross-datacenter). This
eliminates cross-node clock divergence entirely since all nodes use the same
clock source.

NTP sync within 100ms is sufficient to make clock-skew effects negligible for
window algorithms. Token bucket is more sensitive — NTP within 10ms is recommended
if not using server time.


## What this library doesn't do

**No Redis Cluster support.** Lua scripts with `EVALSHA` are atomic on a single
Redis node. In a Redis Cluster, all keys in a Lua script must hash to the same
slot. The current key scheme (`rl:tb:{identifier}`) doesn't guarantee this for
multi-key scripts. For Redis Cluster: use hash tags (`rl:{identifier}:tb`) or
switch to RedisGears/RedisBloom which handle cross-slot atomicity.

**No sliding window log at very high limits.** At 10,000 req/min per key,
SWLog stores 10,000 sorted set members ≈ 640 KB per key. With 1,000 concurrent
users that's 640 MB — before Redis overhead. This is a hard ceiling. SWC is
the right choice above ~1,000 req/min at scale.

**No Lua script versioning.** If you change a Lua script and redeploy without
flushing Redis scripts, old nodes run the old SHA and new nodes run a new SHA.
Both work (each loads its own version), but monitoring which version is running
requires explicit instrumentation. For schema changes to the Redis key format,
add a version prefix to key names (`rl:v2:tb:{identifier}`).

**No multi-region support.** Cross-region Redis replication is asynchronous.
A limit of 100 req/min means 100 req/min per region, not globally. If you need
global rate limiting across regions, you need a synchronous coordination layer
(e.g. a single Redis instance with cross-region replication accepted as slightly
stale, or a distributed consensus system like etcd). This is a hard problem with
no clean solution.