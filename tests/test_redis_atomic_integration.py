"""Real-Redis atomicity and recovery tests with pytest-owned test services."""

from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
import os
import socket
import subprocess
import uuid
from contextlib import contextmanager
from urllib.request import Request, urlopen

import pytest

from redis.asyncio import Redis

from sluice.algorithms import SlidingWindowCounter, SlidingWindowLog, TokenBucket
from sluice.backends import FallbackBackend, RedisBackend
from sluice.backends.hybrid_backend import CircuitState, HybridBackend


REDIS_URL = os.getenv("SLUICE_TEST_REDIS_URL", "redis://localhost:6379/15")
REDIS_IMAGE = os.getenv("SLUICE_TEST_REDIS_IMAGE", "redis:7.4-alpine")
TOXIPROXY_IMAGE = os.getenv(
    "SLUICE_TEST_TOXIPROXY_IMAGE", "ghcr.io/shopify/toxiproxy:2.12.0"
)
pytestmark = pytest.mark.redis_integration


def _docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True
    )
    return result.stdout.strip()


def _require_free_port(port: int) -> None:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise RuntimeError(
            f"Port {port} is required for Redis integration tests but is occupied"
        ) from exc
    finally:
        probe.close()


def _wait_for_redis(container: str) -> None:
    import time

    for _ in range(30):
        result = subprocess.run(
            ["docker", "exec", container, "redis-cli", "ping"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip() == "PONG":
            return
        time.sleep(1)
    raise RuntimeError(f"Redis container {container!r} did not become ready")


@contextmanager
def _redis_container(
    label: str,
    port: int | None,
    *,
    network: str | None = None,
    network_alias: str | None = None,
    net_admin: bool = False,
):
    if port is not None:
        _require_free_port(port)
    container = f"sluice-pytest-{label}-{uuid.uuid4().hex[:8]}"
    command = ["run", "--detach", "--rm", "--name", container]
    if net_admin:
        command.extend(["--cap-add", "NET_ADMIN"])
    if network:
        command.extend(["--network", network])
    if network_alias:
        command.extend(["--network-alias", network_alias])
    if port is not None:
        command.extend(["--publish", f"{port}:6379"])
    command.extend([
        REDIS_IMAGE, "redis-server", "--save", "", "--appendonly", "no",
    ])
    _docker(*command)
    try:
        _wait_for_redis(container)
        url = f"redis://localhost:{port}/15" if port is not None else None
        yield url, container
    finally:
        _docker("stop", container, check=False)


@contextmanager
def _docker_network(label: str):
    network = f"sluice-pytest-{label}-{uuid.uuid4().hex[:8]}"
    _docker("network", "create", network)
    try:
        yield network
    finally:
        _docker("network", "rm", network, check=False)


@contextmanager
def _toxiproxy(network: str):
    _require_free_port(8474)
    _require_free_port(6382)
    container = f"sluice-pytest-toxiproxy-{uuid.uuid4().hex[:8]}"
    _docker(
        "run", "--detach", "--rm", "--name", container,
        "--network", network,
        "--publish", "8474:8474",
        "--publish", "6382:8666",
        TOXIPROXY_IMAGE,
    )
    api_url = "http://localhost:8474"
    try:
        for _ in range(30):
            try:
                with urlopen(f"{api_url}/version", timeout=1):
                    break
            except Exception:
                import time
                time.sleep(1)
        else:
            raise RuntimeError("Toxiproxy API did not become ready")
        yield api_url
    finally:
        _docker("stop", container, check=False)


@pytest.fixture(scope="module", autouse=True)
def redis_service():
    """Use an explicitly supplied Redis URL or start a disposable local Redis."""
    if os.getenv("SLUICE_TEST_REDIS_URL"):
        yield
        return
    with _redis_container("integration", 6379):
        yield


@pytest.mark.asyncio
async def test_disposable_redis_is_reachable_and_executes_sluice_lua():
    backend = RedisBackend.from_url(REDIS_URL)
    await backend.connect()
    try:
        assert await backend.ping()
        limiter = SlidingWindowLog(
            backend, limit=3, window_ms=60_000,
            key_prefix=f"sluice:smoke:{uuid.uuid4().hex}",
        )
        outcomes = [await limiter.check("smoke-client") for _ in range(4)]
        assert [item.allowed for item in outcomes] == [True, True, True, False]
    finally:
        await backend.close()


@pytest.fixture
async def backend():
    instance = RedisBackend.from_url(REDIS_URL)
    await instance.connect()
    try:
        assert await instance.ping()
        yield instance
    finally:
        await instance.close()


def unique_prefix() -> str:
    return f"sluice:integration:{uuid.uuid4().hex}"


_PROCESS_START_BARRIER = None


def _set_process_start_barrier(barrier) -> None:
    global _PROCESS_START_BARRIER
    _PROCESS_START_BARRIER = barrier


def _process_admission_worker(url: str, prefix: str, algorithm: str) -> int:
    async def run() -> int:
        backend = RedisBackend.from_url(url)
        await backend.connect()
        try:
            if algorithm == "token_bucket":
                limiter = TokenBucket(
                    backend, capacity=12, refill_rate=0.0001, key_prefix=prefix
                )
            elif algorithm == "sliding_window_log":
                limiter = SlidingWindowLog(
                    backend, limit=12, window_ms=60_000, key_prefix=prefix
                )
            else:
                limiter = SlidingWindowCounter(
                    backend, limit=12, window_ms=60_000, key_prefix=prefix
                )

            assert _PROCESS_START_BARRIER is not None
            _PROCESS_START_BARRIER.wait(timeout=20)
            results = await asyncio.gather(
                *(limiter.check("multi-process-client") for _ in range(30))
            )
            return sum(result.allowed for result in results)
        finally:
            await backend.close()

    return asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["token_bucket", "sliding_window_log", "sliding_window_counter"])
async def test_concurrent_single_cost_admission_never_exceeds_limit(backend, algorithm):
    limit = 12
    prefix = unique_prefix()
    if algorithm == "token_bucket":
        limiter = TokenBucket(backend, capacity=limit, refill_rate=0.0001, key_prefix=prefix)
    elif algorithm == "sliding_window_log":
        limiter = SlidingWindowLog(backend, limit=limit, window_ms=60_000, key_prefix=prefix)
    else:
        limiter = SlidingWindowCounter(backend, limit=limit, window_ms=60_000, key_prefix=prefix)

    results = await asyncio.gather(*(limiter.check("same-client") for _ in range(120)))
    assert sum(result.allowed for result in results) == limit
    assert all(result.remaining >= 0 for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["token_bucket", "sliding_window_log", "sliding_window_counter"])
async def test_admission_is_atomic_across_independent_redis_clients(backend, algorithm):
    """Exercise contention through separate pools, as happens across workers."""
    limit = 12
    prefix = unique_prefix()
    clients = [backend]
    clients.extend(RedisBackend.from_url(REDIS_URL) for _ in range(3))
    for client in clients[1:]:
        await client.connect()

    try:
        def limiter(client):
            if algorithm == "token_bucket":
                return TokenBucket(client, capacity=limit, refill_rate=0.0001, key_prefix=prefix)
            if algorithm == "sliding_window_log":
                return SlidingWindowLog(client, limit=limit, window_ms=60_000, key_prefix=prefix)
            return SlidingWindowCounter(client, limit=limit, window_ms=60_000, key_prefix=prefix)

        operations = [limiter(clients[index % len(clients)]).check("multi-worker") for index in range(120)]
        results = await asyncio.gather(*operations)
        assert sum(result.allowed for result in results) == limit
        assert all(result.remaining >= 0 for result in results)
    finally:
        for client in clients[1:]:
            await client.close()


@pytest.mark.parametrize(
    "algorithm", ["token_bucket", "sliding_window_log", "sliding_window_counter"]
)
def test_admission_is_atomic_across_independent_processes(algorithm):
    """Exercise concurrent limiter calls from separate OS processes."""
    process_count = 4
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(process_count)
    prefix = unique_prefix()
    with ProcessPoolExecutor(
        max_workers=process_count,
        mp_context=context,
        initializer=_set_process_start_barrier,
        initargs=(barrier,),
    ) as workers:
        admitted = list(
            workers.map(
                _process_admission_worker,
                [REDIS_URL] * process_count,
                [prefix] * process_count,
                [algorithm] * process_count,
            )
        )

    assert sum(admitted) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["token_bucket", "sliding_window_log", "sliding_window_counter"])
