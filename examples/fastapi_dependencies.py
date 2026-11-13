"""FastAPI route-dependency integration for Sluice."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, APIRouter

from sluice import RateLimitPolicy
from sluice.algorithms import SlidingWindowLog, TokenBucket
from sluice.backends import RedisBackend

backend = RedisBackend.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
policy = (
    RateLimitPolicy(backend)
    .limit("/auth/login", SlidingWindowLog, limit=5, window="1m")
    .default(TokenBucket, capacity=100, window="1m")
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await backend.connect()
    app.state.sluice_backend = backend
    try:
        yield
    finally:
        await backend.close()

app = FastAPI(lifespan=lifespan)
router = APIRouter(dependencies=[Depends(policy.dependency())])

@router.post("/auth/login")
async def login():
    return {"ok": True}

@router.get("/data")
async def data():
    return {"ok": True}

app.include_router(router)
