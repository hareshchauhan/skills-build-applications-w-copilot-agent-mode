"""
FNOL Intelligence Platform — JWT Authentication
================================================
Provides inbound authentication for the FNOL API surface:

  • TokenUser / Principal model (Pydantic)
  • get_current_user() FastAPI dependency — dual-auth:
      1. Authorization: Bearer <jwt>  → JWT validation  → TokenUser
      2. X-API-Key: <key>             → HMAC comparison → TokenUser(roles=["admin"])
  • _validate_jwt()  — PyJWT HS256 (dev) or RS256 via JWKS (prod)
  • auth_router      — /auth/token (dev issuer) · /auth/me (whoami)

Production deployment guide
-----------------------------
1. Generate a strong secret:
       python -c "import secrets; print(secrets.token_urlsafe(48))"
   and set JWT_SECRET=<value> in .env (or vault-injected env var).

2. Set JWT_ISSUER and JWT_AUDIENCE to match your IdP or carrier config.

3. For RS256 / external IdP (Okta, Azure AD, Ping):
   - Set JWT_JWKS_URL=https://<idp>/.well-known/jwks.json
   - Set JWT_ALGORITHM=RS256
   - Leave JWT_SECRET unset — JWKS path takes priority.

4. Disable the dev issuer:
       JWT_DEV_ISSUER_ENABLED=false

5. The X-API-Key path remains active for machine-to-machine callers.
   Rotate the key regularly; it grants full admin access.

Dev-mode fixture users (POST /auth/token, dev only)
-----------------------------------------------------
  username        roles                   password
  ─────────────── ──────────────────────── ──────────────────
  adjuster1       [adjuster]               adjuster1
  supervisor1     [supervisor]             supervisor1
  siu1            [siu_investigator]       siu1
  admin           [admin]                  admin
  readonly        [readonly]               readonly

Internal Accenture IP. Not for external distribution without approval.
"""
from __future__ import annotations

import hmac
import logging
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from fnol_settings import settings

log = logging.getLogger("fnol.auth")


# ── Dev-mode user fixtures ─────────────────────────────────────────────────────
# Used ONLY by the /auth/token endpoint when jwt_dev_issuer_enabled=True.
# Never consulted in production.  Override via JWT_JWKS_URL + external IdP.

_DEV_USERS: Dict[str, Dict] = {
    "adjuster1":   {"password": "adjuster1",   "roles": ["adjuster"],          "email": "adjuster1@fnol.dev"},
    "supervisor1": {"password": "supervisor1", "roles": ["supervisor"],         "email": "supervisor1@fnol.dev"},
    "siu1":        {"password": "siu1",        "roles": ["siu_investigator"],   "email": "siu1@fnol.dev"},
    "admin":       {"password": "admin",       "roles": ["admin"],              "email": "admin@fnol.dev"},
    "readonly":    {"password": "readonly",    "roles": ["readonly"],           "email": "readonly@fnol.dev"},
}


# ── Pydantic models ────────────────────────────────────────────────────────────

class TokenUser(BaseModel):
    """Decoded, validated token payload injected into route handlers.

    Constructed from:
      • JWT claims (sub, email, roles, exp, iss, aud)  — auth_method="jwt"
      • X-API-Key validation                            — auth_method="api_key"
    """

    sub: str                        # Subject (username / service-account ID)
    email: Optional[str] = None
    roles: List[str] = []           # e.g. ["adjuster", "supervisor"]
    exp: Optional[int] = None       # Unix timestamp (from JWT exp claim)
    iss: Optional[str] = None       # Issuer
    aud: Optional[str] = None       # Audience
    auth_method: str = "jwt"        # "jwt" | "api_key"

    @property
    def is_api_key_auth(self) -> bool:
        """True when the caller authenticated via X-API-Key (M2M path)."""
        return self.auth_method == "api_key"


class TokenRequest(BaseModel):
    """Body for POST /auth/token (dev-mode password grant)."""
    username: str
    password: str
    grant_type: str = "password"    # Only "password" supported in dev mode


