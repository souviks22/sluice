"""Backend interfaces and implementations.

Concrete backends are imported lazily so importing algorithm/domain APIs does
not require the optional Redis client until RedisBackend is actually used.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

from sluice.backends.base import BackendLifecycle, LegacyRateLimitBackend, RateLimitBackend

_EXPORTS = {
    "RedisBackend": (".redis_backend", "RedisBackend"),
    "FallbackBackend": (".fallback_backend", "FallbackBackend"),
    "HybridBackend": (".hybrid_backend", "HybridBackend"),
    "CircuitBreaker": (".hybrid_backend", "CircuitBreaker"),
    "CircuitState": (".hybrid_backend", "CircuitState"),
}

__all__ = [
    "RateLimitBackend", "LegacyRateLimitBackend", "BackendLifecycle",
    "RedisBackend", "FallbackBackend", "HybridBackend", "CircuitBreaker", "CircuitState",
]


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
