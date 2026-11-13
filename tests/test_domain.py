"""Pure domain model tests: no Redis server or fakeredis required."""

from dataclasses import FrozenInstanceError

import pytest

from sluice.domain import (
    InvalidCostError,
    InvalidIdentifierError,
    RateLimitConfigurationError,
    RateLimitResult,
    SlidingWindowConfig,
    TokenBucketConfig,
    SystemClock,
    validate_cost,
    validate_identifier,
)
from sluice.domain.validation import MAX_LUA_SAFE_QUOTA, MAX_LUA_SAFE_WINDOW_MS


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
def test_token_bucket_rejects_invalid_capacity(capacity):
    with pytest.raises(RateLimitConfigurationError):
        TokenBucketConfig(capacity=capacity)


@pytest.mark.parametrize("rate", [0, -1, float("inf"), float("nan"), True])
def test_token_bucket_rejects_invalid_refill_rate(rate):
    with pytest.raises(RateLimitConfigurationError):
        TokenBucketConfig(refill_rate=rate)


def test_token_bucket_rejects_refill_rate_that_overflows_retry_milliseconds():
    with pytest.raises(RateLimitConfigurationError, match="too small"):
        TokenBucketConfig(capacity=1, refill_rate=1e-15)


@pytest.mark.parametrize("capacity", [MAX_LUA_SAFE_QUOTA + 1, 2**53])
def test_token_bucket_rejects_quota_outside_exact_lua_range(capacity):
    with pytest.raises(RateLimitConfigurationError, match="exact Redis Lua arithmetic"):
        TokenBucketConfig(capacity=capacity, refill_rate=1_000_000)


@pytest.mark.parametrize("limit,window", [(0, 1000), (1, 0), (-1, 1000), (1, -1)])
def test_sliding_window_rejects_non_positive_values(limit, window):
    with pytest.raises(RateLimitConfigurationError):
        SlidingWindowConfig(limit=limit, window_ms=window)


def test_sliding_window_rejects_quota_and_window_outside_exact_lua_range():
    with pytest.raises(RateLimitConfigurationError, match="exact Redis Lua arithmetic"):
        SlidingWindowConfig(limit=MAX_LUA_SAFE_QUOTA + 1, window_ms=1000)
    with pytest.raises(RateLimitConfigurationError, match="exact Redis Lua arithmetic"):
        SlidingWindowConfig(limit=10, window_ms=MAX_LUA_SAFE_WINDOW_MS + 1)


@pytest.mark.parametrize("cost", [0, -1, True, 1.5, "1"])
def test_cost_must_be_positive_integer(cost):
    with pytest.raises(InvalidCostError):
        validate_cost(cost)


@pytest.mark.parametrize("identifier", ["", None, 4, "x" * 1025, "a\x00b"])
def test_identifier_is_validated(identifier):
    with pytest.raises(InvalidIdentifierError):
        validate_identifier(identifier)


def test_configuration_is_immutable():
    config = TokenBucketConfig(capacity=10, refill_rate=2)
    with pytest.raises(FrozenInstanceError):
        config.capacity = 20


def test_refill_rate_is_normalized_to_float():
    assert TokenBucketConfig(refill_rate=2).refill_rate == 2.0


def test_result_is_immutable_and_rejects_invalid_values():
    result = RateLimitResult(True, 3, 1000, 0, "token_bucket", "rl:test")
    with pytest.raises(FrozenInstanceError):
        result.remaining = 4
    with pytest.raises(ValueError):
        RateLimitResult(False, -1, 0, 1, "token_bucket", "rl:test")


def test_allowed_result_cannot_have_retry_delay():
    with pytest.raises(ValueError):
        RateLimitResult(True, 1, 100, 10, "token_bucket", "rl:test")


def test_system_clock_returns_epoch_milliseconds():
    now = SystemClock().now_ms()
    assert isinstance(now, int)
    assert now > 0


def test_rate_limit_operation_is_immutable_and_validated():
    from dataclasses import FrozenInstanceError
    from sluice.domain import RateLimitOperation

    operation = RateLimitOperation(
        algorithm="token_bucket",
        key="rl:api:client-1",
        config=TokenBucketConfig(capacity=10, refill_rate=2),
        cost=3,
    )
    assert operation.cost == 3
    with pytest.raises(FrozenInstanceError):
        operation.cost = 4


def test_rate_limit_operation_rejects_algorithm_config_mismatch():
    from sluice.domain import RateLimitOperation

    with pytest.raises(RateLimitConfigurationError):
        RateLimitOperation(
            algorithm="token_bucket",
            key="rl:client",
            config=SlidingWindowConfig(limit=10, window_ms=1000),
        )


def test_rate_limit_operation_rejects_cost_above_capacity_or_limit():
    from sluice.domain import RateLimitOperation

    with pytest.raises(InvalidCostError):
        RateLimitOperation(
            algorithm="token_bucket",
            key="rl:client",
            config=TokenBucketConfig(capacity=3, refill_rate=1),
            cost=4,
        )
    with pytest.raises(InvalidCostError):
        RateLimitOperation(
            algorithm="sliding_window_log",
            key="rl:client",
            config=SlidingWindowConfig(limit=3, window_ms=1000),
            cost=4,
        )
