import re
import base64, json
from typing import Callable

from fastapi import Request
from sluice.algorithms.base import RateLimiter


# ---------------------------------------------------------------------------
# Identifier extractors  (unchanged — still useful inside resolvers)
# ---------------------------------------------------------------------------

def ip_identifier(request: Request) -> str:
    """Client IP, respecting X-Forwarded-For."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def ip_route_identifier(request: Request) -> str:
    """(IP, path) — scopes the counter per endpoint."""
    return f"{ip_identifier(request)}:{request.url.path}"


def jwt_subject_identifier(request: Request) -> str:
    """JWT 'sub' claim, falling back to IP. Verification must happen elsewhere."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            payload_b64 = auth[7:].split(".")[1]
            padding = 4 - len(payload_b64) % 4
            payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
            return payload.get("sub", ip_identifier(request))
        except Exception:
            pass
    return ip_identifier(request)


# ---------------------------------------------------------------------------
# Built-in resolver factories
# ---------------------------------------------------------------------------

# A resolver maps a request to (identifier, limiter).
# The identifier scopes the counter; the limiter carries the algorithm + config.
LimitResolver = Callable[[Request], tuple[str, RateLimiter]]

def build_resolver(
    limiters: dict[str, RateLimiter],
    identifier_fn: Callable[[Request], str] = ip_route_identifier,
    fallback_key: str = "*",
) -> LimitResolver:
    """
    Build a resolver from a route → limiter mapping.

    Key isolation
    -------------
    Two limiters of the same algorithm type share the same default key_prefix
    (e.g. both TokenBuckets default to "rl:tb"). If they both use ip_identifier,
    they would produce identical Redis keys and share a counter — the upload
    limit would drain the global bucket and vice versa.

    build_resolver() stamps a unique key_prefix on every limiter based on its
    route, so keys are always scoped per-route regardless of identifier function:

        "/api/upload" TokenBucket → key_prefix "rl:tb:api_upload"
        "*"           TokenBucket → key_prefix "rl:tb:global"

    Redis keys become:
        "rl:tb:api_upload:127.0.0.1"   ← upload limiter
        "rl:tb:global:127.0.0.1"       ← global limiter

    No overlap, no shared state, even with ip_identifier.

    This stamping mutates the limiter objects in-place. Pass a fresh limiter
    instance per route — don't reuse the same object across two routes.

    The "*" key is the catch-all for routes not explicitly listed.
    ValueError is raised at construction time if "*" is missing — fail fast.
    """
    if fallback_key not in limiters:
        raise ValueError(
            f"limiters dict must contain a '{fallback_key}' catch-all entry. "
            f"Got keys: {list(limiters.keys())}"
        )

    for route, limiter in limiters.items():
        if route == fallback_key:
            suffix = "global"
        else:
            suffix = route.strip("/").replace("/", "_") or "root"

        # Compose with the existing prefix so the algorithm name is preserved:
        #   TokenBucket default "rl:tb"  → "rl:tb:api_upload"
        #   SlidingWindowLog "rl:swl"    → "rl:swl:api_login"
        
        existing = limiter.key_prefix
        new_prefix = f"{existing}:{suffix}"
        limiter.key_prefix = new_prefix

    def resolver(request: Request) -> tuple[str, RateLimiter]:
        path    = request.url.path
        limiter = limiters.get(path, limiters[fallback_key])
        ident   = identifier_fn(request)
        return ident, limiter

    return resolver


# ---------------------------------------------------------------------------
# Time / rate string parsing
# ---------------------------------------------------------------------------

def parse_window(s: str) -> int:
    """
    Parse a human-readable window string to milliseconds.

        "1m"    → 60_000
        "30s"   → 30_000
        "1h"    → 3_600_000
        "500ms" → 500
        "2h30m" → not supported — keep it simple

    Raises ValueError on unrecognised format.
    """
    s = s.strip().lower()
    patterns = [
        (r"^(\d+(?:\.\d+)?)h$",  3_600_000),
        (r"^(\d+(?:\.\d+)?)m$",  60_000),
        (r"^(\d+(?:\.\d+)?)s$",  1_000),
        (r"^(\d+(?:\.\d+)?)ms$", 1),
    ]
    for pattern, multiplier in patterns:
        m = re.match(pattern, s)
        if m:
            return int(float(m.group(1)) * multiplier)
    raise ValueError(
        f"Cannot parse window '{s}'. "
        f"Expected format: '60s', '1m', '2h', '500ms'."
    )


def parse_rate(s: str) -> float:
    """
    Parse a rate string to tokens per second.

        "100/min" → 100/60  ≈ 1.667
        "10/s"    → 10.0
        "1/h"     → 1/3600
        "50/m"    → 50/60

    Also accepts a plain float/int string: "1.5" → 1.5 tokens/sec.
    """
    s = s.strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)/(\w+)$", s)
    if m:
        count = float(m.group(1))
        unit  = m.group(2)
        divisors = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "hour": 3600}
        if unit not in divisors:
            raise ValueError(f"Unknown rate unit '{unit}'. Use /s, /m, /h.")
        return count / divisors[unit]
    # Plain number
    try:
        return float(s)
    except ValueError:
        raise ValueError(
            f"Cannot parse rate '{s}'. "
            f"Expected format: '100/min', '10/s', '1/h', or a plain float."
        )
