# Migration guide

This guide covers the staged backend and FastAPI integration changes made during the Sluice production-hardening work. Because downstream applications may have customized adapters, migrate and test one integration at a time.

## Backend protocol: two calls to one operation

The supported backend contract is now operation-oriented:

```python
result = await backend.check(operation)
```

Algorithms build a `RateLimitOperation` containing the algorithm, fully namespaced key, validated configuration, and request cost. A backend must perform the complete admission transition and return a `RateLimitResult`.

The former low-level protocol remains available as `LegacyRateLimitBackend`:

```python
await backend.now_ms()
await backend.evalsha(script_name, keys, args)
```

It is retained for transitional compatibility, but new algorithms use `RateLimitBackend.check()`. Third-party backend authors should implement the operation protocol and ensure that admission is atomic within their storage system. Do not implement `check()` as an application-side read followed by a separate write; concurrent requests can overspend quota.

## Weighted requests

`check(identifier, cost=...)` now means an all-or-nothing operation for all three algorithms. A rejected operation must not partially deduct quota. Review custom adapters and any application code that previously assumed repeated single-unit calls.

## FastAPI integration

For new applications, prefer `RateLimitPolicy.dependency()`:

```python
policy = (
    RateLimitPolicy(backend)
    .limit("/api/login", SlidingWindowLog, limit=5, window="1m")
    .default(TokenBucket, capacity=100, window="1m")
)
router = APIRouter(dependencies=[Depends(policy.dependency())])
```

Configure the complete policy before calling `dependency()`; the dependency uses a snapshot. Route rules are matched against the resolved route template. The older optional `RateLimitMiddleware` remains available for ASGI-wide enforcement and uses a resolver created with `policy.resolver()`.

Backend errors fail closed with HTTP 503 by default in the dependency and middleware integrations. If availability is more important than enforcement for a particular application, explicitly opt into `fail_open=True` on the dependency or `failure_mode="open"` on middleware and document that decision in the threat model.

## Identity changes

Do not trust `X-Forwarded-For` unless the immediate peer is a configured trusted proxy. Use `TrustedProxyConfig` to allowlist proxy addresses/CIDRs. `jwt_subject_identifier` no longer decodes an unverified bearer token; it delegates to authenticated-principal extraction. Ensure authentication middleware has validated signature, issuer, audience, and expiry and populated the request principal before using it.

If identifiers should not appear directly in Redis keys, use `hashed_identifier` with a stable secret stored in a secret manager. Rotating that secret changes key derivation and effectively starts a new quota namespace; plan rotations accordingly.

## Hybrid fallback semantics

`HybridBackend` routes a complete operation once. During a Redis failure, local state is per process, bounded, and ephemeral. It does not preserve a strict cluster-wide quota. A timeout can be ambiguous if Redis committed before the response was lost, so local fallback is not exactly-once failover. Choose hybrid mode only when this degradation behavior is acceptable.

## Suggested migration sequence

1. Upgrade in a staging environment and run the unit suite plus real Redis integration tests.
2. Convert custom backends to `check(operation)` and test concurrent weighted admissions.
3. Move new FastAPI routes to `RateLimitPolicy.dependency()`; keep middleware only where global ASGI enforcement is intentional.
4. Review identity extraction and trusted proxy configuration before deploying.
5. Decide explicitly whether backend failures should fail closed or use local fallback.
6. Compare representative traffic and inspect Redis key cardinality/memory before production rollout.

Do not treat a passing in-process test suite as proof of distributed correctness. Run the Redis integration job against the same Redis major/minor family used in production.
