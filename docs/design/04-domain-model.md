# Domain model and validation

Sluice's domain package is independent of FastAPI, Redis, Lua, and Starlette.

## Value objects

- `RateLimitConfig`: immutable common namespace configuration.
- `TokenBucketConfig`: validated positive capacity and finite positive refill rate.
- `SlidingWindowConfig`: validated positive limit and window duration.
- `RateLimitResult`: immutable decision result with duration fields in milliseconds.
- `Clock`: protocol for Unix epoch milliseconds; `SystemClock` is the local implementation.

The algorithm constructors retain their existing keyword-based API for compatibility, but now validate their parameters at construction. The config value objects are the preferred foundation for the next public API iteration.

## Validation rules

- `capacity`, `limit`, and `window_ms` must be positive integers; booleans are not accepted as integers.
- `refill_rate` must be a positive finite number.
- `cost` must be a positive integer.
- Quotas and time windows are bounded so Lua intermediate arithmetic stays
  within the exactly representable integer range of Redis Lua numbers.
- Token-bucket refill rates cannot be so small that retry/reset durations
  overflow exact millisecond values.
- `identifier` must be a non-empty string, at most 1024 characters, without NUL.
- `key_prefix` must be a non-empty string of at most 128 characters.
- Result counters and durations must be non-negative integers.
- An allowed result cannot carry a retry delay.

## Time semantics

`Clock.now_ms()` returns Unix epoch milliseconds. It exists for local implementations and deterministic testing. Redis-backed distributed decisions must not rely on this client clock; the Redis execution adapter will acquire authoritative server time inside the atomic operation.

## Weighted admission

The domain contract now states that `cost` is a positive integer and admission is all-or-nothing. This step validates the input and makes the contract explicit. Atomic weighted execution is implemented in the backend and algorithm steps that follow; current legacy window algorithms still have their old sequential script behavior until that migration is complete.
