# Step 8 — FastAPI route dependencies and policy registry

Implemented:
- `RateLimitPolicy.dependency()` for FastAPI `Depends` integration.
- Route-template lookup using Starlette's resolved route metadata.
- Immutable mapping snapshots when creating a resolver or dependency.
- Deterministic per-rule key prefixes without mutating limiter objects.
- Standard quota headers and HTTP 429 rejection with `Retry-After`.
- Explicit backend error behavior: fail closed with HTTP 503 by default, opt-in `fail_open=True`.
- Lazy concrete-backend imports so domain and algorithm APIs can be imported without loading the Redis client.
- `examples/fastapi_dependencies.py` demonstrating lifespan-owned backend lifecycle.

Verification:
- Python compilation and project TOML parsing pass.
- Focused domain and FastAPI dependency tests: 35 passed.
- Full legacy suite and live Redis integration remain unverified in this environment because Redis/FakeRedis dependencies are not installed.
