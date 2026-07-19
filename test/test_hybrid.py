"""
Test suite for HybridBackend (async wrapper around RedisBackend / FallbackBackend).
Mocks out the Redis and Fallback backends so no live Redis is needed.
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, patch
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sluice.backends.hybrid_backend import CircuitState, HybridBackend


@pytest.fixture
def backend():
    with patch("sluice.backends.hybrid_backend.RedisBackend") as MockRedisCls, \
         patch("sluice.backends.hybrid_backend.FallbackBackend") as MockFallbackCls:

        mock_redis = AsyncMock()
        MockRedisCls.from_url.return_value = mock_redis

        mock_local = AsyncMock()
        MockFallbackCls.return_value = mock_local

        hb = HybridBackend(
            redis_url="redis://fake:6379/0",
            failure_threshold=3,
            recovery_timeout_sec=0.05,
            recovery_jitter_frac=0.0,
            redis_call_timeout_sec=0.05,
            local_maxsize=100,
            local_ttl_sec=60.0,
        )
        hb._test_redis_mock = mock_redis
        hb._test_local_mock = mock_local
        yield hb


async def make_redis_fail(hb, times: int):
    hb._test_redis_mock.now_ms.side_effect = ConnectionError("redis down")
    for _ in range(times):
        await hb.now_ms()


class TestHybridBackendHappyPath:

    @pytest.mark.asyncio
    async def test_successful_call_uses_redis(self, backend):
        backend._test_redis_mock.now_ms.return_value = 12345
        result = await backend.now_ms()
        assert result == 12345
        backend._test_local_mock.now_ms.assert_not_awaited()
        assert backend.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_evalsha_routes_to_redis_when_closed(self, backend):
        backend._test_redis_mock.evalsha.return_value = [1, 0]
        result = await backend.evalsha("token_bucket", ["k1"], [10, 1])
        assert result == [1, 0]
        backend._test_local_mock.evalsha.assert_not_awaited()


class TestHybridBackendFailover:

    @pytest.mark.asyncio
    async def test_single_failure_falls_back_but_stays_closed(self, backend):
        backend._test_redis_mock.now_ms.side_effect = ConnectionError("down")
        backend._test_local_mock.now_ms.return_value = 111

        result = await backend.now_ms()

        assert result == 111
        assert backend.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_threshold_failures_open_circuit(self, backend):
        await make_redis_fail(backend, backend.failure_threshold)
        assert backend.state == CircuitState.OPEN
        assert backend.is_degraded is True

    @pytest.mark.asyncio
    async def test_open_circuit_routes_immediately_to_local(self, backend):
        await make_redis_fail(backend, backend.failure_threshold)
        backend._test_redis_mock.now_ms.reset_mock()
        backend._test_local_mock.now_ms.return_value = 777

        result = await backend.now_ms()

        assert result == 777
        backend._test_redis_mock.now_ms.assert_not_awaited()


class TestHybridBackendTimeout:

    @pytest.mark.asyncio
    async def test_hanging_redis_call_is_treated_as_failure(self, backend):
        """
        A Redis call that never raises but never returns either (pool
        exhaustion, stuck connection) must still trip the breaker, thanks
        to the wait_for timeout in _route.
        """
        async def hang():
            await asyncio.sleep(10)

        backend._test_redis_mock.now_ms.side_effect = hang
        backend._test_local_mock.now_ms.return_value = 42

        result = await backend.now_ms()

        assert result == 42
        assert backend._breaker._failure_count == 1

    @pytest.mark.asyncio
    async def test_repeated_hangs_open_the_circuit(self, backend):
        async def hang():
            await asyncio.sleep(10)

        backend._test_redis_mock.now_ms.side_effect = hang
        backend._test_local_mock.now_ms.return_value = 1

        for _ in range(backend.failure_threshold):
            await backend.now_ms()

        assert backend.state == CircuitState.OPEN


class TestHybridBackendRecovery:

    @pytest.mark.asyncio
    async def test_successful_probe_closes_circuit_and_serves_from_redis(self, backend):
        await make_redis_fail(backend, backend.failure_threshold)
        assert backend.state == CircuitState.OPEN

        await asyncio.sleep(backend.recovery_timeout_sec * 1.5)

        backend._test_redis_mock.now_ms.side_effect = None
        backend._test_redis_mock.now_ms.return_value = 555

        result = await backend.now_ms()

        assert result == 555
        assert backend.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failed_probe_reopens_and_uses_local(self, backend):
        await make_redis_fail(backend, backend.failure_threshold)
        await asyncio.sleep(backend.recovery_timeout_sec * 1.5)

        # probe attempt still fails
        backend._test_local_mock.now_ms.return_value = 888

        result = await backend.now_ms()

        assert result == 888
        assert backend.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_circuit_reprobes_again_after_a_failed_probe(self, backend):
        """
        Regression coverage: after a failed probe, the circuit must return
        to OPEN (not get stuck in HALF_OPEN) so a later timeout window can
        trigger another probe.
        """
        await make_redis_fail(backend, backend.failure_threshold)
        await asyncio.sleep(backend.recovery_timeout_sec * 1.5)
        backend._test_local_mock.now_ms.return_value = 1
        await backend.now_ms()  # failed probe
        assert backend.state == CircuitState.OPEN

        await asyncio.sleep(backend.recovery_timeout_sec * 1.5)
        backend._test_redis_mock.now_ms.side_effect = None
        backend._test_redis_mock.now_ms.return_value = 999

        result = await backend.now_ms()

        assert result == 999
        assert backend.state == CircuitState.CLOSED


class TestHybridBackendLocalFallbackFailure:

    @pytest.mark.asyncio
    async def test_local_exception_is_logged_and_reraised_not_swallowed(self, backend):
        backend._test_redis_mock.now_ms.side_effect = ConnectionError("redis down")
        backend._test_local_mock.now_ms.side_effect = RuntimeError("local cache corrupted")

        with pytest.raises(RuntimeError, match="local cache corrupted"):
            await backend.now_ms()

    @pytest.mark.asyncio
    async def test_local_exception_while_open_still_propagates(self, backend):
        await make_redis_fail(backend, backend.failure_threshold)
        backend._test_local_mock.now_ms.side_effect = RuntimeError("local cache corrupted")

        with pytest.raises(RuntimeError):
            await backend.now_ms()


class TestHybridBackendLifecycle:

    @pytest.mark.asyncio
    async def test_connect_delegates_to_redis(self, backend):
        await backend.connect()
        backend._test_redis_mock.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_delegates_to_redis(self, backend):
        await backend.close()
        backend._test_redis_mock.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ping_delegates_to_redis(self, backend):
        backend._test_redis_mock.ping.return_value = True
        result = await backend.ping()
        assert result is True

    @pytest.mark.asyncio
    async def test_local_cache_size_delegates(self, backend):
        backend._test_local_mock.current_size.return_value = {"token_bucket": 42}
        result = await backend.local_cache_size() 
        assert result == {"token_bucket": 42}


class TestHybridBackendConcurrency:

    @pytest.mark.asyncio
    async def test_concurrent_calls_around_the_threshold_open_exactly_once(self, backend):
        """
        With the generation-fenced breaker, concurrent failures should
        still deterministically open the circuit, and once open, later
        stale successes/failures from calls issued before the transition
        must not un-open it. We can't directly observe the "logged once"
        property here, but we can assert the end state is stable and
        correct after a burst of concurrent failing calls.
        """
        backend._test_redis_mock.now_ms.side_effect = ConnectionError("down")
        backend._test_local_mock.now_ms.return_value = 0

        await asyncio.gather(*(backend.now_ms() for _ in range(10)))

        assert backend.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_concurrent_mixed_success_and_failure_does_not_corrupt_state(self, backend):
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise ConnectionError("intermittent")
            return call_count

        backend._test_redis_mock.now_ms.side_effect = flaky
        backend._test_local_mock.now_ms.return_value = -1

        results = await asyncio.gather(*(backend.now_ms() for _ in range(8)))

        assert backend.state in (CircuitState.CLOSED, CircuitState.OPEN)
        assert all(r is not None for r in results)