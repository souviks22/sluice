"""Temporary bridge from typed operations to the legacy script API.

This module centralizes the staged migration. Step 6 replaces this bridge with
single-script, server-timed, weighted atomic transitions.
"""
from __future__ import annotations

from typing import Any, Protocol

from sluice.domain import RateLimitOperation, RateLimitResult


class _LegacyExecutor(Protocol):
    async def now_ms(self) -> int: ...
    async def evalsha(self, script_name: str, keys: list[str], args: list[Any]) -> list[Any]: ...


async def execute_legacy_operation(
    backend: _LegacyExecutor, operation: RateLimitOperation
) -> RateLimitResult:
    """Execute through the old clock+script sequence (not yet atomic as a whole)."""
    config = operation.config
    now = await backend.now_ms()
    if operation.algorithm == "token_bucket":
        assert hasattr(config, "capacity") and hasattr(config, "refill_rate")
        raw = await backend.evalsha(
            operation.algorithm, [operation.key],
            [config.capacity, config.refill_rate, operation.cost, now],
        )
        return _result(raw, operation)

    assert hasattr(config, "limit") and hasattr(config, "window_ms")
    last: RateLimitResult | None = None
    for index in range(operation.cost):
        timestamp = now + index if operation.algorithm == "sliding_window_log" else now
        raw = await backend.evalsha(
            operation.algorithm, [operation.key], [config.limit, config.window_ms, timestamp]
        )
        last = _result(raw, operation)
        if not last.allowed:
            break
    assert last is not None
    return last


def _result(raw: list[Any], operation: RateLimitOperation) -> RateLimitResult:
    return RateLimitResult(
        allowed=bool(int(raw[0])),
        remaining=int(raw[1]),
        reset_after_ms=int(raw[2]),
        retry_after_ms=int(raw[3]),
        algorithm=operation.algorithm,
        key=operation.key,
    )
