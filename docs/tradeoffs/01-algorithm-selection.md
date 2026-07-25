This document covers the decisions that actually matter in production, with
concrete numbers where possible. The goal is to make the tradeoffs explicit so
you can choose the right algorithm and configuration for your situation —
not to claim this library is the right choice for everything.

# Algorithm selection

## When exact counting matters

Use **SlidingWindowLog**.

The ±5% approximation error in SlidingWindowCounter sounds small until you put
it in context. At a limit of 1,000 req/min, ±5% is ±50 requests. For a billing
API that charges per request above quota, that's 50 unbilled requests per user
per minute — or 50 unjustified rejections. For a login endpoint, it's up to 50
extra brute-force attempts per minute getting through.

The cost is memory: at 1,000 req/min per key, SWLog stores ~1,000 sorted set
members × ~64 bytes ≈ 64 KB per key. With 10,000 active users that's 640 MB
in Redis. At 100 req/min per key it's 6.4 MB for 10,000 users — usually fine.

The threshold: if `limit × ~64 bytes × active_keys` fits comfortably in your
Redis memory budget, use SWLog and get exactness for free.

## When memory matters more than exactness

Use **SlidingWindowCounter**.

Two integer keys per user regardless of request volume. 10,000 active users at
any limit is ~640 KB total. At 1M active users it's ~64 MB. The approximation
error is bounded at `limit × max(prev_weight) = limit × 1.0`, meaning in the
absolute worst case (all previous-window requests concentrated at the boundary)
the counter can admit up to `2× limit` in a short burst. In practice this is
rare and the error averages well under 5%.

The worst case is worth being concrete about: if a client sends `limit` requests
in the last millisecond of window W, then `limit` requests in the first
millisecond of window W+1, a fixed-window counter would allow 2× limit (the
classic reset-spike attack). SWC would allow approximately:
  `limit × (1 - ε) + limit ≈ limit × 1.999`
where ε is the small elapsed fraction. So SWC prevents the full 2× spike but
doesn't eliminate it entirely. SWLog prevents it completely.

## When smooth throughput matters more than per-window precision

Use **TokenBucket**.

The bucket metaphor: tokens accumulate up to `capacity`, consumed per request.
The useful property is that idle periods bank tokens for later use. A client
that makes zero requests for 30 seconds against a 60-req/min limiter will have
accumulated 30 tokens — it can then make 30 requests immediately without
any rejection. This is desirable for batch clients and mobile apps with
irregular access patterns.

The tradeoff: because bursts are explicitly permitted, you can't make a
strict "no more than N requests per window" guarantee. A client that
idles for 60 seconds then bursts can fire `capacity` requests in 10ms.
Window-based algorithms prevent this; token bucket embraces it.