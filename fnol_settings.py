"""
FNOL Intelligence Platform — Central configuration
==================================================
Single source of truth for all runtime configuration. Replaces ~15 scattered
`os.getenv(...)` calls across api_server, conversational_agent,
total_loss_agent, llm_adapter, sor_adapter, salvage_adapter, and launcher.

Usage:

    from fnol_settings import settings, reload_settings
    print(settings.fnol_api_key)
    print(settings.rate_limit_window_seconds)

The module-level `settings` instance is constructed at import time. Tests can
call `reload_settings()` to pick up env-var changes without re-importing.

Validation:
  • All numeric fields have explicit lower/upper bounds.
  • `fnol_api_key` is validated against a list of known-default sentinels
    and the API server refuses to start when an invalid key is in use.
  • CORS origins are parsed from a comma-separated string into a list.

Production deployment:
  • Supports a `.env` file at the project root (pydantic-settings convention).
  • All settings can be overridden by environment variables — the env name
    is the upper-case version of the attribute name (e.g. FNOL_API_KEY).
"""

from __future__ import annotations

import os
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Known-default API keys that we refuse to accept. Add any value that has
# ever appeared in source, documentation, screenshots, or test fixtures — if
# it has touched the repo, treat it as already compromised. The server's
# `require_valid_api_key()` will refuse to start if `fnol_api_key` matches
# anything in this set.
KNOWN_DEFAULT_API_KEYS = frozenset({
    "fnol-api-key-2026",
    "changeme",
    "test",
    "",
    # Leaked via prior commit to fnol_settings.py default. Rotated; permanently
    # quarantined here so a future contributor can't reintroduce it.
    "wxN-Kgy_-FD2H5n7pqNMxfdg1ETGhzo07M0uwSjAY3E",
})