class TokenResponse(BaseModel):
    """OAuth2-compatible token response."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int                 # seconds
    roles: List[str] = []


# ── JWT validation ─────────────────────────────────────────────────────────────

def _load_pyjwt():
    """Lazy import of PyJWT — raises a clear ImportError if not installed."""
    try:
        import jwt as _jwt          # noqa: PLC0415
        return _jwt
    except ImportError as exc:
        raise ImportError(
            "PyJWT is not installed. "
            "Run: pip install 'PyJWT>=2.8.0'"
        ) from exc


def _validate_jwt(token: str) -> TokenUser:
    """Validate a JWT and return a TokenUser.

    Algorithm selection:
      RS256 + JWKS (production):  settings.jwt_jwks_url is set.
      HS256 (dev):                settings.jwt_secret is set.

    Raises:
        HTTPException 401 — invalid / expired / malformed token.
        HTTPException 500 — server not configured (no secret or JWKS URL).
    """
    jwt = _load_pyjwt()

    algorithm = settings.jwt_algorithm
    audience  = settings.jwt_audience or None
    issuer    = settings.jwt_issuer   or None

    try:
        if settings.jwt_jwks_url:
            # ── RS256 / JWKS path (production) ────────────────────────────
            try:
                from jwt import PyJWKClient  # noqa: PLC0415
            except ImportError as exc:
                raise ImportError(
                    "PyJWT>=2.4.0 is required for JWKS support. "
                    "Run: pip install 'PyJWT>=2.8.0'"
                ) from exc
            jwks_client = PyJWKClient(settings.jwt_jwks_url)
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=[algorithm],
                audience=audience,
                issuer=issuer,
            )

        elif settings.jwt_secret:
            # ── HS256 path (dev / self-contained) ─────────────────────────
            payload = jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=[algorithm],
                audience=audience,
                issuer=issuer,
            )

        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "JWT validation is not configured. "
                    "Set JWT_SECRET (dev) or JWT_JWKS_URL (prod) in the environment."
                ),
            )

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired — obtain a new token and retry.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidAudienceError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token audience.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidIssuerError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token issuer.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.DecodeError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is malformed and could not be decoded.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError as exc:
        log.warning("JWT validation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Extract roles from configurable claim name
    roles_claim = settings.jwt_roles_claim
    raw_roles = payload.get(roles_claim, [])
    if isinstance(raw_roles, str):
        raw_roles = [raw_roles]             # single-role string → list

    return TokenUser(
        sub=str(payload.get("sub", "")),
        email=payload.get("email"),
        roles=[r for r in raw_roles if isinstance(r, str)],
        exp=payload.get("exp"),
        iss=payload.get("iss"),
        aud=payload.get("aud") if isinstance(payload.get("aud"), str) else None,
        auth_method="jwt",
    )


# ── FastAPI dependency ─────────────────────────────────────────────────────────

def get_current_user(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_api_key:     Optional[str] = Header(default=None, alias="X-API-Key"),
) -> TokenUser:
    """FastAPI dependency: resolve the caller to a TokenUser.

    Priority:
      1. ``Authorization: Bearer <jwt>``  → JWT validation → TokenUser
      2. ``X-API-Key: <key>``             → HMAC comparison → TokenUser(roles=["admin"])

    The X-API-Key path uses ``hmac.compare_digest`` for constant-time
    comparison to prevent timing-based key enumeration.

    Raises:
        HTTPException 401 — no valid credential supplied.
    """
    # ── Bearer JWT path ────────────────────────────────────────────────────
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
            if token:
                return _validate_jwt(token)

    # ── X-API-Key fallback (M2M / backward-compat) ────────────────────────
    if x_api_key:
        if not hmac.compare_digest(x_api_key.strip(), settings.fnol_api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid X-API-Key.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # API-key callers receive the admin role — they are trusted M2M clients
        return TokenUser(
            sub="api_key_client",
            roles=["admin"],
            auth_method="api_key",
        )

    # ── No credential ──────────────────────────────────────────────────────
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Authentication required. "
            "Provide 'Authorization: Bearer <token>' or 'X-API-Key: <key>'."
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )


# ── Dev-mode token issuer (/auth router) ──────────────────────────────────────

def _issue_dev_jwt(sub: str, roles: List[str], email: Optional[str] = None) -> str:
    """Sign and return a HS256 JWT using the configured jwt_secret."""
    jwt = _load_pyjwt()
    if not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Dev token issuer not configured: set JWT_SECRET in .env. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            ),
        )
    now = int(time.time())
    payload: Dict = {
        "sub": sub,
        "iss": settings.jwt_issuer   or "fnol-dev",
        "aud": settings.jwt_audience or "fnol-intelligence",
        "iat": now,
        "exp": now + settings.jwt_access_token_expire_minutes * 60,
        settings.jwt_roles_claim: roles,
    }
    if email:
        payload["email"] = email
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


auth_router = APIRouter(prefix="/auth", tags=["Authentication"])


@auth_router.post(
    "/token",
    response_model=TokenResponse,
    summary="Issue a dev-mode JWT",
    description=(
        "Password-grant token issuer for local development and testing. "
        "**Disabled in production** (set `JWT_DEV_ISSUER_ENABLED=false` or configure `JWT_JWKS_URL`). "
        "\n\nDev fixture users: `adjuster1`, `supervisor1`, `siu1`, `admin`, `readonly` "
        "(password = username)."
    ),
)
def issue_token(req: TokenRequest) -> TokenResponse:
    """Issue a short-lived JWT for a dev-fixture user.

    Raises:
        403 — dev issuer disabled or production JWKS URL configured.
        401 — unknown username or wrong password.
    """
    if not settings.jwt_dev_issuer_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dev token issuer is disabled in this environment (JWT_DEV_ISSUER_ENABLED=false).",
        )
    if settings.jwt_jwks_url:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Dev token issuer is disabled when JWT_JWKS_URL is configured. "
                "Obtain tokens from your IdP."
            ),
        )

    user = _DEV_USERS.get(req.username)
    if not user or not hmac.compare_digest(req.password, user["password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    token = _issue_dev_jwt(req.username, user["roles"], user.get("email"))
    log.info("Dev JWT issued: sub=%s roles=%s", req.username, user["roles"])

    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
        roles=user["roles"],
    )


@auth_router.get(
    "/me",
    response_model=TokenUser,
    summary="Current user identity",
    description="Returns the resolved identity and roles for the current credential.",
)
def whoami(user: TokenUser = Depends(get_current_user)) -> TokenUser:
    """Return the resolved identity and roles for the current credential."""
    return user


@auth_router.get(
    "/dev-users",
    summary="List dev fixture users",
    description=(
        "Returns the fixture user list for local development. "
        "Only available when `JWT_DEV_ISSUER_ENABLED=true`."
    ),
)
def list_dev_users(user: TokenUser = Depends(get_current_user)) -> dict:
    """Return dev fixture user names and their roles (passwords redacted)."""
    if not settings.jwt_dev_issuer_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dev users endpoint disabled (JWT_DEV_ISSUER_ENABLED=false).",
        )
    return {
        username: {"roles": data["roles"], "email": data.get("email")}
        for username, data in _DEV_USERS.items()
    }
