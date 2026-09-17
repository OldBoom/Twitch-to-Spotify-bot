"""Chat rate limiter tests."""

import pytest

from rate_limit import ChatRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_allows_under_cap() -> None:
    limiter = ChatRateLimiter(max_messages=2, window_seconds=30.0)
    assert await limiter.acquire(now=1.0) == 0.0
    assert await limiter.acquire(now=1.1) == 0.0


@pytest.mark.asyncio
async def test_rate_limiter_waits_when_full() -> None:
    limiter = ChatRateLimiter(max_messages=1, window_seconds=0.05)
    await limiter.acquire(now=0.0)
    slept = await limiter.acquire(now=0.0)
    assert slept >= 0.05