async def test_admission_state_has_a_bounded_redis_ttl(backend, algorithm):
    prefix = unique_prefix()
    if algorithm == "token_bucket":
        limiter = TokenBucket(backend, capacity=12, refill_rate=10, key_prefix=prefix)
    elif algorithm == "sliding_window_log":
        limiter = SlidingWindowLog(backend, limit=12, window_ms=60_000, key_prefix=prefix)
    else:
        limiter = SlidingWindowCounter(backend, limit=12, window_ms=60_000, key_prefix=prefix)

    await limiter.check("ttl-client")
    ttl_ms = await backend._client.pttl(f"{prefix}:ttl-client")
    assert 0 < ttl_ms <= (61_000 if algorithm == "sliding_window_log" else 121_000)


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["token_bucket", "sliding_window_log", "sliding_window_counter"])
async def test_weighted_admission_is_all_or_nothing(backend, algorithm):
    limit = 10
    prefix = unique_prefix()
    if algorithm == "token_bucket":
        limiter = TokenBucket(backend, capacity=limit, refill_rate=0.0001, key_prefix=prefix)
    elif algorithm == "sliding_window_log":
        limiter = SlidingWindowLog(backend, limit=limit, window_ms=60_000, key_prefix=prefix)
    else:
        limiter = SlidingWindowCounter(backend, limit=limit, window_ms=60_000, key_prefix=prefix)

    results = await asyncio.gather(*(limiter.check("weighted-client", cost=3) for _ in range(20)))
    assert sum(result.allowed for result in results) == 3

    # Exactly one unit remains; a rejected cost=2 must not consume it.
    rejected = await limiter.check("weighted-client", cost=2)
    assert not rejected.allowed
    final_unit = await limiter.check("weighted-client", cost=1)
    assert final_unit.allowed


