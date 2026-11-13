"""Shared validation rules for limiter configuration and checks."""

from __future__ import annotations

import math

from .exceptions import InvalidCostError, InvalidIdentifierError, RateLimitConfigurationError

MAX_IDENTIFIER_LENGTH = 1024
MAX_KEY_PREFIX_LENGTH = 128
MAX_LUA_SAFE_INTEGER = (2**53) - 1
MAX_LUA_SAFE_QUOTA = MAX_LUA_SAFE_INTEGER // 2
MAX_LUA_SAFE_WINDOW_MS = (MAX_LUA_SAFE_INTEGER - 1000) // 2


def positive_int(value: int, name: str) -> int:
    """Require a positive integer representable exactly by Lua's number type."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RateLimitConfigurationError(f"{name} must be a positive integer")
    if value > MAX_LUA_SAFE_INTEGER:
        raise RateLimitConfigurationError(
            f"{name} must be at most {MAX_LUA_SAFE_INTEGER} for exact Redis Lua arithmetic"
        )
    return value


def positive_quota(value: int, name: str) -> int:
    """Validate a quota whose intermediate sums must also remain exact in Lua."""
    positive_int(value, name)
    if value > MAX_LUA_SAFE_QUOTA:
        raise RateLimitConfigurationError(
            f"{name} must be at most {MAX_LUA_SAFE_QUOTA} for exact Redis Lua arithmetic"
        )
    return value


def positive_window_ms(value: int, name: str = "window_ms") -> int:
    """Bound window arithmetic, including the script's doubled TTL and scores."""
    positive_int(value, name)
    if value > MAX_LUA_SAFE_WINDOW_MS:
        raise RateLimitConfigurationError(
            f"{name} must be at most {MAX_LUA_SAFE_WINDOW_MS} for exact Redis Lua arithmetic"
        )
    return value


def validate_refill_rate(capacity: int, value: float) -> float:
    """Ensure token-bucket retry/reset durations remain finite and exact."""
    rate = positive_finite_float(value, "refill_rate")
    min_rate = capacity * 1000 / MAX_LUA_SAFE_INTEGER
    if rate < min_rate:
        raise RateLimitConfigurationError(
            "refill_rate is too small for exact millisecond retry/reset values"
        )
    return rate


def positive_finite_float(value: float, name: str) -> float:
    """Require a positive, finite numeric value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RateLimitConfigurationError(f"{name} must be a positive finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise RateLimitConfigurationError(f"{name} must be a positive finite number")
    return normalized


def validate_key_prefix(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RateLimitConfigurationError("key_prefix must be a non-empty string")
    if len(value) > MAX_KEY_PREFIX_LENGTH:
        raise RateLimitConfigurationError(
            f"key_prefix must be at most {MAX_KEY_PREFIX_LENGTH} characters"
        )
    return value


def validate_identifier(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidIdentifierError("identifier must be a non-empty string")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise InvalidIdentifierError(
            f"identifier must be at most {MAX_IDENTIFIER_LENGTH} characters"
        )
    if "\x00" in value:
        raise InvalidIdentifierError("identifier must not contain NUL characters")
    return value


def validate_cost(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidCostError("cost must be a positive integer")
    if value > MAX_LUA_SAFE_INTEGER:
        raise InvalidCostError(
            f"cost must be at most {MAX_LUA_SAFE_INTEGER} for exact Redis Lua arithmetic"
        )
    return value
