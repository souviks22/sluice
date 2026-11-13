"""Backend contracts.

The operation-oriented protocol is the supported interface. The legacy
script protocol remains available during the staged backend migration.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sluice.domain.operation import RateLimitOperation
from sluice.domain.result import RateLimitResult


@runtime_checkable
class RateLimitBackend(Protocol):
    """Execute one complete rate-limit admission operation."""

    async def check(self, operation: RateLimitOperation) -> RateLimitResult: ...


class LegacyRateLimitBackend(Protocol):
    """Deprecated low-level contract retained for third-party compatibility."""

    async def now_ms(self) -> int: ...

    async def evalsha(self, script_name: str, keys: list[str], args: list[Any]) -> list[Any]: ...


class BackendLifecycle(Protocol):
    """Optional resource lifecycle, deliberately separate from admission."""

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
