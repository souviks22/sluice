"""
sluice - Distributed Rate Limiter Library
=========================================

Three algorithms, one Redis backend, pluggable FastAPI middleware.

"""

from sluice.algorithms import RateLimiter, RateLimitResult
from sluice.backends import RateLimitBackend
from sluice.domain import (
    Clock, SystemClock, RateLimitConfig, TokenBucketConfig, SlidingWindowConfig,
    BackendUnavailableError, RateLimitConfigurationError, InvalidIdentifierError,
    InvalidCostError,
)
from sluice.observability import (
    InstrumentedBackend, MetricsCollector, NoOpObserver,
    RateLimitEvent, RateLimitObserver,
)
from sluice.middleware import (
    RateLimitMiddleware, RateLimitPolicy, TrustedProxyConfig,
    authenticated_subject_identifier, hashed_identifier,
)

__all__ = [
    "RateLimiter",
    "RateLimitResult",
    "RateLimitBackend",
    "RateLimitMiddleware",
    "RateLimitPolicy",
    "InstrumentedBackend",
    "MetricsCollector",
    "NoOpObserver",
    "RateLimitEvent",
    "RateLimitObserver",
    "TrustedProxyConfig",
    "authenticated_subject_identifier",
    "hashed_identifier",
    "Clock",
    "SystemClock",
    "RateLimitConfig",
    "TokenBucketConfig",
    "SlidingWindowConfig",
    "BackendUnavailableError",
    "RateLimitConfigurationError",
    "InvalidIdentifierError",
    "InvalidCostError",
]

__version__ = "0.1.3"
