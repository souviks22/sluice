# FastAPI route dependencies and policy registry

## Primary integration

Use `RateLimitPolicy` to define endpoint-specific rules, then attach
`Depends(policy.dependency())` to an `APIRouter` or individual route. The
dependency executes after FastAPI has resolved the route, so it selects rules
using `request.scope["route"].path`. This correctly matches parameterized
templates such as `/users/{user_id}` instead of looking up concrete paths such
as `/users/42`.

```python
router = APIRouter(dependencies=[Depends(policy.dependency())])
```

The policy dependency is a snapshot: route mappings are copied into a
read-only mapping when `dependency()` is called. Configure the builder before
creating the dependency. A missing catch-all rule is a construction error.

## Key isolation

Each policy rule receives a deterministic route-specific key prefix by
default. A short readable route component plus a SHA-256 suffix avoids common
normalization collisions and keeps prefixes bounded. Explicit `key_prefix`
values are respected; callers who deliberately share a prefix can opt into
shared quota namespaces.

Resolver construction no longer mutates limiter instances. Repeated calls to
`resolver()` are therefore safe and do not progressively alter key prefixes.

## HTTP behavior

Allowed requests receive `RateLimit-Limit`, `RateLimit-Remaining`,
`RateLimit-Reset`, and `X-RateLimit-Algorithm` response headers. Rejected
requests raise HTTP 429 with `Retry-After` and quota headers.

Backend errors fail closed with HTTP 503 by default. `fail_open=True` is an
explicit availability-oriented override and should be chosen only when the
application's threat model permits bypassing enforcement during outages.

## Lifecycle

The application owns backend lifecycle. Construct the top-level backend once,
call `connect()` in FastAPI lifespan startup, and call `close()` once in
lifespan shutdown. Do not create Redis clients per request or per dependency
invocation. `HybridBackend` delegates both calls to its Redis backend, and
`InstrumentedBackend` forwards lifecycle attributes to the wrapped backend;
closing either wrapper therefore closes the underlying Redis client according
to that client's pool ownership settings. Treat a wrapped backend as having
one lifecycle owner. If multiple components share a Redis client or connection
pool, coordinate shutdown at the component that owns that shared resource.

## Optional middleware

`RateLimitMiddleware` remains available for applications that need a global
ASGI enforcement layer or cannot attach route dependencies. It is not the
recommended default for new FastAPI integrations. Middleware HTTP behavior and
trusted identity extraction are hardened in later roadmap steps.
