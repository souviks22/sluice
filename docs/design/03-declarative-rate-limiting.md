# Declarative Policy over Global Middleware + Per-Route Dependencies

## Why?

Rate limiting a service usually means two different things at once: a
global ceiling that applies everywhere, and tighter limits on specific
routes like login or upload. The original way to express "specific route,
specific limit" in **sluice** was `RateLimitDependency`, attached per-route
via `Depends(...)`, alongside a separate global `RateLimitMiddleware`.

That looks reasonable — global default, per-route override — but the two
enforcement points don't compose the way that phrasing implies.

---

## The Problem

1. **The two checks were additive, not overriding.** A request to
   `/api/login` was checked against the middleware's global 100 req/min
   limit *and* the dependency's per-route 5 req/min limit, independently.
   The stricter one didn't replace the looser one — both had to pass. This
   is confusing to reason about and wrong: a route explicitly configured
   with a 5 req/min limit was still implicitly bound by the global 100.

2. **Coverage depends on memory, not structure.** A route with no
   `Depends(rate_limit)` only had the global limit — that's not
   necessarily wrong, but nothing *declares* that intentionally. There's no
   way to distinguish "deliberately using the default" from "forgot to add
   a stricter dependency."

3. **Config is wherever the decorator is.** Knowing what limit actually
   applies to `/api/login` means reading both the middleware setup *and*
   that route's dependency, then reasoning about how they combine.

4. **Identifier logic and key scoping get copy-pasted.** IP extraction,
   JWT-subject extraction, and Redis key isolation between limiters were
   each dependency's problem to get right independently.

---

## The Solution

`RateLimitMiddleware` becomes the single enforcement point — one instance,
sitting in front of every route. It no longer holds a fixed limiter; it
holds a **resolver**, and per-route limits move from request-handling code
into a **declarative policy**.

```text
Request ──▶ resolver(request) ──▶ (identifier, RateLimiter) ──▶ limiter.check()
                  │
                  └── built once, from Policy(...).limit(...).default(...)
```

### One enforcement point, not two

A `LimitResolver` is `(Request) -> (identifier, RateLimiter)`, called once
per request inside `dispatch()`. Collapsing middleware and dependency into
this single call is what removes the additive-check bug: there is exactly
one place a request's limit is decided, not two places whose results get
combined.

### Per-route limits become data, not code structure

`Policy` — `.limit(path, algorithm, ...)`, `.default(...)`,
`.identifier(...)` — builds a `route → RateLimiter` table and compiles it
into a resolver via `build_resolver()`. The route handler knows nothing
about rate limiting; the mapping lives in one object that can be logged
(`describe()`) or loaded from external config (`Policy.from_dict()`)
without touching route code.

### Redis key collisions are handled automatically

Two limiters of the same algorithm share a default `key_prefix` (e.g. every
`TokenBucket` defaults to `rl:tb`). Naively giving `/api/upload` and `*`
each their own `TokenBucket` with the same identifier function would have
both write to the same Redis key — the upload limit and the global limit
would drain each other. `build_resolver()` stamps a route-derived suffix
onto each limiter's `key_prefix` (`rl:tb:api_upload` vs. `rl:tb:global`) at
construction time, so this can't happen even if every route uses the same
identifier function.

### Human-readable config, not raw constructor args

`Policy.limit()` accepts `window="1m"` and `rate="100/min"` instead of raw
milliseconds and tokens/sec, parsed by `parse_window()` / `parse_rate()`.
This is the actual surface a developer edits, and it's what `from_dict()`
accepts too — so the same policy can be built fluently in code or loaded
from YAML/TOML/env without a second parallel API to maintain.

### Coverage is enforced, not assumed

`.resolver()` raises if `.default()` was never called — there's no route
that silently falls through to "whatever the global limiter happens to be"
without that being an explicit, named default rule.

---

## Scope

This change fixes **how many places a limit decision is made** and **how
that decision is declared**, not **how limits are computed** —
`TokenBucket`, `SlidingWindowLog`, and `SlidingWindowCounter` behave
identically either way. It does not:

* Change per-algorithm semantics or Redis atomicity guarantees.