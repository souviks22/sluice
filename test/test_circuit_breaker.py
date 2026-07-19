"""
Test suite for CircuitBreaker (pure state machine).
Uses no live Redis, no async, no mocks — just pure Python.
"""

from __future__ import annotations
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sluice.backends.hybrid_backend import CircuitBreaker, CircuitState


class TestCircuitBreakerInitialState:

    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.is_degraded is False

    def test_starts_at_generation_zero(self):
        cb = CircuitBreaker()
        assert cb.generation == 0

    def test_closed_routes_to_redis(self):
        cb = CircuitBreaker()
        use_redis, gen = cb.should_use_redis()
        assert use_redis is True
        assert gen == 0


class TestCircuitBreakerFailuresBelowThreshold:

    def test_single_failure_stays_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        _, gen = cb.should_use_redis()
        cb.record_failure(gen)
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 1

    def test_failures_just_below_threshold_stay_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(2):
            _, gen = cb.should_use_redis()
            cb.record_failure(gen)
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 2

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(2):
            _, gen = cb.should_use_redis()
            cb.record_failure(gen)
        _, gen = cb.should_use_redis()
        cb.record_success(gen)
        assert cb._failure_count == 0
        assert cb.state == CircuitState.CLOSED

    def test_generation_unchanged_while_closed(self):
        cb = CircuitBreaker(failure_threshold=5)
        _, gen = cb.should_use_redis()
        cb.record_failure(gen)
        assert cb.generation == 0  # no transition yet


class TestCircuitBreakerOpens:

    def test_threshold_failures_open_circuit(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            _, gen = cb.should_use_redis()
            cb.record_failure(gen)
        assert cb.state == CircuitState.OPEN
        assert cb.is_degraded is True

    def test_opening_bumps_generation(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            _, gen = cb.should_use_redis()
            cb.record_failure(gen)
        assert cb.generation == 1

    def test_open_routes_to_local_before_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_sec=10.0)
        _, gen = cb.should_use_redis()
        cb.record_failure(gen)
        assert cb.state == CircuitState.OPEN

        use_redis, _ = cb.should_use_redis()
        assert use_redis is False

    def test_stale_failure_does_not_affect_already_open_circuit(self):
        # failures recorded against a stale generation must not affect state
        cb = CircuitBreaker(failure_threshold=2)
        _, gen0 = cb.should_use_redis()
        cb.record_failure(gen0)
        _, gen0b = cb.should_use_redis()
        cb.record_failure(gen0b)
        assert cb.state == CircuitState.OPEN
        gen_after_open = cb.generation

        # a stale failure from before the circuit opened should be dropped
        cb.record_failure(gen0)  # stale generation
        assert cb.generation == gen_after_open
        assert cb.state == CircuitState.OPEN


class TestCircuitBreakerRecovery:

    def test_probe_after_timeout_routes_to_redis_and_enters_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_sec=0.01, recovery_jitter_frac=0.0)
        _, gen = cb.should_use_redis()
        cb.record_failure(gen)
        assert cb.state == CircuitState.OPEN

        time.sleep(0.02)
        use_redis, probe_gen = cb.should_use_redis()

        assert use_redis is True
        assert cb.state == CircuitState.HALF_OPEN
        assert probe_gen == cb.generation

    def test_only_one_probe_in_flight(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_sec=0.01, recovery_jitter_frac=0.0)
        _, gen = cb.should_use_redis()
        cb.record_failure(gen)
        time.sleep(0.02)

        first_use_redis, _ = cb.should_use_redis()   # becomes the probe
        assert first_use_redis is True
        assert cb.state == CircuitState.HALF_OPEN

        second_use_redis, _ = cb.should_use_redis()  # concurrent caller
        assert second_use_redis is False

    def test_successful_probe_closes_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_sec=0.01, recovery_jitter_frac=0.0)
        _, gen = cb.should_use_redis()
        cb.record_failure(gen)
        time.sleep(0.02)

        _, probe_gen = cb.should_use_redis()
        cb.record_success(probe_gen)

        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 0

    def test_failed_probe_reopens_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_sec=0.01, recovery_jitter_frac=0.0)
        _, gen = cb.should_use_redis()
        cb.record_failure(gen)
        time.sleep(0.02)

        _, probe_gen = cb.should_use_redis()
        cb.record_failure(probe_gen)

        assert cb.state == CircuitState.OPEN

    def test_reopening_after_failed_probe_still_routes_to_local(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_sec=0.02, recovery_jitter_frac=0.0)
        _, gen = cb.should_use_redis()
        cb.record_failure(gen)
        time.sleep(0.03)

        _, probe_gen = cb.should_use_redis()
        cb.record_failure(probe_gen)  # probe failed -> OPEN again
        assert cb.state == CircuitState.OPEN

        # immediately after reopening, should still route to local
        use_redis, _ = cb.should_use_redis()
        assert use_redis is False

    def test_stale_probe_result_is_ignored(self):
        """
        A probe's result must only apply if the generation still matches.
        If, hypothetically, two completions raced for the same probe slot,
        the second (stale) one must not flip state.
        """
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_sec=0.01, recovery_jitter_frac=0.0)
        _, gen = cb.should_use_redis()
        cb.record_failure(gen)
        time.sleep(0.02)

        _, probe_gen = cb.should_use_redis()
        cb.record_success(probe_gen)  # closes circuit, bumps generation
        closed_gen = cb.generation
        assert cb.state == CircuitState.CLOSED

        # a late/duplicate completion from the old probe generation arrives
        cb.record_failure(probe_gen)
        assert cb.state == CircuitState.CLOSED
        assert cb.generation == closed_gen


