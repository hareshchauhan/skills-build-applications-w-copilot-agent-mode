"""
FNOL Intelligence Platform — Shared FastAPI dependencies
========================================================
Auth + rate-limiting + error helpers used by ``fnol_api_server.py`` and every
router under ``agents/``, ``fnol_*_routes.py``, etc.

Lives in its own module so routers can import these without a circular
dependency on ``fnol_api_server.py`` (which itself includes the routers).

Authentication model (Phase 1 — dual-auth)
------------------------------------------
All route files should migrate to ``require_roles()`` from ``fnol_rbac``:

    from fnol_rbac import require_roles, require_roles_rate_limited, Role, CLAIMS_ROLES

    @router.post("/submit")
    async def submit(_, _p: TokenUser = Depends(require_roles(*CLAIMS_ROLES))): ...

The legacy ``require_api_key`` and ``rate_limited`` names are preserved as
thin shims over ``get_current_user()`` (fnol_auth) so that **existing route
files continue to work without any changes** — they now silently accept
JWT Bearer tokens as well as the original X-API-Key.

``_check_api_key()`` is exported for the WebSocket endpoint in
``fnol_langgraph_routes.py``, which cannot use Depends() at the WS layer.

Internal Accenture IP. Not for external distribution without approval.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException

from fnol_auth import TokenUser, get_current_user   # dual-auth (JWT + API key)
from fnol_rbac import _RATE_LIMITER                 # single shared rate-limit bucket
from fnol_settings import settings

log = logging.getLogger("fnol.api")


# ── Legacy X-API-Key validator (used directly by WebSocket auth) ──────────────

def _check_api_key(x_api_key: Optional[str]) -> None:
    """Constant-time X-API-Key validation.

    Used by the WebSocket endpoint in fnol_langgraph_routes.py, which reads
    the key from a query parameter and cannot use FastAPI Depends().
    For all other (HTTP) endpoints, prefer ``get_current_user`` or
    ``require_roles`` so JWT Bearer tokens are also accepted.
    """
    import hmac as _hmac  # noqa: PLC0415
    if not x_api_key or not _hmac.compare_digest(x_api_key.strip(), settings.fnol_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


# ── Backward-compatible shims ──────────────────────────────────────────────────
# These preserve the function names imported by all existing route files.
# They are thin wrappers over get_current_user() — every endpoint that
# previously accepted only X-API-Key now silently accepts JWT Bearer tokens
# as well, with zero change to the route file itself.
#
# New route files SHOULD use require_roles() from fnol_rbac instead.

def require_api_key(user: TokenUser = Depends(get_current_user)) -> TokenUser:
    """FastAPI dependency: authenticate via JWT Bearer or X-API-Key.

    Backward-compatible shim — returns a TokenUser instead of the raw key
    string, but callers that discard the value (``_ = Depends(...)``)
    are unaffected at runtime (Python does not enforce annotation types).

    Prefer ``require_roles()`` from ``fnol_rbac`` for new endpoints so that
    role-based access control is enforced in addition to authentication.
    """
    return user


def rate_limited(user: TokenUser = Depends(get_current_user)) -> TokenUser:
    """FastAPI dependency: authenticate + apply sliding-window rate limit.

    Rate-limit bucket key is the caller's JWT ``sub`` (or ``"api_key_client"``
    for X-API-Key callers).

    Backward-compatible shim. New endpoints should use
    ``require_roles_rate_limited()`` from ``fnol_rbac``.
    """
    decision = _RATE_LIMITER.check(user.sub)
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded — slow down",
            headers={"Retry-After": str(int(decision.retry_after_seconds) + 1)},
        )
    return user


# ── Error helpers (unchanged) ─────────────────────────────────────────────────

def client_error(detail: str, status_code: int = 400) -> HTTPException:
    """Build a client-facing HTTPException with no internal-exception text."""
    return HTTPException(status_code=status_code, detail=detail)


def server_error(log_msg: str, exc: BaseException) -> HTTPException:
    """Log full exception server-side; return an opaque 500 to the client."""
    log.exception("%s: %s", log_msg, exc)
    return HTTPException(status_code=500, detail="Internal server error")
