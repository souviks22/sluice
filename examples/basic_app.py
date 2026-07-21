"""
Example FastAPI app — sluice rate limiting via Policy.

All rate limit configuration lives in one place: the Policy object.
Route handlers know nothing about rate limiting.
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import FastAPI

from sluice import RateLimitMiddleware, RateLimitPolicy
from sluice.backends import RedisBackend
from sluice.algorithms import SlidingWindowCounter, SlidingWindowLog, TokenBucket

logger    = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


backend = RedisBackend.from_url(REDIS_URL, max_connections=20)

policy = (
    RateLimitPolicy(backend)
    # Auth: exact counting, strict limit. SWLog because ±5% error
    # on a brute-force target is a security issue, not a trade-off.
    .limit("/api/login",  SlidingWindowLog,     limit=5,   window="1m")
    # Expensive endpoint: approximate is fine, memory matters at scale.
    .limit("/api/report", SlidingWindowCounter, limit=10,  window="1m")
    # Everything else: token bucket for smooth throughput.
    .default(TokenBucket, capacity=100, window="1m")
    # Scope counters per (IP, path) so /api/login and /api/data
    # have independent budgets for the same client.
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    await backend.connect()
    if not await backend.ping():
        logger.warning("Redis unreachable at startup — failing open.")
    # Log the active policy at startup so it's visible in deployment logs.
    for row in policy.describe():
        logger.info("sluice: %s", row)
    yield
    await backend.close()

app = FastAPI(title="sluice example", lifespan=lifespan)

app.add_middleware(
    RateLimitMiddleware,
    resolver=policy.resolver(),
    exclude_paths={"/health", "/metrics", "/docs", "/openapi.json", "/redoc"},
)

@app.get("/health")
async def health():
    return {"status": "ok", "redis": "up" if await backend.ping() else "down"}

@app.get("/api/data")
async def get_data():
    return {"data": "here"}

@app.post("/api/login")
async def login():
    return {"token": "example-jwt"}

@app.get("/api/report")
async def generate_report():
    return {"report": {"rows": 1000}}
