"""
FNOL Intelligence Platform — StateBackend abstraction
=====================================================
Phase 0:  make_store() returns LocalStateBackend (wraps BoundedStore).
          STATE_BACKEND=local (default). Zero Redis dependency. Zero behaviour
          change. All 20 existing stores work identically.

Phase 1:  Set STATE_BACKEND=redis → make_store() returns RedisStateBackend.
          Requires: pip install redis>=5.0.0 hiredis>=2.0.0
          Each store maps to a Redis Hash namespace with TTL, enabling
          multi-worker deployments and restart durability.

Why a Protocol, not ABC
-----------------------
BoundedStore already satisfies the StateBackend Protocol structurally — no
inheritance changes needed. The Protocol is runtime-checkable so tests can use
isinstance(store, StateBackend) to verify the contract.

Naming convention (the `name` parameter)
-----------------------------------------
`name` becomes the Redis key namespace: fnol:<name>:<key>.
It is also used for log correlation. Use stable snake_case identifiers that
describe the data (not the variable name). Catalogue:

  Store variable            name               Location
  ──────────────────────    ─────────────────  ───────────────────────────────
  _PIPELINE_TRACES          pipeline_traces    fnol_api_server.py
  _IDEMPOTENCY_STORE        idempotency        fnol_api_server.py
  _SESSIONS                 sessions           fnol_conversational_agent.py
  _DECISION_LOG             decision_log       fnol_governance_agent.py
  _BIAS_STORE               bias_store         fnol_governance_agent.py
  _STORE (TL)               tl_evals           fnol_total_loss_agent.py
  _STORE (SIU)              siu_cases          fnol_siu_agent.py
  _ISO_CACHE                iso_cache          fnol_iso_adapter.py
  _CACHE                    network_graph_cache fnol_network_graph_adapter.py
  _DOC_STORE                doc_store          fnol_doc_assist_agent.py
  _TASK_STORE               doc_tasks          fnol_doc_assist_agent.py
  _ALERT_STORE              doc_alerts         fnol_doc_assist_agent.py
  _RESULT_STORE (line)      line_results       fnol_line_creation_agent.py
  _LINE_STORE               line_records       fnol_line_creation_agent.py
  _CLAIM_IDX (line)         line_claim_idx     fnol_line_creation_agent.py
  _RESULT_STORE (vr)        vr_results         fnol_vendor_report_agent.py
  _TRIGGER_STORE            vr_triggers        fnol_vendor_report_agent.py
  _CLAIM_RESULT_INDEX       vr_claim_idx       fnol_vendor_report_agent.py
  _RESULT_STORE (geo)       geo_results        fnol_geo_supplier_agent.py
  _CLAIM_IDX (geo)          geo_claim_idx      fnol_geo_supplier_agent.py

Adding a new store
------------------
  from fnol_state_backend import make_store, StateBackend
  _MY_STORE: StateBackend = make_store("my_store", max_size=2048, ttl_seconds=86400)

Switching to Redis (no code changes required)
---------------------------------------------
  Add to .env or export:
    STATE_BACKEND=redis
    REDIS_URL=redis://localhost:6379/0
    REDIS_KEY_PREFIX=fnol:   # optional, default
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Protocol, runtime_checkable

log = logging.getLogger("fnol.state_backend")


# ── Protocol ──────────────────────────────────────────────────────────────────

@runtime_checkable
class StateBackend(Protocol):
    """Structural interface satisfied by both BoundedStore and RedisStateBackend.

    All methods have identical semantics to BoundedStore — no callers need
    to change for Phase 0.  Phase 1 (Redis) is a factory swap only.
    """

    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...
    def delete(self, key: str) -> bool: ...
    def __contains__(self, key: str) -> bool: ...
    def __len__(self) -> int: ...
    def keys(self) -> Iterable[str]: ...
    def values(self) -> Iterable[Any]: ...
    def items(self) -> Iterable[tuple]: ...


# ── Redis backend (Phase 1) ───────────────────────────────────────────────────

class RedisStateBackend:
    """Redis Hash + TTL backend.  Active when STATE_BACKEND=redis.

    Uses the synchronous redis-py client (redis.Redis).  Redis operations
    are typically 0.1–1 ms — safe to call synchronously from async FastAPI
    route handlers without blocking the event loop meaningfully.  If P99
    Redis latency exceeds ~5 ms, switch to redis.asyncio.Redis and make the
    methods async (requires callers to await).

    Key schema:  {prefix}{name}:{key}
    Example:     fnol:pipeline_traces:FNOL-2026-001234

    Install:
        pip install "redis>=5.0.0" hiredis>=2.0.0
    """

    def __init__(self, name: str, ttl_seconds: int) -> None:
        try:
            import redis as _redis  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "redis-py is not installed. "
                "Run: pip install 'redis>=5.0.0' hiredis>=2.0.0"
            ) from exc

        from fnol_settings import settings  # noqa: PLC0415 — late import avoids circular
        self._ttl  = int(ttl_seconds)
        self._pfx  = f"{settings.redis_key_prefix}{name}:"
        self._r    = _redis.from_url(settings.redis_url, decode_responses=True)
        log.debug("RedisStateBackend(%s) — prefix=%s ttl=%ds", name, self._pfx, ttl_seconds)

    # ── Core interface ────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        raw = self._r.get(f"{self._pfx}{key}")
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw  # stored as plain string (legacy)

    def set(self, key: str, value: Any) -> None:
        serialised = json.dumps(value, default=str)
        self._r.set(f"{self._pfx}{key}", serialised, ex=self._ttl)

    def delete(self, key: str) -> bool:
        return bool(self._r.delete(f"{self._pfx}{key}"))

    def __contains__(self, key: str) -> bool:
        return bool(self._r.exists(f"{self._pfx}{key}"))

    def __len__(self) -> int:
        # KEYS is O(N) — acceptable for ops tooling / health endpoints.
        # High-throughput callers should maintain a counter key instead.
        return len(self._r.keys(f"{self._pfx}*"))

    def keys(self) -> list:
        raw_keys = self._r.keys(f"{self._pfx}*")
        strip = len(self._pfx)
        return [k[strip:] for k in raw_keys]

    def values(self) -> list:
        keys = self._r.keys(f"{self._pfx}*")
        if not keys:
            return []
        raws = self._r.mget(keys)
        out = []
        for raw in raws:
            if raw is not None:
                try:
                    out.append(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    out.append(raw)
        return out

    def items(self) -> list:
        keys = self._r.keys(f"{self._pfx}*")
        if not keys:
            return []
        raws = self._r.mget(keys)
        strip = len(self._pfx)
        result = []
        for k, raw in zip(keys, raws):
            if raw is not None:
                try:
                    v = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    v = raw
                result.append((k[strip:], v))
        return result


# ── Factory ───────────────────────────────────────────────────────────────────

def make_store(name: str, max_size: int, ttl_seconds: int) -> StateBackend:
    """Return the appropriate StateBackend implementation.

    Args:
        name:        Stable identifier for this store — used as Redis key
                     namespace and for log correlation. See naming catalogue
                     at the top of this module.
        max_size:    Maximum entries before LRU eviction (local backend only;
                     Redis has no hard per-namespace size cap — implement a
                     counter key if you need a Redis-side cap).
        ttl_seconds: Entry lifetime.  Both backends honour this.

    Returns:
        BoundedStore  (when STATE_BACKEND=local, default)
        RedisStateBackend  (when STATE_BACKEND=redis)

    Note: BoundedStore satisfies the StateBackend Protocol structurally —
    it is returned directly without wrapping, so there is zero overhead
    compared to the pre-Phase-0 instantiation.
    """
    from fnol_settings import settings  # noqa: PLC0415
    if settings.state_backend == "redis":
        log.info("make_store(%s) → RedisStateBackend", name)
        return RedisStateBackend(name=name, ttl_seconds=ttl_seconds)

    # Default: LocalStateBackend == BoundedStore (satisfies Protocol natively)
    from fnol_runtime import BoundedStore  # noqa: PLC0415
    log.debug("make_store(%s) → BoundedStore(max=%d, ttl=%ds)", name, max_size, ttl_seconds)
    return BoundedStore(max_size=max_size, ttl_seconds=ttl_seconds)