@pytest.mark.asyncio
async def test_noscript_after_script_flush_is_recovered(backend):
    limiter = SlidingWindowLog(backend, limit=5, window_ms=60_000, key_prefix=unique_prefix())
    first = await limiter.check("script-cache-client")
    assert first.allowed

    # Simulates Redis restart/script-cache loss without disrupting data.
    await backend._client.script_flush()
    second = await limiter.check("script-cache-client")
    assert second.allowed
    assert second.remaining == 3


@pytest.mark.asyncio
async def test_redis_clock_and_admission_do_not_require_client_clock(backend):
    limiter = SlidingWindowLog(backend, limit=2, window_ms=60_000, key_prefix=unique_prefix())

    async def forbidden_clock():
        raise AssertionError("Redis operation must not request application-side time")

    backend.now_ms = forbidden_clock
    result = await limiter.check("server-time-client")
    assert result.allowed


@pytest.mark.asyncio
async def test_connection_pool_exhaustion_uses_hybrid_local_fallback():
    redis_backend = RedisBackend.from_url(REDIS_URL, max_connections=1)
    await redis_backend.connect()
    pool = redis_backend._client.connection_pool
    held_connection = await pool.get_connection()
    local = FallbackBackend()
    hybrid = HybridBackend(
        redis_backend=redis_backend,
        fallback_backend=local,
        failure_threshold=1,
        recovery_timeout_sec=60,
        recovery_jitter_frac=0,
    )
    limiter = TokenBucket(hybrid, capacity=5, refill_rate=1)

    try:
        result = await limiter.check("pool-exhaustion")
        assert result.allowed
        assert hybrid.state == CircuitState.OPEN
        assert local.current_size()["token_bucket"] == 1
    finally:
        await pool.release(held_connection)
        await redis_backend.close()


