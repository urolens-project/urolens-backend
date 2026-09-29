"""Suite-wide fixtures.

The login rate limiter (`src.core.rate_limit`) keeps its counts in process
memory, so without a reset one test's login attempts would count against
the next test's — clear it around every test.
"""
from __future__ import annotations

import pytest

from src.core import rate_limit


@pytest.fixture(autouse=True)
def clearLoginRateLimits():
    rate_limit._accountLimiter.clear()
    rate_limit._ipLimiter.clear()
    yield
    rate_limit._accountLimiter.clear()
    rate_limit._ipLimiter.clear()
