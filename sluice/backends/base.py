"""
Base types shared by all backends.
"""

from __future__ import annotations

from typing import Any, Protocol


class RateLimitBackend(Protocol):
    """
    Protocol every backend must satisfy.
    Implementations are fully async; call `await backend.connect()` before use.
    """

    async def now_ms(self) -> int:
        """
        Return the current time in milliseconds.
        """
        ...

    async def evalsha(
        self,
        script_name: str,
        keys: list[str],
        args: list[Any]
    ) -> list[Any]:
        """
        Run rate-limiting algorithm *script_name*.

        Parameters
        ----------
        script_name
            Name of the algorithm to execute.
        keys
            List of keys to pass to the algorithm.
        args
            List of arguments to pass to the algorithm.
        """
        ...