"""
FNOL Intelligence Platform — Role-Based Access Control
=======================================================
Defines the 5 P&C standard roles and dependency factories that enforce them.

Role hierarchy
--------------
  ROLE_ADMIN             Full access: governance, config, all operations.
  ROLE_SUPERVISOR        Claims + HITL override + full list + SIU read.
  ROLE_SIU_INVESTIGATOR  SIU case management + ISO ClaimSearch.
  ROLE_ADJUSTER          Submit claims, view own claims, copilot, doc-assist.
  ROLE_READONLY          GET-only on claims, traces, pipeline status.

Convenience sets (use in require_roles() calls to reduce repetition)
---------------------------------------------------------------------
  CLAIMS_ROLES   — adjuster | supervisor | admin   (submit/view claims)
  READ_ROLES     — adjuster | supervisor | admin | readonly
  SIU_ROLES      — siu_investigator | admin
  SUPERVISOR_UP  — supervisor | admin
  ADMIN_ONLY     — admin

Endpoint → role mapping summary
---------------------------------
  POST /fnol/submit           CLAIMS_ROLES (rate-limited)
  GET  /fnol/claims           READ_ROLES
  GET  /fnol/claims/{id}      READ_ROLES
  POST /fnol/copilot/*        CLAIMS_ROLES (rate-limited)
  GET  /fnol/config           ADMIN_ONLY
  GET  /fnol/health-full      ADMIN_ONLY

  /governance/*               ADMIN_ONLY
  /siu/*                      SIU_ROLES
  /iso/query                  SIU_ROLES (rate-limited)
  /iso/*                      CLAIMS_ROLES | SIU_ROLES
  /v3/claims (submit)         CLAIMS_ROLES (rate-limited)
  /v3/claims (list/get)       SUPERVISOR_UP | READONLY
  /v3/hitl                    SUPERVISOR_UP
  /langgraph/run              CLAIMS_ROLES (rate-limited)
  /langgraph/hitl (resolve)   SUPERVISOR_UP
  /doc-assist/*               CLAIMS_ROLES
  /lines/*                    CLAIMS_ROLES
  /geo/*                      CLAIMS_ROLES | READ_ROLES
  /vendor-reports/*           CLAIMS_ROLES

Usage
-----
  from fnol_rbac import require_roles, require_roles_rate_limited, Role, CLAIMS_ROLES

  @router.post("/submit")
  async def submit(
      payload: ...,
      _: TokenUser = Depends(require_roles(*CLAIMS_ROLES)),
  ): ...

  @router.post("/copilot/chat")
  async def chat(
      req: ...,
      _: TokenUser = Depends(require_roles_rate_limited(*CLAIMS_ROLES)),
  ): ...

Internal Accenture IP. Not for external distribution without approval.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Callable, Set

from fastapi import Depends, HTTPException, status

from fnol_auth import TokenUser, get_current_user
from fnol_runtime import RateLimiter
from fnol_settings import settings

log = logging.getLogger("fnol.rbac")


# ── Role definitions ───────────────────────────────────────────────────────────

class Role(str, Enum):
    """P&C Auto Claims RBAC roles.

    These map to the ``roles`` claim (or configured ``jwt_roles_claim``) in
    JWT tokens issued by the dev fixture or an external IdP.
    """

    ADJUSTER         = "adjuster"
    SUPERVISOR       = "supervisor"
    SIU_INVESTIGATOR = "siu_investigator"
    ADMIN            = "admin"
    READONLY         = "readonly"


# ── Convenience role sets ──────────────────────────────────────────────────────
# Pass these with the splat operator: require_roles(*CLAIMS_ROLES)

CLAIMS_ROLES:   Set[Role] = {Role.ADJUSTER, Role.SUPERVISOR, Role.ADMIN}
READ_ROLES:     Set[Role] = {Role.ADJUSTER, Role.SUPERVISOR, Role.ADMIN, Role.READONLY}
SIU_ROLES:      Set[Role] = {Role.SIU_INVESTIGATOR, Role.ADMIN}
SUPERVISOR_UP:  Set[Role] = {Role.SUPERVISOR, Role.ADMIN}
ADMIN_ONLY:     Set[Role] = {Role.ADMIN}


# ── Shared rate limiter ────────────────────────────────────────────────────────
# Single instance shared with the legacy fnol_api_deps rate limiter.
# Bucket key = caller's JWT `sub` (or "api_key_client" for X-API-Key callers).

_RATE_LIMITER = RateLimiter(
    max_requests=settings.fnol_rate_limit_max,
    window_seconds=settings.fnol_rate_limit_window_seconds,
)


# ── Dependency factories ───────────────────────────────────────────────────────

def require_roles(*roles: Role) -> Callable:
    """Return a FastAPI dependency that enforces role membership.

    The authenticated caller must have AT LEAST ONE of the listed roles.

    API-key callers (M2M, ``auth_method="api_key"``) automatically satisfy
    any role check because the X-API-Key is treated as an admin super-key.

    Args:
        *roles:  Acceptable ``Role`` values (OR semantics).

    Returns:
        An async callable suitable for ``Depends()``.

    Example::

        @router.get("/decisions")
        async def list_decisions(
            _: TokenUser = Depends(require_roles(*ADMIN_ONLY))
        ): ...
    """
    allowed = frozenset(r.value for r in roles)

    async def _check(user: TokenUser = Depends(get_current_user)) -> TokenUser:
        if not allowed.intersection(set(user.roles)):
            log.warning(
                "RBAC denied: sub=%s roles=%s required_any=%s",
                user.sub, user.roles, sorted(allowed),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Access denied. "
                    f"Required (any of): {sorted(allowed)}. "
                    f"Your roles: {sorted(user.roles)}."
                ),
            )
        return user

    # Stable __name__ keeps FastAPI's OpenAPI dependency deduplication correct
    _check.__name__ = f"require_roles_{'_'.join(sorted(allowed))}"
    return _check


def require_roles_rate_limited(*roles: Role) -> Callable:
    """``require_roles()`` with a per-caller sliding-window rate limit.

    Use on LLM-backed or external-API endpoints to cap provider cost and
    limit DoS surface if a credential leaks.  The rate-limit bucket key is
    the caller's JWT ``sub`` (or ``"api_key_client"`` for X-API-Key callers).

    Args:
        *roles:  Same semantics as ``require_roles()``.
    """
    allowed = frozenset(r.value for r in roles)

    async def _check_and_rate(user: TokenUser = Depends(get_current_user)) -> TokenUser:
        # Role check first (cheap — no I/O)
        if not allowed.intersection(set(user.roles)):
            log.warning(
                "RBAC denied (rate-limited path): sub=%s roles=%s required_any=%s",
                user.sub, user.roles, sorted(allowed),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Access denied. "
                    f"Required (any of): {sorted(allowed)}. "
                    f"Your roles: {sorted(user.roles)}."
                ),
            )
        # Rate limit keyed on caller identity
        decision = _RATE_LIMITER.check(user.sub)
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded — slow down.",
                headers={"Retry-After": str(int(decision.retry_after_seconds) + 1)},
            )
        return user

    _check_and_rate.__name__ = f"require_roles_rl_{'_'.join(sorted(allowed))}"
    return _check_and_rate
