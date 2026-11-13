"""
Redis backend: loads Lua scripts once via SCRIPT LOAD, then executes one
server-timed, atomic admission transition through EVALSHA.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.exceptions import NoScriptError, ResponseError

from sluice.domain import (
    RateLimitOperation,
    RateLimitResult,
    SlidingWindowConfig,
    TokenBucketConfig,
)

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _script_loading_unsupported(error: ResponseError) -> bool:
    """Return true only for server responses that explicitly reject SCRIPT.

    Redis ACL denials and other operational errors must remain visible to the
    caller; raw EVAL is only a compatibility path for servers/clients that do
    not implement SCRIPT LOAD.
    """
    message = str(error).lower()
    return "unknown command" in message or "unsupported command" in message


class RedisBackend:
    """
    Thin async wrapper around Redis that manages Lua script SHA caching.

    Usage::

        backend = RedisBackend.from_url("redis://localhost:6379/0")
        await backend.connect()
        result = await backend.check(operation)
    """

    def __init__(self, client: Redis) -> None:
        self._client = client
        self._sha_cache: dict[str, str] = {}
        self._src_cache: dict[str, str] = {}

    @classmethod
    def from_url(self, url: str, **kwargs: Any) -> "RedisBackend":
        client = aioredis.from_url(url, decode_responses=True, **kwargs)
        return self(client)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """
        Pre-load all Lua scripts and cache their SHAs via SCRIPT LOAD.
        Falls back to storing the raw source for EVAL only if the server
        explicitly reports SCRIPT as an unknown or unsupported command.

        Cache state is published only after every script has loaded. If
        initialization fails, a partially initialized cache is not exposed.
        """
        sha_cache: dict[str, str] = {}
        src_cache: dict[str, str] = {}
        script_paths = sorted(_SCRIPTS_DIR.glob("*.lua"))
        if not script_paths:
            raise RuntimeError(f"No Redis Lua scripts found in {_SCRIPTS_DIR}")
        for script_path in script_paths:
            name = script_path.stem
            src = script_path.read_text()
            try:
                sha = await self._client.script_load(src)
                sha_cache[name] = sha
            except ResponseError as exc:
                if not _script_loading_unsupported(exc):
                    raise
                # Fallback: store source under a sentinel prefix so evalsha
                # knows to use EVAL instead.
                sha_cache[name] = f"__src__:{name}"
            src_cache[name] = src
        self._sha_cache = sha_cache
        self._src_cache = src_cache

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    async def evalsha(self, script_name: str, keys: list[str], args: list[Any]) -> list[Any]:
        """
        Execute a pre-loaded script by name.
        * In production: uses EVALSHA (SHA cached at connect-time).
        * In tests / fakeredis: falls back to EVAL with raw Lua source.
        * Auto-recovers from NoScriptError (Redis restart flushed scripts).
        """
        sha = self._sha_cache.get(script_name)
        if sha is None:
            raise KeyError(f"Script '{script_name}' not loaded. Call connect() first.")

        # Fallback path: SCRIPT LOAD was not supported (e.g. fakeredis)
        if sha.startswith("__src__:"):
            src = self._src_cache.get(script_name)
            if src is None:
                src = (_SCRIPTS_DIR / f"{script_name}.lua").read_text()
            return await cast(Any, self._client.eval(src, len(keys), *keys, *args))

        try:
            return await cast(Any, self._client.evalsha(sha, len(keys), *keys, *args))
        except NoScriptError:
            # Redis flushed scripts (e.g. restart) — reload and retry
            src = (_SCRIPTS_DIR / f"{script_name}.lua").read_text()
            try:
                sha = await cast(Any, self._client.script_load(src))
            except ResponseError as exc:
                if not _script_loading_unsupported(exc):
                    raise
                return await cast(Any, self._client.eval(src, len(keys), *keys, *args))
            self._sha_cache[script_name] = sha
            return await cast(Any, self._client.evalsha(sha, len(keys), *keys, *args))

    async def check(self, operation: RateLimitOperation) -> RateLimitResult:
        """Run one server-timed, atomic admission transition in Redis."""
        config = operation.config
        if operation.algorithm == "token_bucket":
            assert isinstance(config, TokenBucketConfig)
            args = [config.capacity, config.refill_rate, operation.cost]
        else:
            assert isinstance(config, SlidingWindowConfig)
            args = [config.limit, config.window_ms, operation.cost]
        raw = cast(Any, await self.evalsha(operation.algorithm, [operation.key], args))
        if len(raw) != 4:
            raise RuntimeError(
                f"Redis script {operation.algorithm!r} returned {len(raw)} values; expected 4"
            )
        return RateLimitResult(
            allowed=bool(int(raw[0])),
            remaining=int(raw[1]),
            reset_after_ms=int(raw[2]),
            retry_after_ms=int(raw[3]),
            algorithm=operation.algorithm,
            key=operation.key,
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    async def now_ms(self) -> int:
        """Compatibility helper; new admission scripts obtain Redis TIME internally."""
        sec, microsec = await self._client.time()
        ms = sec * 1000 + microsec // 1000
        return ms
    
    async def ping(self) -> bool:
        try:
            return await cast(Any, self._client.ping())
        except Exception:
            return False
        
