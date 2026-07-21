"""
sluice - Distributed Rate Limiter Library
=========================================

Three algorithms, one Redis backend, pluggable FastAPI middleware.

"""

from sluice.algorithms import RateLimiter, RateLimitResult
from sluice.backends import RateLimitBackend
from sluice.middleware import RateLimitMiddleware, RateLimitPolicy

__all__ = [
    "RateLimiter",
    "RateLimitResult",
    "RateLimitBackend",
    "RateLimitMiddleware",
    "RateLimitPolicy",
]

__version__ = "0.1.1"
