"""Clock abstractions used by local implementations and deterministic tests."""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Source of Unix epoch milliseconds."""

    def now_ms(self) -> int:
        """Return current Unix time in milliseconds."""
        ...


class SystemClock:
    """Production wall clock. Distributed Redis decisions should use Redis TIME."""

    __slots__ = ()

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000
