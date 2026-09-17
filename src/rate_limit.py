"""Sliding-window chat send rate limiter."""

from __future__ import annotations

import asyncio
import time
from collections import deque


class ChatRateLimiter:
    """Enforce max N chat sends within a rolling window (default 20 / 30s)."""

    def __init__(self, *, max_messages: int = 20, window_seconds: float = 30.0) -> None:
        self._max = max_messages
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    async def acquire(self, *, now: float | None = None) -> float:
        """Wait until a send slot is available. Returns wait time actually slept."""
        slept = 0.0
        async with self._lock:
            while True:
                clock = time.monotonic() if now is None else now
                self._prune(clock)
                if len(self._timestamps) < self._max:
                    self._timestamps.append(clock)
                    return slept
                wait = self._window - (clock - self._timestamps[0]) + 0.01
                if wait <= 0:
                    continue
                await asyncio.sleep(wait)
                slept += wait
                now = None  # after sleep, use real monotonic clock
