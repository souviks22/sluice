"""Domain exceptions independent of HTTP, Redis, and framework adapters."""


class SluiceError(Exception):
    """Base class for errors raised by Sluice."""


class BackendUnavailableError(SluiceError):
    """A rate-limit backend could not evaluate an admission."""


class RateLimitConfigurationError(SluiceError, ValueError):
    """A limiter configuration is invalid."""


class InvalidIdentifierError(SluiceError, ValueError):
    """A rate-limit identifier is invalid."""


class InvalidCostError(SluiceError, ValueError):
    """A requested quota cost is invalid."""
