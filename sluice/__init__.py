"""
sluice - Distributed Rate Limiter Library
=========================================

Three algorithms, one Redis backend, pluggable FastAPI middleware.

"""

from sluice.algorithms.token_bucket import TokenBucket
from sluice.algorithms.sliding_window_log import SlidingWindowLog
from sluice.algorithms.sliding_window_counter import SlidingWindowCounter
from sluice.algorithms.base import RateLimitResult
from sluice.backends.redis_backend import RedisBackend
from sluice.middleware.fastapi import (
    RateLimitMiddleware,
    RateLimitDependency,
    ip_identifier,
    ip_route_identifier,
    jwt_subject_identifier,
)

__all__ = [
    "TokenBucket",
    "SlidingWindowLog",
    "SlidingWindowCounter",
    "RateLimitResult",
    "RedisBackend",
    "RateLimitMiddleware",
    "RateLimitDependency",
    "ip_identifier",
    "ip_route_identifier",
    "jwt_subject_identifier",
]

__version__ = "0.1.0"
