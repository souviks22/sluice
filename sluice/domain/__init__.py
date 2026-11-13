"""Framework- and backend-independent Sluice domain model."""

from .clock import Clock, SystemClock
from .config import RateLimitConfig, SlidingWindowConfig, TokenBucketConfig
from .exceptions import (
    BackendUnavailableError,
    InvalidCostError,
    InvalidIdentifierError,
    RateLimitConfigurationError,
    SluiceError,
)
from .result import RateLimitResult
from .operation import RateLimitOperation
from .validation import validate_cost, validate_identifier, validate_key_prefix

__all__ = [
    "Clock",
    "SystemClock",
    "RateLimitConfig",
    "TokenBucketConfig",
    "SlidingWindowConfig",
    "RateLimitResult",
    "RateLimitOperation",
    "SluiceError",
    "BackendUnavailableError",
    "RateLimitConfigurationError",
    "InvalidIdentifierError",
    "InvalidCostError",
    "validate_cost",
    "validate_identifier",
    "validate_key_prefix",
]
