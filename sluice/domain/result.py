"""Stable, immutable result returned by every limiter."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of one rate-limit decision.

    `remaining` is the algorithm's estimate after this decision.
    `reset_after_ms` is the algorithm-specific time until its reported reset.
    `retry_after_ms` is zero for allowed requests and a non-negative delay for
    rejected requests. These values are durations, never wall-clock timestamps.
    """

    allowed: bool
    remaining: int
    reset_after_ms: int
    retry_after_ms: int
    algorithm: str
    key: str

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise TypeError("allowed must be a bool")
        for name in ("remaining", "reset_after_ms", "retry_after_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.algorithm, str) or not self.algorithm:
            raise ValueError("algorithm must be a non-empty string")
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("key must be a non-empty string")
        if self.allowed and self.retry_after_ms != 0:
            raise ValueError("retry_after_ms must be zero for allowed requests")
