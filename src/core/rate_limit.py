"""Login rate limiting (UROLENS-222, security audit F-06).

In-process sliding windows, no external store:

- **per account** (username / patient UID): the limit that actually stops
  password guessing — 5 attempts per 5 minutes;
- **per client IP**: a generous backstop against one client spraying many
  accounts — 30 attempts per minute. Generous on purpose: if the app runs
  behind a proxy without `uvicorn --proxy-headers`, every user shares the
  proxy's IP.

Limits are per process: with several workers, each keeps its own counts (the
effective limit multiplies by the worker count). Move the store to Redis if
the API is scaled out.
"""
from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable

from .exceptions import TooManyRequestsException

_ACCOUNT_ATTEMPTS = 5
_ACCOUNT_WINDOW_SECONDS = 300
_IP_ATTEMPTS = 30
_IP_WINDOW_SECONDS = 60
# Hard cap on tracked keys per limiter, so spraying random usernames can't
# grow memory without bound. Past it, expired keys are swept first, then the
# least recently used are dropped.
_MAX_KEYS = 10_000


class SlidingWindowLimiter:
    """Allow at most `limit` hits per key within any `windowSeconds` span."""

    def __init__(
        self,
        limit: int,
        windowSeconds: float,
        clock: Callable[[], float] = time.monotonic,
        maxKeys: int = _MAX_KEYS,
    ) -> None:
        """Build an empty limiter; `clock` and `maxKeys` are injectable for tests."""
        self.limit = limit
        self.windowSeconds = windowSeconds
        self.maxKeys = maxKeys
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> float | None:
        """Record one attempt for `key`, unless it is over the limit.

        A refused attempt isn't recorded, so hammering doesn't push the
        window further out.

        Returns:
            `None` if allowed (and recorded); otherwise the seconds until the
            oldest attempt in the window expires.
        """
        now = self._clock()
        if key not in self._hits and len(self._hits) >= self.maxKeys:
            self._evict(now)
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.windowSeconds:
            hits.popleft()
        if len(hits) >= self.limit:
            return self.windowSeconds - (now - hits[0])
        hits.append(now)
        # Most recently used last, so eviction drops the stalest keys first.
        self._hits[key] = self._hits.pop(key)
        return None

    def _evict(self, now: float) -> None:
        # Drop keys whose whole window has passed; if still full, drop the
        # least recently used half.
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= self.windowSeconds]:
            del self._hits[key]
        if len(self._hits) >= self.maxKeys:
            for key in list(self._hits)[: len(self._hits) // 2]:
                del self._hits[key]

    def reset(self, key: str) -> None:
        """Forget every attempt for `key` (e.g. after a successful login)."""
        self._hits.pop(key, None)

    def clear(self) -> None:
        """Forget every attempt for every key."""
        self._hits.clear()


_accountLimiter = SlidingWindowLimiter(_ACCOUNT_ATTEMPTS, _ACCOUNT_WINDOW_SECONDS)
_ipLimiter = SlidingWindowLimiter(_IP_ATTEMPTS, _IP_WINDOW_SECONDS)


def _accountKey(scope: str, identifier: str) -> str:
    return f"{scope}:{identifier.strip().lower()}"


def enforceLoginRateLimit(scope: str, identifier: str, ipAddress: str) -> None:
    """Count one login attempt; refuse it if the account or IP is over its limit.

    Args:
        scope: which login this is (`"staff"` or `"patient"`), so a username
            and a patient UID never share a counter.
        identifier: the username or patient UID being tried (case- and
            whitespace-insensitive).
        ipAddress: the client's IP.

    Raises:
        TooManyRequestsException: `TOO_MANY_LOGIN_ATTEMPTS` (429, with
            `Retry-After`), if either limit is exceeded.
    """
    for limiter, key in (
        (_ipLimiter, f"ip:{ipAddress}"),
        (_accountLimiter, _accountKey(scope, identifier)),
    ):
        retryAfter = limiter.hit(key)
        if retryAfter is not None:
            raise TooManyRequestsException(retryAfterSeconds=max(1, math.ceil(retryAfter)))


def clearLoginRateLimit(scope: str, identifier: str) -> None:
    """Reset an account's attempt count after it logs in successfully."""
    _accountLimiter.reset(_accountKey(scope, identifier))
