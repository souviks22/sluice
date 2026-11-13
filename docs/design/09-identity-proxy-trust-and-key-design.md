# Identity, proxy trust, and key design

## Client IP and forwarding headers

Sluice treats the socket peer (`request.client.host`) as authoritative by
default. `X-Forwarded-For` is ignored unless the immediate peer belongs to
an explicitly configured `TrustedProxyConfig` allowlist.

Configure trusted proxy CIDRs from deployment-controlled settings, for
example `TrustedProxyConfig(["10.20.0.0/16", "2001:db8:abcd::/48"])`.
Do not derive this allowlist from request headers or user input.

When the immediate peer is trusted, Sluice parses the XFF chain and walks
it from right to left, skipping trusted hops. The first untrusted hop is
the effective client. Malformed chains fall back to the socket peer.
`X-Real-IP` is not implicitly trusted.

The built-in `ip_identifier` and middleware default do not trust forwarded
headers. To enable proxy-aware extraction, pass a configured callable:
`identifier_fn=lambda request: ip_identifier(request, trusted_proxies=trusted)`.

## Authenticated identities

`authenticated_subject_identifier` reads a subject from an upstream
authenticated principal on `request.user` or `scope["user"]`. The app's
authentication layer remains responsible for signature, issuer, audience,
expiry, and authorization validation.

`jwt_subject_identifier` remains as a compatibility name but no longer
decodes bearer tokens. Unverified JWT payloads are attacker-controlled and
must never determine a quota identity.

## Key privacy and stability

`hashed_identifier` creates a deterministic HMAC-SHA256 pseudonym suitable
for backend key material. Use a dedicated secret of at least 16 bytes and
a stable namespace. Keep the secret consistent across workers and rotate
it deliberately: changing it creates a new identity namespace and resets
effective quotas. HMAC reduces exposure of raw user IDs/API keys in Redis
key names; it does not replace access controls or encryption at rest.

Prefer stable account/API-client identifiers over display names, email
addresses, or raw bearer/API tokens. Avoid identifiers containing secrets
or unnecessary personal data.

When a resolved FastAPI route is available, `ip_route_identifier` uses its
route template rather than a concrete path. This keeps requests such as
`/users/1` and `/users/2` in the same route quota and avoids high-cardinality
keys from attacker-controlled path parameters. A pure ASGI middleware may run
before routing and therefore falls back to the raw path; use route dependencies
for route-specific policies.