@pytest.mark.asyncio
async def test_redis_restart_during_traffic_recovers_hybrid_backend():
    with _redis_container("restart", 6380) as (url, container_name):
        redis_backend = RedisBackend.from_url(
            url, socket_connect_timeout=1, socket_timeout=1
        )
        admin = Redis.from_url(
            url, decode_responses=True,
            socket_connect_timeout=1, socket_timeout=1,
        )
        local = FallbackBackend()
        hybrid = HybridBackend(
            redis_backend=redis_backend,
            fallback_backend=local,
            failure_threshold=1,
            recovery_timeout_sec=0.01,
            recovery_jitter_frac=0,
            redis_call_timeout_sec=0.25,
        )
        limiter = TokenBucket(
            hybrid, capacity=200, refill_rate=1, key_prefix=unique_prefix()
        )
        try:
            await hybrid.connect()
            assert (await limiter.check("restart-client")).allowed

            async def active_traffic():
                results = []
                for _ in range(100):
                    results.append(await limiter.check("restart-client"))
                    await asyncio.sleep(0.002)
                return results

            traffic = asyncio.create_task(active_traffic())
            await asyncio.sleep(0.01)
            restart = await asyncio.create_subprocess_exec(
                "docker", "restart", container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await restart.communicate()
            assert restart.returncode == 0, stderr.decode()

            for _ in range(100):
                if await hybrid.ping():
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Redis did not become healthy after its container restarted")

            results = await traffic
            assert len(results) == 100
            assert all(result.allowed for result in results)
            await limiter.check("restart-client")
            assert hybrid.state == CircuitState.CLOSED
        finally:
            if "traffic" in locals() and not traffic.done():
                await traffic
            await admin.aclose()
            await hybrid.close()


@pytest.mark.asyncio
async def test_redis_network_partition_uses_fallback_and_recovers():
    async def docker(*args: str) -> None:
        process = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()

    with _docker_network("partition") as network_name:
        with _redis_container(
            "partition", 6381, network=network_name
        ) as (url, container_name):
            redis_backend = RedisBackend.from_url(
                url, socket_connect_timeout=0.2, socket_timeout=0.2
            )
            local = FallbackBackend()
            hybrid = HybridBackend(
                redis_backend=redis_backend,
                fallback_backend=local,
                failure_threshold=1,
                recovery_timeout_sec=0.5,
                recovery_jitter_frac=0,
                redis_call_timeout_sec=0.2,
            )
            limiter = TokenBucket(
                hybrid, capacity=200, refill_rate=1, key_prefix=unique_prefix()
            )
            try:
                await hybrid.connect()
                assert (await limiter.check("partition-client")).allowed

                async def active_traffic():
                    results = []
                    for _ in range(100):
                        results.append(await limiter.check("partition-client"))
                        await asyncio.sleep(0.01)
                    return results

                traffic = asyncio.create_task(active_traffic())
                await asyncio.sleep(0.05)
                await docker("network", "disconnect", network_name, container_name)
                await asyncio.sleep(0.25)
                assert hybrid.state in {CircuitState.OPEN, CircuitState.HALF_OPEN}
                assert local.current_size()["token_bucket"] > 0
                await docker("network", "connect", network_name, container_name)

                for _ in range(100):
                    if await hybrid.ping():
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("Redis did not recover after network reconnection")

                results = await traffic
                assert len(results) == 100
                assert all(result.allowed for result in results)
                assert local.current_size()["token_bucket"] > 0
                await limiter.check("partition-client")
                assert hybrid.state == CircuitState.CLOSED
            finally:
                if "traffic" in locals() and not traffic.done():
                    await traffic
                await hybrid.close()


@pytest.mark.asyncio
async def test_redis_latency_uses_fallback_and_recovers():
    def toxiproxy(method: str, path: str, payload: dict | None = None) -> None:
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"{api_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=3):
            pass

    with _docker_network("latency") as network:
        with _redis_container(
            "latency", None, network=network, network_alias="redis"
        ):
            with _toxiproxy(network) as api_url:
                proxy_name = f"sluice-redis-{uuid.uuid4().hex[:8]}"
                toxiproxy(
                    "POST",
                    "/proxies",
                    {
                        "name": proxy_name,
                        "listen": "0.0.0.0:8666",
                        "upstream": "redis:6379",
                        "enabled": True,
                    },
                )
                redis_backend = RedisBackend.from_url(
                    "redis://localhost:6382/15",
                    socket_connect_timeout=0.2,
                    socket_timeout=0.1,
                )
                local = FallbackBackend()
                hybrid = HybridBackend(
                    redis_backend=redis_backend,
                    fallback_backend=local,
                    failure_threshold=1,
                    recovery_timeout_sec=0.5,
                    recovery_jitter_frac=0,
                    redis_call_timeout_sec=0.15,
                )
                limiter = TokenBucket(
                    hybrid, capacity=200, refill_rate=1, key_prefix=unique_prefix()
                )
                toxic_path = f"/proxies/{proxy_name}/toxics/redis-latency"

                try:
                    await hybrid.connect()
                    assert (await limiter.check("latency-client")).allowed
                    toxiproxy(
                        "POST",
                        f"/proxies/{proxy_name}/toxics",
                        {
                            "name": "redis-latency",
                            "type": "latency",
                            "stream": "downstream",
                            "attributes": {"latency": 500, "jitter": 0},
                        },
                    )

                    async def active_traffic():
                        results = []
                        for _ in range(20):
                            results.append(await limiter.check("latency-client"))
                            await asyncio.sleep(0.01)
                        return results

                    traffic = asyncio.create_task(active_traffic())
                    await asyncio.sleep(0.25)
                    assert hybrid.state in {CircuitState.OPEN, CircuitState.HALF_OPEN}
                    assert local.current_size()["token_bucket"] > 0
                    toxiproxy("DELETE", toxic_path)

                    for _ in range(100):
                        await limiter.check("latency-client")
                        if hybrid.state == CircuitState.CLOSED:
                            break
                        await asyncio.sleep(0.1)
                    else:
                        pytest.fail("Redis did not recover after latency toxic removal")

                    results = await traffic
                    assert len(results) == 20
                    assert all(result.allowed for result in results)
                    assert hybrid.state == CircuitState.CLOSED
                finally:
                    if "traffic" in locals() and not traffic.done():
                        await traffic
                    try:
                        toxiproxy("DELETE", toxic_path)
                    except Exception:
                        pass
                    await hybrid.close()


@pytest.mark.asyncio
async def test_redis_packet_loss_uses_fallback_and_recovers():
    async def docker(*args: str, check: bool = True) -> None:
        process = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if check:
            assert process.returncode == 0, stderr.decode()

    with _redis_container("packet-loss", 6383, net_admin=True) as (url, container_name):
        redis_backend = RedisBackend.from_url(
            url, socket_connect_timeout=0.2, socket_timeout=0.2
        )
        local = FallbackBackend()
        hybrid = HybridBackend(
            redis_backend=redis_backend,
            fallback_backend=local,
            failure_threshold=1,
            recovery_timeout_sec=0.5,
            recovery_jitter_frac=0,
            redis_call_timeout_sec=0.2,
        )
        limiter = TokenBucket(
            hybrid, capacity=200, refill_rate=1, key_prefix=unique_prefix()
        )

        async def remove_packet_loss() -> None:
            await docker(
                "exec", container_name, "tc", "qdisc", "del", "dev", "eth0", "root",
                check=False,
            )

        try:
            await hybrid.connect()
            assert (await limiter.check("packet-loss-client")).allowed
            _docker("exec", container_name, "apk", "add", "--no-cache", "iproute2")

            async def active_traffic():
                results = []
                for _ in range(30):
                    results.append(await limiter.check("packet-loss-client"))
                    await asyncio.sleep(0.01)
                return results

            traffic = asyncio.create_task(active_traffic())
            await asyncio.sleep(0.05)
            await docker(
                "exec", container_name, "tc", "qdisc", "replace", "dev", "eth0",
                "root", "netem", "loss", "100%",
            )
            await asyncio.sleep(0.3)
            assert hybrid.state in {CircuitState.OPEN, CircuitState.HALF_OPEN}
            assert local.current_size()["token_bucket"] > 0
            await remove_packet_loss()

            for _ in range(100):
                await limiter.check("packet-loss-client")
                if hybrid.state == CircuitState.CLOSED:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Redis did not recover after packet loss was removed")

            results = await traffic
            assert len(results) == 30
            assert all(result.allowed for result in results)
            assert hybrid.state == CircuitState.CLOSED
        finally:
            await remove_packet_loss()
            if "traffic" in locals() and not traffic.done():
                await traffic
            await hybrid.close()
