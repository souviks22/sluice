"""
Redis backend: loads Lua scripts once via SCRIPT LOAD, then calls via EVALSHA.
This gives us atomic multi-key operations without round-trip overhead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.exceptions import NoScriptError

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


class RedisBackend:
    """
    Thin async wrapper around Redis that manages Lua script SHA caching.

    Usage::

        backend = RedisBackend.from_url("redis://localhost:6379/0")
        await backend.connect()
        result = await backend.evalsha("token_bucket", keys=[...], args=[...])
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
        Falls back to storing the raw source for EVAL if SCRIPT LOAD is
        unsupported (e.g. fakeredis in tests).
        """
        for script_path in _SCRIPTS_DIR.glob("*.lua"):
            name = script_path.stem
            src  = script_path.read_text()
            try:
                sha = await self._client.script_load(src)
                self._sha_cache[name] = sha
            except Exception:
                # Fallback: store source under a sentinel prefix so evalsha
                # knows to use EVAL instead.
                self._sha_cache[name] = f"__src__:{name}"
            finally:
                # Store sources separately for the EVAL fallback path
                self._src_cache[name] = src

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
            return await self._client.eval(src, len(keys), *keys, *args)

        try:
            return await self._client.evalsha(sha, len(keys), *keys, *args) 
        except NoScriptError:
            # Redis flushed scripts (e.g. restart) — reload and retry
            src = (_SCRIPTS_DIR / f"{script_name}.lua").read_text()
            try:
                sha = await self._client.script_load(src)
                self._sha_cache[script_name] = sha
                return await self._client.evalsha(sha, len(keys), *keys, *args)
            except Exception:
                return await self._client.eval(src, len(keys), *keys, *args)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    async def now_ms(self) -> int:
        """Current Unix time in milliseconds from Redis instance (no clock-skew)."""
        sec, microsec = await self._client.time()
        ms = sec * 1000 + microsec // 1000
        return ms
    
    async def ping(self) -> bool:
        try:
            return await self._client.ping()
        except Exception:
            return False
        