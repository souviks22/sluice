"""Immutable, validated configuration value objects."""

from __future__ import annotations

from dataclasses import dataclass

from .validation import (
    positive_quota,
    positive_window_ms,
    validate_key_prefix,
    validate_refill_rate,
)


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    """Common configuration shared by rate-limiting algorithms."""

    key_prefix: str = "rl"

    def __post_init__(self) -> None:
        validate_key_prefix(self.key_prefix)


@dataclass(frozen=True, slots=True)
class TokenBucketConfig(RateLimitConfig):
    """Token bucket capacity and refill rate (tokens per second)."""

    capacity: int = 1
    refill_rate: float = 1.0

    def __post_init__(self) -> None:
        super(TokenBucketConfig, self).__post_init__()
        positive_quota(self.capacity, "capacity")
        object.__setattr__(
            self, "refill_rate", validate_refill_rate(self.capacity, self.refill_rate)
        )


@dataclass(frozen=True, slots=True)
class SlidingWindowConfig(RateLimitConfig):
    """Sliding-window quota and duration."""

    limit: int = 1
    window_ms: int = 1

    def __post_init__(self) -> None:
        super(SlidingWindowConfig, self).__post_init__()
        positive_quota(self.limit, "limit")
        positive_window_ms(self.window_ms)
