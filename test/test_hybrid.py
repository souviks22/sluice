"""
Test suite for HybridBackend (circuit breaker wrapping RedisBackend).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from sluice.backends.hybrid_backend import HybridBackend, CircuitState


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def backend():
    """
    A HybridBackend with RedisBackend.from_url and FallbackBackend mocked out,
    so no real network/cache is touched. Failure threshold and recovery
    timeout are kept small for fast tests.
    """
    with patch("sluice.backends.hybrid_backend.RedisBackend") as MockRedisCls, \
         patch("sluice.backends.hybrid_backend.FallbackBackend") as MockFallbackCls:

        mock_redis = AsyncMock()
        MockRedisCls.from_url.return_value = mock_redis

        mock_local = AsyncMock()
        MockFallbackCls.return_value = mock_local

        hb = HybridBackend(
            redis_url="redis://fake:6379/0",
            failure_threshold=3,
            recovery_timeout_sec=0.05,   # short, so tests don't sleep long
            local_maxsize=100,
            local_ttl_sec=60.0,
        )
        # expose mocks on the instance for assertions
        hb._test_redis_mock = mock_redis
        hb._test_local_mock = mock_local
        yield hb


async def make_redis_fail(hb, times: int):
    """Drive `times` consecutive Redis failures through now_ms()."""
    hb._test_redis_mock.now_ms.side_effect = ConnectionError("redis down")
    for _ in range(times):
        await hb.now_ms()


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

class TestInitialState:

    def test_starts_closed(self, backend):
        assert backend.state == CircuitState.CLOSED
        assert backend.is_degraded is False

    def test_failure_count_starts_at_zero(self, backend):
        assert backend._failure_count == 0


# ---------------------------------------------------------------------------
# Happy path — Redis healthy
# ---------------------------------------------------------------------------

class TestHealthyRedis:

    @pytest.mark.asyncio
    async def test_successful_call_uses_redis(self, backend):
        backend._test_redis_mock.now_ms.return_value = 12345
        result = await backend.now_ms()
        assert result == 12345
        backend._test_redis_mock.now_ms.assert_awaited_once()
        backend._test_local_mock.now_ms.assert_not_awaited()
        assert backend.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self, backend):
        # two failures, then a success
        await make_redis_fail(backend, 2)
        assert backend._failure_count == 2

        backend._test_redis_mock.now_ms.side_effect = None
        backend._test_redis_mock.now_ms.return_value = 999
        await backend.now_ms()

        assert backend._failure_count == 0
        assert backend.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_evalsha_routes_to_redis_when_closed(self, backend):
        backend._test_redis_mock.evalsha.return_value = [1, 0]
        result = await backend.evalsha("token_bucket", ["k1"], [10, 1])
        assert result == [1, 0]
        backend._test_redis_mock.evalsha.assert_awaited_once_with(
            "token_bucket", ["k1"], [10, 1]
        )
        backend._test_local_mock.evalsha.assert_not_awaited()


# ---------------------------------------------------------------------------
# Failures below threshold — still CLOSED, falls back per-call
# ---------------------------------------------------------------------------

class TestFailuresBelowThreshold:

    @pytest.mark.asyncio
    async def test_single_failure_stays_closed(self, backend):
        backend._test_redis_mock.now_ms.side_effect = ConnectionError("down")
        backend._test_local_mock.now_ms.return_value = 111

        result = await backend.now_ms()

        assert result == 111
        assert backend.state == CircuitState.CLOSED  # below threshold (3)
        assert backend._failure_count == 1
        backend._test_local_mock.now_ms.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failures_just_below_threshold_stay_closed(self, backend):
        await make_redis_fail(backend, backend.failure_threshold - 1)
        assert backend.state == CircuitState.CLOSED
        assert backend._failure_count == backend.failure_threshold - 1

    @pytest.mark.asyncio
    async def test_each_failed_call_still_returns_local_result(self, backend):
        backend._test_redis_mock.now_ms.side_effect = ConnectionError("down")
        backend._test_local_mock.now_ms.return_value = 42
        for _ in range(backend.failure_threshold - 1):
            result = await backend.now_ms()
            assert result == 42


# ---------------------------------------------------------------------------
# Threshold reached — circuit OPENs
# ---------------------------------------------------------------------------

class TestCircuitOpens:

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

    @pytest.mark.asyncio
    async def test_open_circuit_does_not_reprobe_before_timeout(self, backend):
        # recovery_timeout_sec is 0.05s; call again immediately
        await make_redis_fail(backend, backend.failure_threshold)
        backend._test_redis_mock.ping.reset_mock()

        await backend.now_ms()  # should stay within timeout window

        backend._test_redis_mock.ping.assert_not_awaited()
        assert backend.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# Recovery probing (HALF_OPEN)
# ---------------------------------------------------------------------------

class TestRecoveryProbing:

    @pytest.mark.asyncio
    async def test_successful_probe_closes_circuit(self, backend):
        await make_redis_fail(backend, backend.failure_threshold)
        assert backend.state == CircuitState.OPEN

        await asyncio.sleep(backend.recovery_timeout_sec * 1.5)

        backend._test_redis_mock.ping.return_value = True
        backend._test_redis_mock.now_ms.side_effect = None
        backend._test_redis_mock.now_ms.return_value = 555

        result = await backend.now_ms()

        backend._test_redis_mock.ping.assert_awaited_once()
        assert backend.state == CircuitState.CLOSED
        assert backend._failure_count == 0
        # after a successful probe, the call itself is served by redis
        assert result == 555

    @pytest.mark.asyncio
    async def test_failed_probe_falls_back_to_local(self, backend):
        await make_redis_fail(backend, backend.failure_threshold)
        await asyncio.sleep(backend.recovery_timeout_sec * 1.5)

        backend._test_redis_mock.ping.return_value = False
        backend._test_local_mock.now_ms.return_value = 888

        result = await backend.now_ms()

        backend._test_redis_mock.ping.assert_awaited_once()
        assert result == 888

    @pytest.mark.asyncio
    async def test_probe_exception_treated_as_failed_recovery(self, backend):
        await make_redis_fail(backend, backend.failure_threshold)
        await asyncio.sleep(backend.recovery_timeout_sec * 1.5)

        backend._test_redis_mock.ping.side_effect = ConnectionError("still down")
        backend._test_local_mock.now_ms.return_value = 321

        result = await backend.now_ms()

        assert result == 321
        # circuit must not incorrectly report CLOSED after an exception
        assert backend.state != CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_stuck_after_failed_probe_never_reprobes(self, backend):
        """
        Regression test for a state-machine gap: after a failed probe,
        _route sets state to HALF_OPEN but never resets it to OPEN.
        Because the recovery-timeout check only lives in the OPEN branch,
        the circuit can get stuck in HALF_OPEN and route to local forever,
        even long after recovery_timeout_sec has elapsed again.
        """
        await make_redis_fail(backend, backend.failure_threshold)
        await asyncio.sleep(backend.recovery_timeout_sec * 1.5)

        backend._test_redis_mock.ping.return_value = False
        backend._test_local_mock.now_ms.return_value = 1

        await backend.now_ms()  # failed probe -> state left as HALF_OPEN
        assert backend.state == CircuitState.HALF_OPEN

        # Wait well past another recovery window and make Redis healthy again.
        await asyncio.sleep(backend.recovery_timeout_sec * 3)
        backend._test_redis_mock.ping.reset_mock()
        backend._test_redis_mock.ping.return_value = True
        backend._test_redis_mock.now_ms.side_effect = None
        backend._test_redis_mock.now_ms.return_value = 999

        await backend.now_ms()

        # Documents current (likely unintended) behavior: no new probe is
        # attempted because the HALF_OPEN branch always routes to local
        # without checking elapsed time. If this assertion starts failing,
        # the bug has been fixed upstream and this test should be flipped.
        backend._test_redis_mock.ping.assert_not_awaited()
        assert backend.state == CircuitState.HALF_OPEN


# ---------------------------------------------------------------------------
# evalsha routing mirrors now_ms routing
# ---------------------------------------------------------------------------

class TestEvalshaRouting:

    @pytest.mark.asyncio
    async def test_evalsha_falls_back_on_redis_failure(self, backend):
        backend._test_redis_mock.evalsha.side_effect = ConnectionError("down")
        backend._test_local_mock.evalsha.return_value = [0, 5]

        result = await backend.evalsha("sliding_window", ["k1"], [60, 100])

        assert result == [0, 5]
        backend._test_local_mock.evalsha.assert_awaited_once_with(
            "sliding_window", ["k1"], [60, 100]
        )

    @pytest.mark.asyncio
    async def test_evalsha_uses_local_when_circuit_open(self, backend):
        await make_redis_fail(backend, backend.failure_threshold)
        backend._test_redis_mock.evalsha.reset_mock()
        backend._test_local_mock.evalsha.return_value = [1, 1]

        result = await backend.evalsha("token_bucket", ["k2"], [10, 1])

        assert result == [1, 1]
        backend._test_redis_mock.evalsha.assert_not_awaited()


# ---------------------------------------------------------------------------
# Lifecycle passthrough
# ---------------------------------------------------------------------------

class TestLifecycle:

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
        backend._test_redis_mock.ping.assert_awaited_once()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:

    @pytest.mark.asyncio
    async def test_concurrent_failures_do_not_undercount_below_threshold(self, backend):
        """
        _record_failure() is not guarded by self._lock, so concurrent
        failing calls race on `self._failure_count += 1`. This test
        documents expected worst-case behavior: total failures recorded
        should be <= number of calls made (never more), and the circuit
        should have opened by the time all tasks complete since call
        count (10) far exceeds failure_threshold (3).
        """
        backend._test_redis_mock.now_ms.side_effect = ConnectionError("down")
        backend._test_local_mock.now_ms.return_value = 0

        await asyncio.gather(*(backend.now_ms() for _ in range(10)))

        assert backend._failure_count <= 10
        assert backend.state == CircuitState.OPEN