class TestCircuitBreakerGenerationFencing:
    """
    The core regression coverage: a stale completion (issued under an old
    generation) must never be able to mutate current state.
    """

    def test_late_success_after_circuit_opened_is_ignored(self):
        cb = CircuitBreaker(failure_threshold=2)
        _, gen_a = cb.should_use_redis()   # call A issued while CLOSED
        _, gen_b = cb.should_use_redis()   # call B issued while CLOSED
        cb.record_failure(gen_b)
        cb.record_failure(gen_b)           # threshold reached -> OPEN
        assert cb.state == CircuitState.OPEN
        gen_after_open = cb.generation

        # call A finally resolves successfully, but against a stale generation
        cb.record_success(gen_a)

        assert cb.state == CircuitState.OPEN  # unaffected
        assert cb.generation == gen_after_open

    def test_late_failure_after_recovery_cannot_reopen_circuit(self):
        """
        The scenario that motivated generation fencing: a request issued
        long before an outage, which is still hanging, must not be able to
        re-trip a circuit that has since recovered.
        """
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_sec=0.01, recovery_jitter_frac=0.0)

        _, stale_gen = cb.should_use_redis()  # a request issued pre-outage, still "in flight"

        _, gen = cb.should_use_redis()
        cb.record_failure(gen)  # circuit opens
        time.sleep(0.02)
        _, probe_gen = cb.should_use_redis()
        cb.record_success(probe_gen)  # circuit recovers
        assert cb.state == CircuitState.CLOSED
        recovered_gen = cb.generation

        # the ancient straggler finally times out and reports failure
        cb.record_failure(stale_gen)

        assert cb.state == CircuitState.CLOSED
        assert cb.generation == recovered_gen
        assert cb._failure_count == 0


class TestCircuitBreakerJitter:

    def test_jitter_zero_gives_exact_timeout(self):
        cb = CircuitBreaker(recovery_timeout_sec=10.0, recovery_jitter_frac=0.0)
        assert cb._jittered_timeout() == 10.0

    def test_jitter_stays_within_bounds(self):
        cb = CircuitBreaker(recovery_timeout_sec=10.0, recovery_jitter_frac=0.2)
        for _ in range(200):
            t = cb._jittered_timeout()
            assert 8.0 <= t <= 12.0

    def test_deadline_is_recomputed_on_each_open_transition(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_sec=5.0, recovery_jitter_frac=0.5)
        _, gen = cb.should_use_redis()
        cb.record_failure(gen)
        first_deadline = cb._current_recovery_deadline_sec
        assert 2.5 <= first_deadline <= 7.5
