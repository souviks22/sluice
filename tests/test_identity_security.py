from __future__ import annotations

import pytest
from types import SimpleNamespace
from starlette.requests import Request

from sluice.middleware.utils import (
    TrustedProxyConfig,
    authenticated_subject_identifier,
    client_ip,
    hashed_identifier,
    ip_identifier,
    ip_route_identifier,
    jwt_subject_identifier,
)


def request_for(
    headers=None,
    client=("203.0.113.10", 12345),
    scope_user=None,
    path="/",
    route_path=None,
):
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ], "client": client, "server": ("testserver", 80),
    }
    if scope_user is not None:
        scope["user"] = scope_user
    if route_path is not None:
        scope["route"] = SimpleNamespace(path=route_path)
    return Request(scope)


def test_forwarded_header_ignored_without_trusted_proxy():
    request = request_for({"X-Forwarded-For": "198.51.100.99"})
    assert ip_identifier(request) == "203.0.113.10"


def test_route_identifier_uses_template_instead_of_parameter_value():
    request = request_for(
        path="/users/account-42",
        route_path="/users/{user_id}",
    )
    assert ip_route_identifier(request) == "203.0.113.10:/users/{user_id}"


def test_forwarded_header_ignored_when_peer_not_trusted():
    request = request_for({"X-Forwarded-For": "198.51.100.99"})
    trusted = TrustedProxyConfig(["10.0.0.0/8"])
    assert client_ip(request, trusted_proxies=trusted) == "203.0.113.10"


def test_single_trusted_proxy_can_assert_client_ip():
    request = request_for(
        {"X-Forwarded-For": "198.51.100.99"},
        client=("10.1.2.3", 443),
    )
    trusted = TrustedProxyConfig(["10.0.0.0/8"])
    assert client_ip(request, trusted_proxies=trusted) == "198.51.100.99"


def test_walks_proxy_chain_from_right_and_ignores_spoofed_left_prefix():
    request = request_for(
        {"X-Forwarded-For": "192.0.2.66, 198.51.100.99, 10.1.2.4"},
        client=("10.1.2.3", 443),
    )
    trusted = TrustedProxyConfig(["10.0.0.0/8"])
    assert client_ip(request, trusted_proxies=trusted) == "198.51.100.99"


def test_malformed_forwarded_chain_falls_back_to_socket_peer():
    request = request_for(
        {"X-Forwarded-For": "not-an-ip, 198.51.100.99"},
        client=("10.1.2.3", 443),
    )
    trusted = TrustedProxyConfig(["10.0.0.0/8"])
    assert client_ip(request, trusted_proxies=trusted) == "10.1.2.3"


def test_unverified_bearer_token_is_not_decoded_for_identity():
    request = request_for({"Authorization": "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiJhdHRhY2tlciJ9."})
    with pytest.raises(ValueError):
        jwt_subject_identifier(request)


def test_authenticated_principal_is_used():
    request = request_for(scope_user={"sub": "account-123", "is_authenticated": True})
    assert authenticated_subject_identifier(request) == "user:account-123"


def test_unauthenticated_principal_is_rejected():
    request = request_for(scope_user={"sub": "account-123", "is_authenticated": False})
    with pytest.raises(ValueError):
        authenticated_subject_identifier(request)


def test_hmac_identifier_is_stable_and_namespaced():
    a = hashed_identifier("user:42", secret="0123456789abcdef", namespace="api")
    b = hashed_identifier("user:42", secret="0123456789abcdef", namespace="api")
    c = hashed_identifier("user:42", secret="fedcba9876543210", namespace="api")
    assert a == b
    assert a != c
    assert "42" not in a


def test_hmac_requires_sufficient_secret_entropy_length():
    with pytest.raises(ValueError):
        hashed_identifier("user:42", secret="short")
