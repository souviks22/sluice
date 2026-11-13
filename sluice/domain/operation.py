"""Immutable command describing one rate-limit admission decision."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from .config import SlidingWindowConfig, TokenBucketConfig
from .exceptions import InvalidCostError, RateLimitConfigurationError
from .validation import validate_cost, validate_identifier

TOKEN_BUCKET = "token_bucket"
SLIDING_WINDOW_LOG = "sliding_window_log"
SLIDING_WINDOW_COUNTER = "sliding_window_counter"

OperationConfig: TypeAlias = TokenBucketConfig | SlidingWindowConfig


@dataclass(frozen=True, slots=True)
class RateLimitOperation:
    """A complete request to admit ``cost`` units against one key.

    ``key`` is the fully namespaced backend key. Config objects are immutable;
    the backend must treat this operation as one indivisible admission decision.
    """

    algorithm: str
    key: str
    config: OperationConfig
    cost: int = 1

    def __post_init__(self) -> None:
        if self.algorithm not in {TOKEN_BUCKET, SLIDING_WINDOW_LOG, SLIDING_WINDOW_COUNTER}:
            raise RateLimitConfigurationError(f"Unsupported algorithm: {self.algorithm!r}")
        validate_identifier(self.key)
        validate_cost(self.cost)
        if self.algorithm == TOKEN_BUCKET and not isinstance(self.config, TokenBucketConfig):
            raise RateLimitConfigurationError("token_bucket requires TokenBucketConfig")
        if self.algorithm in {SLIDING_WINDOW_LOG, SLIDING_WINDOW_COUNTER} and not isinstance(self.config, SlidingWindowConfig):
            raise RateLimitConfigurationError(f"{self.algorithm} requires SlidingWindowConfig")
        if self.algorithm == TOKEN_BUCKET:
            assert isinstance(self.config, TokenBucketConfig)
            maximum = self.config.capacity
        else:
            assert isinstance(self.config, SlidingWindowConfig)
            maximum = self.config.limit
        if self.cost > maximum:
            raise InvalidCostError(
                f"cost ({self.cost}) cannot exceed this limiter's maximum admission ({maximum})"
            )