class Settings(BaseSettings):
    """Process-wide configuration. Fields map 1:1 to env vars with the same
    name in upper case (the leading 'fnol_' prefix is part of the variable
    name, e.g. `FNOL_API_KEY`)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── API surface ─────────────────────────────────────────────────────
    fnol_api_key: str = Field(
        default="",
        description=("Required at startup. Set via FNOL_API_KEY env var or .env. "
                     "The server refuses to start when this is unset or matches a "
                     "known-default sentinel (see KNOWN_DEFAULT_API_KEYS)."),
    )
    fnol_allowed_origins: str = Field(
        default="http://localhost:8000,http://127.0.0.1:8000",
        description="CORS allow-list (comma-separated).",
    )
    fnol_log_level: str = Field(default="INFO")
    fnol_host: str = Field(default="127.0.0.1")
    fnol_port: int = Field(default=8000, ge=1, le=65535)

    # ── In-memory stores (size + TTL) ───────────────────────────────────
    fnol_trace_max: int = Field(default=2048, ge=1)
    fnol_trace_ttl_seconds: int = Field(default=24 * 3600, ge=60)
    fnol_session_max: int = Field(default=2048, ge=1)
    fnol_session_ttl_seconds: int = Field(default=2 * 3600, ge=60)
    fnol_tl_eval_max: int = Field(default=2048, ge=1)
    fnol_tl_eval_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=60)

    # ── State backend (Phase 0 → Phase 1 Redis migration) ───────────────
    # Phase 0: state_backend="local"  → BoundedStore (in-process, no deps).
    # Phase 1: state_backend="redis"  → RedisStateBackend (multi-worker).
    state_backend: str = Field(
        default="local",
        description=(
            "'local' = BoundedStore (POC / single-worker dev). "
            "'redis' = Redis Hash backend (multi-worker production). "
            "See fnol_state_backend.make_store() for the migration guide."
        ),
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description=(
            "Redis connection URL. Only used when state_backend='redis'. "
            "Supports redis://, rediss:// (TLS), and unix:// socket paths."
        ),
    )
    redis_key_prefix: str = Field(
        default="fnol:",
        description=(
            "Namespace prefix for every Redis key (e.g. 'fnol:pipeline_traces:…'). "
            "Change per environment to isolate dev/staging/prod on a shared cluster."
        ),
    )

    # ── Rate limiting ────────────────────────────────────────────────────
    fnol_rate_limit_max: int = Field(default=60, ge=1)
    fnol_rate_limit_window_seconds: int = Field(default=60, ge=1)

    # ── JWT / OAuth2 (inbound API authentication) ────────────────────────
    # Phase 0 (dev):  set JWT_SECRET for HS256 self-signed tokens.
    # Phase 1 (prod): set JWT_JWKS_URL to your IdP's JWKS endpoint (RS256).
    #                 JWT_SECRET is ignored when JWT_JWKS_URL is present.
    jwt_secret: Optional[str] = Field(
        default=None,
        description=(
            "HS256 signing secret for dev-mode JWT issuance and validation. "
            "Generate with: python -c \"import secrets; print(secrets.token_urlsafe(48))\". "
            "Required for dev-mode POST /auth/token. "
            "Leave unset when JWT_JWKS_URL is configured (RS256 / external IdP)."
        ),
    )
    jwt_algorithm: str = Field(
        default="HS256",
        description=(
            "JWT signing algorithm. "
            "'HS256' for dev (uses jwt_secret). "
            "'RS256' for production (requires jwt_jwks_url)."
        ),
    )
    jwt_jwks_url: Optional[str] = Field(
        default=None,
        description=(
            "JWKS endpoint URL for RS256 token validation via external IdP. "
            "Example: 'https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys'. "
            "When set, overrides HS256 jwt_secret path and disables dev issuer."
        ),
    )
    jwt_issuer: Optional[str] = Field(
        default="fnol-dev",
        description=(
            "Expected 'iss' claim in incoming JWTs. "
            "Must match IdP issuer exactly. "
            "Dev default: 'fnol-dev'."
        ),
    )
    jwt_audience: Optional[str] = Field(
        default="fnol-intelligence",
        description=(
            "Expected 'aud' claim in incoming JWTs. "
            "Dev default: 'fnol-intelligence'. "
            "For Azure AD: 'api://<app-id>'."
        ),
    )
    jwt_roles_claim: str = Field(
        default="roles",
        description=(
            "JWT claim key that contains the caller's role list. "
            "Standard: 'roles'. Okta: 'groups'. Azure AD app roles: 'roles'. "
            "Set to match your IdP's token schema."
        ),
    )
    jwt_access_token_expire_minutes: int = Field(
        default=60, ge=1, le=1440,
        description=(
            "Dev-mode token TTL in minutes (1–1440, default 60). "
            "Production tokens use the IdP-configured TTL (this setting ignored)."
        ),
    )
    jwt_dev_issuer_enabled: bool = Field(
        default=True,
        description=(
            "Enable the POST /auth/token dev-fixture endpoint. "
            "Set false in production. "
            "Automatically overridden to false when jwt_jwks_url is configured."
        ),
    )

    # ── Network graph cache TTL ──────────────────────────────────────────
    network_graph_cache_ttl_seconds: int = Field(
        default=1800, ge=60,
        description="Network graph adapter response cache TTL in seconds (default 30 min).",
    )

    # ── LLM provider (selection + credentials) ──────────────────────────
    fnol_llm_provider: str = Field(default="auto",
                                   description="auto | mock | anthropic | openai | azure_openai | bedrock")
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    azure_openai_api_key: Optional[str] = None
    azure_openai_endpoint: Optional[str] = None
    azure_openai_deployment: Optional[str] = None
    azure_openai_api_version: str = "2024-08-01-preview"
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_region: str = "us-east-1"

    # ── SOR adapter ─────────────────────────────────────────────────────
    sor_type: str = Field(default="mock",
                          description="mock | duckcreek | guidewire")

    # ── Salvage adapter ─────────────────────────────────────────────────
    salvage_vendor: str = Field(default="auto",
                                description="auto | copart | iaa | mock")
    copart_api_base_url: Optional[str] = None
    copart_api_key: Optional[str] = None
    iaa_api_base_url: Optional[str] = None
    iaa_api_key: Optional[str] = None

    # ── Verisk ISO ClaimSearch adapter ──────────────────────────────────
    verisk_iso_adapter: str = Field(
        default="auto",
        description="auto | live | mock. 'auto' → live when credentials present, shell otherwise.",
    )
    verisk_iso_api_base_url: Optional[str] = Field(
        default=None,
        description="Verisk ISO ClaimSearch REST v3 base URL. Verisk-supplied during carrier onboarding.",
    )
    verisk_iso_api_key: Optional[str] = Field(
        default=None,
        description="X-Api-Key header value for Verisk ISO REST v3.",
    )
    verisk_iso_cert_path: Optional[str] = Field(
        default=None,
        description="Filesystem path to PEM-format mTLS client certificate. Verisk-issued during onboarding.",
    )
    verisk_iso_key_path: Optional[str] = Field(
        default=None,
        description="Filesystem path to PEM-format mTLS client private key (unencrypted).",
    )
    verisk_iso_ca_path: Optional[str] = Field(
        default=None,
        description="Filesystem path to Verisk CA bundle PEM. When None, system CA store is used (testing only).",
    )
    verisk_iso_timeout_seconds: float = Field(
        default=8.0, gt=0, le=60,
        description="Per-request timeout for Verisk ISO API calls. ISO SLA < 3s; 8s allows for tail latency.",
    )
    iso_cache_ttl_seconds: int = Field(
        default=900, ge=60,
        description="ISO response cache TTL in seconds (default 15 min). ISO is billed per inquiry.",
    )

    # ── HTTP client defaults (for live-mode adapters) ───────────────────
    http_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    http_read_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    http_max_retries: int = Field(default=2, ge=0, le=10)
    http_verify_tls: bool = True

    # ── ACORD Gap 7 — Message Transport & XML Envelope ───────────────────
    # Schema registry: maps transaction type → ACORD version string.
    # Overrides the inline ACORD_SCHEMA_REGISTRY dict in the adapter.
    # Format: comma-separated "TransactionType=major.minor.maint" pairs.
    # Example: "ClaimsOccurrenceRq=2.0.0,ClaimsOccurrenceRs=2.0.0"
    acord_schema_registry_override: Optional[str] = Field(
        default=None,
        description=(
            "ACORD SchemaVersion registry override. Comma-separated "
            "TransactionType=major.minor.maint pairs. "
            "Overrides inline ACORD_SCHEMA_REGISTRY in fnol_acord_xml_adapter. "
            "None = use built-in registry (POC default)."
        ),
    )

    # CarrierCredentials — ACORD XML / EDI outbound auth
    acord_carrier_id: Optional[str] = Field(
        default=None,
        description=(
            "ACORD-assigned carrier/member ID. "
            "Emitted in SignonRq ClientApp/CarrierId element. "
            "Required for state EDI mandate submission."
        ),
    )
    acord_carrier_name: Optional[str] = Field(
        default=None,
        description="Carrier name for ACORD SignonRq ClientApp/Org element.",
    )
    acord_signon_user_id: Optional[str] = Field(
        default=None,
        description=(
            "ACORD SignonRq legacy EDI UserId. "
            "Only used for non-REST EDI VAN / AS2 submissions. "
            "Leave None for REST / OAuth2 / mTLS transport paths."
        ),
    )

    # OAuth2 production path (REST transport — token injected in HTTP header)
    acord_oauth_bearer_token: Optional[str] = Field(
        default=None,
        description=(
            "OAuth 2.0 Bearer token for ACORD XML REST transport. "
            "Injected as Authorization: Bearer header — NEVER in XML body. "
            "None = POC X-API-Key path (not production-ready)."
        ),
    )
    acord_oauth_token_url: Optional[str] = Field(
        default=None,
        description=(
            "OAuth 2.0 token endpoint URL for token refresh. "
            "Used by the adapter when acord_oauth_bearer_token expires. "
            "Carrier-specific — supplied during SOR integration onboarding."
        ),
    )
    acord_oauth_client_id: Optional[str] = Field(
        default=None,
        description="OAuth 2.0 client_id for token refresh flow.",
    )
    acord_oauth_client_secret: Optional[str] = Field(
        default=None,
        description=(
            "OAuth 2.0 client_secret for token refresh. "
            "Load from carrier vault (HashiCorp / AWS Secrets Manager) in production."
        ),
    )

    # mTLS production path — mutual TLS client certificate
    acord_mtls_cert_path: Optional[str] = Field(
        default=None,
        description=(
            "Path to PEM-format client certificate for mTLS ACORD XML transport. "
            "Carrier-issued during SOR integration. "
            "None = mTLS not configured (fallback to OAuth2 or API key)."
        ),
    )
    acord_mtls_key_path: Optional[str] = Field(
        default=None,
        description=(
            "Path to PEM-format client private key for mTLS. "
            "Must match acord_mtls_cert_path. "
            "Never commit to VCS — load from vault or env at runtime."
        ),
    )
    acord_mtls_ca_path: Optional[str] = Field(
        default=None,
        description=(
            "Path to PEM-format CA bundle for mTLS server cert verification. "
            "Carrier-supplied during onboarding. "
            "None = use system default CA store (not for production)."
        ),
    )

    # ── Validators ──────────────────────────────────────────────────────

    @field_validator("fnol_api_key", mode="after")
    @classmethod
    def _strip_api_key(cls, v: str) -> str:
        return (v or "").strip()

    # ── Derived values ──────────────────────────────────────────────────

    @property
    def allowed_origins_list(self) -> List[str]:
        return [o.strip() for o in self.fnol_allowed_origins.split(",") if o.strip()]

    @property
    def api_key_is_default(self) -> bool:
        return self.fnol_api_key in KNOWN_DEFAULT_API_KEYS

    def require_valid_api_key(self) -> None:
        """Raise SystemExit if the API key is unset or matches a known
        default. Call from the API server's startup path so misconfiguration
        fails fast rather than silently exposing the API."""
        if self.api_key_is_default:
            import sys
            msg = (
                "FNOL_API_KEY is not set or matches a known-default value. "
                "Generate a strong key (e.g. `python -c \"import secrets; "
                "print(secrets.token_urlsafe(32))\"`) and export FNOL_API_KEY "
                "before starting the server."
            )
            print(f"FATAL: {msg}", file=sys.stderr)
            raise SystemExit(2)


# Singleton — constructed once at import time
settings: Settings = Settings()


def reload_settings() -> Settings:
    """Re-read environment / .env. Useful in tests that mutate `os.environ`."""
    global settings
    settings = Settings()
    return settings


if __name__ == "__main__":
    # Diagnostic: print the resolved settings with secret-shaped values
    # masked. Useful for `python fnol_settings.py` sanity-checks during
    # deployment.
    SECRET_HINTS = ("key", "secret", "token", "password")
    for k, v in sorted(settings.model_dump().items()):
        masked = (
            "***SET***" if v and any(h in k.lower() for h in SECRET_HINTS) else v
        )
        print(f"{k:36s} = {masked!r}")
