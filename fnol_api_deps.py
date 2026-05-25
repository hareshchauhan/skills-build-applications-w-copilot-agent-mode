"""
FNOL Intelligence Platform — Shared FastAPI dependencies
========================================================
Auth + rate-limiting + error helpers used by `fnol_api_server.py` AND every
router under `agents/`, `fnol_*_routes.py`, etc.

Lives in its own module so routers can import these without a circular
dependency on `fnol_api_server.py` (which itself includes the routers).

All routers MUST use `require_api_key` / `rate_limited` from this module —
NEVER re-implement auth in a router, and NEVER read `FNOL_API_KEY` directly
(both are how previous router files reintroduced the hardcoded-default
sentinel that the central settings rejects).
"""

from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException

from fnol_runtime import RateLimiter
from fnol_settings import settings


log = logging.getLogger("fnol.api")


# Process-wide rate limiter. The api_server creates and owns the bucket
# itself in earlier code; this module exposes the same instance so router
# files can apply the dependency without re-creating it.
_RATE_LIMITER = RateLimiter(
    max_requests=settings.fnol_rate_limit_max,
    window_seconds=settings.fnol_rate_limit_window_seconds,
)


def _check_api_key(x_api_key: Optional[str]) -> None:
    """Constant-time validation against the configured key."""
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.fnol_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> str:
    """FastAPI dependency: validates the API key and returns it (the rate-
    limit bucket key). Use on every authenticated endpoint."""
    _check_api_key(x_api_key)
    return x_api_key or ""


def rate_limited(x_api_key: str = Depends(require_api_key)) -> str:
    """FastAPI dependency: per-key sliding-window rate limit. Apply on
    LLM-backed or external-API-calling endpoints to cap provider cost +
    DoS surface if the key leaks."""
    decision = _RATE_LIMITER.check(x_api_key)
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded — slow down",
            headers={"Retry-After": str(int(decision.retry_after_seconds) + 1)},
        )
    return x_api_key


def client_error(detail: str, status_code: int = 400) -> HTTPException:
    """Build a client-facing HTTPException with no internal-exception text."""
    return HTTPException(status_code=status_code, detail=detail)


def server_error(log_msg: str, exc: BaseException) -> HTTPException:
    """Log full exception server-side, return an opaque 500 to the client."""
    log.exception("%s: %s", log_msg, exc)
    return HTTPException(status_code=500, detail="Internal server error")
