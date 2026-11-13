"""Request identity extraction and key construction helpers.

Forwarding headers are untrusted unless the immediate network peer is
explicitly configured as a trusted proxy. Authentication claims are read
only from the ASGI request's authenticated principal, never decoded from
an unverified bearer token.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from fastapi import Request
from sluice.algorithms.base import RateLimiter

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True, slots=True)
class TrustedProxyConfig:
    """Immutable allowlist of proxy IPs/CIDRs allowed to assert X-Forwarded-For."""

    networks: tuple[IPNetwork, ...]

    def __init__(self, networks: Iterable[str | IPNetwork] = ()) -> None:
        parsed: list[IPNetwork] = []
        for network in networks:
            try:
                parsed.append(
                    ipaddress.ip_network(network, strict=False)
                    if isinstance(network, str) else network
                )
            except ValueError as exc:
                raise ValueError(f"Invalid trusted proxy network: {network!r}") from exc
        object.__setattr__(self, "networks", tuple(parsed))

    def trusts(self, address: IPAddress) -> bool:
        return any(address.version == net.version and address in net for net in self.networks)


def _parse_ip(value: str) -> IPAddress:
    # X-Forwarded-For is a list of IP literals, not host:port values.
    # Rejecting ports and obfuscated forms avoids ambiguous parsing.
    return ipaddress.ip_address(value.strip())


def client_ip(request: Request, *, trusted_proxies: TrustedProxyConfig | None = None) -> str:
    """Return the client address, honoring XFF only from a trusted peer.

    The right-most trusted proxy chain is walked from the connection peer
    toward the client. The first untrusted hop is the effective client.
    Malformed forwarding chains are ignored in favor of the socket peer.
    """
    peer_text = request.client.host if request.client else ""
    try:
        peer = _parse_ip(peer_text)
    except ValueError:
        return peer_text or "unknown"

    config = trusted_proxies or TrustedProxyConfig()
    if not config.trusts(peer):
        return str(peer)

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return str(peer)

    try:
        chain = [_parse_ip(item) for item in forwarded.split(",")]
        if not chain:
            return str(peer)
    except ValueError:
        return str(peer)

    # XFF lists the hops before the socket peer. Starting at the peer,
    # strip trusted proxies from the right; the first untrusted hop wins.
    for hop in reversed(chain):
        if config.trusts(hop):
            continue
        return str(hop)
    return str(chain[0])


def ip_identifier(
    request: Request,
    *,
    trusted_proxies: TrustedProxyConfig | None = None,
) -> str:
    """Client IP identifier; forwarding headers are ignored by default."""
    return client_ip(request, trusted_proxies=trusted_proxies)


def ip_route_identifier(
    request: Request,
    *,
    trusted_proxies: TrustedProxyConfig | None = None,
) -> str:
    """Client IP and stable route identity, falling back to the raw path.

    FastAPI dependencies run after route resolution, so use the route template
    (for example ``/users/{user_id}``) to avoid creating one quota key per path
    parameter value. Pure ASGI middleware may run before routing and has no
    route metadata; in that case the request path is used as a fallback.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    return f"{ip_identifier(request, trusted_proxies=trusted_proxies)}:{path}"


def _authenticated_principal(request: Request):
    # Accessing Starlette's request.user property without AuthenticationMiddleware
    # raises AssertionError, so inspect the ASGI scope before using that property.
    principal = request.scope.get("user")
    if principal is None:
        return None
    if isinstance(principal, Mapping):
        authenticated = principal.get("is_authenticated", True)
    else:
        authenticated = getattr(principal, "is_authenticated", True)
    return principal if authenticated else None


def authenticated_subject_identifier(request: Request) -> str:
    """Extract a subject from upstream-authenticated request state.

    Authentication middleware/dependencies must validate credentials and
    populate ``request.user`` or ``scope['user']`` before this is called.
    """
    principal = _authenticated_principal(request)
    if principal is None:
        raise ValueError("No authenticated principal is available on this request")

    if isinstance(principal, Mapping):
        subject = principal.get("sub") or principal.get("id")
    else:
        subject = getattr(principal, "sub", None) or getattr(principal, "id", None)
    if subject is None or not str(subject).strip():
        raise ValueError("Authenticated principal has no stable subject identifier")
    return f"user:{subject}"


def jwt_subject_identifier(request: Request) -> str:
    """Deprecated safe replacement: use an upstream-authenticated principal.

    This function no longer decodes bearer tokens. The token signature,
    issuer, audience, expiry, and claims must be validated by the app.
    """
    return authenticated_subject_identifier(request)


def hashed_identifier(identifier: str, *, secret: str | bytes, namespace: str = "sluice") -> str:
    """Create a stable, non-reversible HMAC identifier for backend keys."""
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("identifier must be a non-empty string")
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not isinstance(key, bytes) or len(key) < 16:
        raise ValueError("secret must contain at least 16 bytes")
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("namespace must be a non-empty string")
    message = f"{namespace}:{identifier}".encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()
    return f"{namespace}:{digest}"


# ---------------------------------------------------------------------------
# Built-in resolver factories
# ---------------------------------------------------------------------------

LimitResolver = Callable[[Request], tuple[str, RateLimiter]]


def build_resolver(
    limiters: dict[str, RateLimiter],
    identifier_fn: Callable[[Request], str] = ip_route_identifier,
    fallback_key: str = "*",
) -> LimitResolver:
    """Build a route-template-aware resolver over an immutable snapshot."""
    if fallback_key not in limiters:
        raise ValueError(
            f"limiters dict must contain a '{fallback_key}' catch-all entry. "
            f"Got keys: {list(limiters.keys())}"
        )
    routes = dict(limiters)

    def resolver(request: Request) -> tuple[str, RateLimiter]:
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        return identifier_fn(request), routes.get(path, routes[fallback_key])

    return resolver


# ---------------------------------------------------------------------------
# Time / rate string parsing
# ---------------------------------------------------------------------------

def parse_window(s: str) -> int:
    """Parse a simple duration such as ``60s``, ``1m``, ``2h``, or ``500ms``."""
    value = s.strip().lower()
    patterns = [
        (r"^(\d+(?:\.\d+)?)h$", 3_600_000),
        (r"^(\d+(?:\.\d+)?)m$", 60_000),
        (r"^(\d+(?:\.\d+)?)s$", 1_000),
        (r"^(\d+(?:\.\d+)?)ms$", 1),
    ]
    for pattern, multiplier in patterns:
        match = re.match(pattern, value)
        if match:
            milliseconds = float(match.group(1)) * multiplier
            if milliseconds <= 0 or milliseconds > 2**63 - 1:
                raise ValueError("window must be positive and within supported range")
            return int(milliseconds)
    raise ValueError(f"Cannot parse window {value!r}; expected '60s', '1m', '2h', or '500ms'.")


def parse_rate(s: str) -> float:
    """Parse a token refill rate such as ``100/min`` or ``1.5`` per second."""
    value = s.strip().lower()
    match = re.match(r"^(\d+(?:\.\d+)?)/(\w+)$", value)
    if match:
        count = float(match.group(1))
        unit = match.group(2)
        divisors = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "hour": 3600}
        if unit not in divisors:
            raise ValueError(f"Unknown rate unit {unit!r}; use /s, /m, or /h.")
        rate = count / divisors[unit]
    else:
        try:
            rate = float(value)
        except ValueError as exc:
            raise ValueError(f"Cannot parse rate {value!r}.") from exc
    if rate <= 0 or rate == float("inf") or rate != rate:
        raise ValueError("rate must be positive and finite")
    return rate
