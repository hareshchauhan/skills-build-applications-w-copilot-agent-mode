"""
FNOL Intelligence Platform — Shared HTTP client wrapper
=======================================================
A single configured `httpx.Client` (and `httpx.AsyncClient`) accessor that
every outbound integration (Copart, IAA, Duck Creek, Guidewire, future
DOI/state-API calls) should route through.

Why this exists:
  • TLS verification, timeouts, and retry policy must be set ONCE so a
    later contributor cannot land an integration with `verify=False` or
    a missing timeout — both of which were possible with the old
    "create a client per adapter" pattern.
  • Connection pooling: a process-wide client reuses TCP connections to
    vendor endpoints, materially reducing tail latency under load.

Defaults come from `fnol_settings.settings`. Override per-call by passing
explicit args to `httpx_client(...)` / `httpx_async_client(...)`.

Production hardening still to land (out of scope for the POC):
  • mTLS client certificates per vendor.
  • Per-vendor circuit breakers.
  • Per-vendor retry policies (e.g. honour Retry-After).
  • Telemetry/tracing hooks.
"""

from __future__ import annotations

import threading
from typing import Optional

import httpx

from fnol_settings import settings


# Process-wide singletons. httpx.Client is thread-safe; AsyncClient is task-safe
# within a single event loop.
_SYNC_CLIENT: Optional[httpx.Client] = None
_ASYNC_CLIENT: Optional[httpx.AsyncClient] = None
_LOCK = threading.Lock()


def _build_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.http_connect_timeout_seconds,
        read=settings.http_read_timeout_seconds,
        write=settings.http_read_timeout_seconds,
        pool=settings.http_connect_timeout_seconds,
    )


def _build_transport(async_: bool = False) -> httpx.BaseTransport | httpx.AsyncBaseTransport:
    """HTTP transport with retry-on-connect-error policy. Note: httpx
    retries only handle CONNECTION errors, not 5xx — those are the caller's
    responsibility (typically with backoff + jitter)."""
    kwargs = {"retries": settings.http_max_retries, "verify": settings.http_verify_tls}
    if async_:
        return httpx.AsyncHTTPTransport(**kwargs)
    return httpx.HTTPTransport(**kwargs)


def httpx_client() -> httpx.Client:
    """Return the process-wide sync client. Lazily constructed; safe to
    call from any thread."""
    global _SYNC_CLIENT
    with _LOCK:
        if _SYNC_CLIENT is None:
            _SYNC_CLIENT = httpx.Client(
                timeout=_build_timeout(),
                transport=_build_transport(async_=False),
                headers={"User-Agent": "fnol-intelligence-platform/1.0"},
            )
    return _SYNC_CLIENT


def httpx_async_client() -> httpx.AsyncClient:
    """Return the process-wide async client. Lazily constructed."""
    global _ASYNC_CLIENT
    with _LOCK:
        if _ASYNC_CLIENT is None:
            _ASYNC_CLIENT = httpx.AsyncClient(
                timeout=_build_timeout(),
                transport=_build_transport(async_=True),
                headers={"User-Agent": "fnol-intelligence-platform/1.0"},
            )
    return _ASYNC_CLIENT


def close_clients() -> None:
    """Close pooled HTTP clients. Call from app shutdown hooks."""
    global _SYNC_CLIENT, _ASYNC_CLIENT
    with _LOCK:
        if _SYNC_CLIENT is not None:
            _SYNC_CLIENT.close()
            _SYNC_CLIENT = None
        # AsyncClient.aclose() is the async equivalent — caller must run it
        # in an event loop. Drop the reference here; the caller is
        # responsible for actually closing it when shutting down.
        _ASYNC_CLIENT = None
