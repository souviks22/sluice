# Integration, concurrency, and chaos testing

## Test layers

- Fast unit tests cover domain validation, local fallback, policy,
  middleware, identity, observability, and circuit-breaker state transitions.
- Redis integration tests run as part of the normal pytest suite. When
  `SLUICE_TEST_REDIS_URL` is unset, a module fixture starts a disposable local
  Redis container; the tests use unique key prefixes and do not flush its DB.
- The Redis CI job uses an isolated Redis 7.4 service and a dedicated DB.
- A compatibility matrix runs the FastAPI integration tests on Python 3.11 and
  3.13 against the declared minimum FastAPI/Redis/cachetools versions and the
  latest versions allowed by the package constraints. FastAPI selects its
  compatible Starlette version; Sluice does not pin Starlette independently.

## Atomicity coverage

Real-Redis tests issue 120 concurrent operations against each algorithm and
assert that the configured quota is not exceeded. Weighted tests race
cost-three operations against a limit of ten, then prove a rejected
cost-two operation did not consume the final unit. Independent OS processes
also start together and race admissions through separate Redis clients for
each algorithm, verifying the shared quota remains atomic across workers.

## Redis recovery

Script-cache recovery is tested by issuing an operation, flushing Redis'
script cache, and issuing another operation. RedisBackend must reload the
script after `NOSCRIPT` and retry it. Redis TIME usage is verified by
replacing the application-side clock helper with a function that fails.
Pool exhaustion is exercised against real Redis with a one-connection pool;
the hybrid backend must route that failed admission to local state.
The restart test starts a dedicated disposable Redis container and restarts it
through Docker during paced traffic, then checks that hybrid local fallback
and Lua `NOSCRIPT` recovery restore service. The network partition case
disconnects Redis from its isolated Docker network during paced traffic,
verifies local fallback, then reconnects it and checks circuit recovery. The
Toxiproxy case adds 500 ms of downstream latency, checks local fallback,
removes the toxic, and verifies recovery. The packet-loss case uses `tc netem`
to drop Redis egress packets, checks local fallback, removes the drop rule, and
verifies recovery. These cases provision their own containers and need no
environment-variable opt-ins.

## Hybrid failure modes

Deterministic failure-injection tests cover:
- Redis timeout followed by local fallback for the same operation.
- Open-circuit routing directly to local fallback.
- Half-open recovery probe and circuit closure.
- Local fallback failure propagation (fail closed).

These tests do not emulate production deployment topology. Production chaos
testing should exercise that environment using the actual deployment network
and worker arrangement.

## Running locally

```bash
python -m pytest -q
```

Docker must be available and localhost ports `6379`, `6380`–`6383`, and `8474`
must be free. To use an existing disposable Redis for the standard integration
cases, set `SLUICE_TEST_REDIS_URL`; the fault-injection tests still provision
isolated services. Never point integration tests at production Redis.
