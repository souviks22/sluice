# Observability and operational controls

## Instrumentation model

Sluice ships dependency-free event contracts and a bounded in-process
`MetricsCollector`. Wrap any operation backend with `InstrumentedBackend`
to record admission outcomes and latency:

```python
metrics = MetricsCollector()
backend = InstrumentedBackend(redis_backend, metrics)
limiter = TokenBucket(backend, capacity=100, refill_rate=2)
```

Export `metrics.snapshot()` through the application's chosen metrics
integration. The collector does not run a metrics server or impose a
Prometheus/OpenTelemetry dependency. Local fallback eviction totals are
exposed separately through `HybridBackend.local_cache_evictions()`.

## Signals

- Admission outcomes: allowed, limited, and backend error.
- Admission latency: count, total, and maximum by algorithm/backend.
- Hybrid degradation: fallback selection and Redis backend errors.
- Local fallback capacity evictions: query `HybridBackend.local_cache_evictions()`
  to see cumulative early quota resets by algorithm; normal TTL expiry is
  excluded from this count.
- Circuit transitions: closed, open, and half-open states.

HybridBackend accepts an optional synchronous observer. Its default is a
no-op. The observer must be fast and non-blocking; enqueue events for
network exporters rather than performing I/O in the request path.

## Cardinality and privacy

Events intentionally exclude raw identifiers, Redis keys, request paths,
user IDs, API keys, exception messages, and arbitrary labels. Metric
dimensions are normalized to fixed algorithm/backend/outcome vocabularies.
Do not add per-user or per-route labels in an exporter.

## Health and operations

Use backend `ping()` and application health checks for readiness/liveness;
admission metrics are not a substitute for dependency health. Alert on
sustained backend errors, fallback activation, elevated admission latency,
and circuit-open duration. Since local fallback is per-process, its
activation changes cluster-wide enforcement semantics and should be
treated as a degraded operating mode.

## Exporter integration

Implement `RateLimitObserver.on_event(event)` for an existing metrics or
telemetry stack. Keep callbacks non-blocking and isolate exporter failures
from the admission decision. The core library deliberately does not ship
an HTTP `/metrics` endpoint or a vendor-specific SDK integration.
