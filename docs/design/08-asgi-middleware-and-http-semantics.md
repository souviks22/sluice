# Optional ASGI middleware and HTTP semantics

FastAPI route dependencies remain the preferred integration. The optional
`RateLimitMiddleware` is a pure ASGI component intended for global policies
and applications that need enforcement outside individual route declarations.

## Request handling

- Non-HTTP ASGI scopes pass through unchanged.
- Exclusions match an exact path or a complete descendant path segment.
- The middleware makes one limiter decision before invoking the application.
- A rejected request returns 429; soft mode forwards the request and marks
  the response with `X-RateLimit-Exceeded: true`.
- Backend errors fail closed with 503 by default. `failure_mode="open"` is
  an explicit availability-first override. The legacy `fail_open` argument
  remains supported as a compatibility shim.

## Response fields

The middleware emits `RateLimit-Limit`, `RateLimit-Remaining`, and
`RateLimit-Reset` on allowed and rejected responses. Reset and Retry-After
are delta-seconds rounded up, with a minimum of one second. Rejected
responses include `Retry-After`. `X-RateLimit-Algorithm` is retained as a
legacy diagnostic header and can be disabled with `legacy_headers=False`.

Existing application headers with the same names are replaced to avoid
contradictory duplicate values. Other response headers are preserved.

## Exclusions

An exclusion `/health` matches `/health` and `/health/live`, but not
`/healthcheck`. This avoids accidental prefix collisions.

## Operational note

ASGI middleware is global and cannot automatically use FastAPI's resolved
route template before routing has run. Prefer route dependencies when
route-template-specific policies are required.
