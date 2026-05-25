"""
FNOL Intelligence Platform — Runtime utilities
==============================================
Shared, thread-safe infrastructure used by api_server, conversational_agent,
and total_loss_agent. Keeps these concerns out of business modules.

Contents:
  - BoundedStore : in-memory dict with LRU eviction + TTL expiry. Drop-in for
                   the POC `Dict[str, Any]` stores; production should swap to
                   Redis/DynamoDB behind the same interface.
  - RateLimiter  : per-key sliding-window rate limiter used by the API auth
                   layer to bound LLM cost / quota DoS on a leaked API key.
  - redact_text  : centralised text-PII redaction (phone/email/SSN/VIN) plus a
                   pluggable name-allowlist redactor used before any LLM call.
"""

from __future__ import annotations

import collections
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, Optional


# ───────────────────────────────────────────────────────────────────────────
# BoundedStore — size + TTL eviction (replaces unbounded dict POC stores)
# ───────────────────────────────────────────────────────────────────────────

class BoundedStore:
    """Thread-safe in-memory store with LRU eviction and per-entry TTL.

    Used in place of plain dicts for the POC's pipeline-trace, conversational
    session, and A11 evaluation stores. Without this, those dicts grow
    forever (DoS + GDPR/CCPA retention failure).
    """

    def __init__(self, max_size: int = 1024, ttl_seconds: int = 24 * 3600):
        self._max = int(max_size)
        self._ttl = int(ttl_seconds)
        self._data: "collections.OrderedDict[str, tuple[float, Any]]" = collections.OrderedDict()
        self._lock = threading.Lock()

    def _purge_expired_locked(self) -> None:
        if self._ttl <= 0:
            return
        cutoff = time.time() - self._ttl
        # OrderedDict preserves insertion order; expired entries cluster at head
        # only when TTL governs ordering, but `set` re-inserts at tail, so we
        # iterate snapshot to be safe.
        for key in list(self._data.keys()):
            ts, _ = self._data[key]
            if ts < cutoff:
                del self._data[key]

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._purge_expired_locked()
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (time.time(), value)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._purge_expired_locked()
            entry = self._data.get(key)
            if entry is None:
                return default
            self._data.move_to_end(key)
            return entry[1]

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired_locked()
            return len(self._data)

    def values(self) -> Iterable[Any]:
        with self._lock:
            self._purge_expired_locked()
            return [v for _ts, v in self._data.values()]

    def items(self) -> Iterable[tuple]:
        with self._lock:
            self._purge_expired_locked()
            return [(k, v) for k, (_ts, v) in self._data.items()]


# ───────────────────────────────────────────────────────────────────────────
# RateLimiter — sliding window per key
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: float


class RateLimiter:
    """Per-key sliding-window rate limiter.

    Cheap, in-process, no external dependency. For multi-instance deployments
    swap with a Redis-backed implementation behind the same interface.
    """

    def __init__(self, max_requests: int, window_seconds: int):
        self._max = int(max_requests)
        self._window = int(window_seconds)
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> RateLimitDecision:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            q = self._hits.setdefault(key, collections.deque())
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._max:
                retry_after = max(0.0, (q[0] + self._window) - now)
                return RateLimitDecision(False, 0, retry_after)
            q.append(now)
            return RateLimitDecision(True, self._max - len(q), 0.0)


# ───────────────────────────────────────────────────────────────────────────
# PII redaction — centralised. Replace with carrier service (Presidio,
# Nightfall, etc.) before production.
# ───────────────────────────────────────────────────────────────────────────

_RE_PHONE = re.compile(r"\+?\d[\d\-\(\)\s]{7,}\d")
_RE_EMAIL = re.compile(r"[\w\.\-\+]+@[\w\.\-]+\.\w+")
_RE_SSN   = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_RE_VIN   = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")


def redact_text(text: str, extra_tokens: Optional[Iterable[str]] = None) -> str:
    """Strip phone/email/SSN/VIN, plus any caller-supplied tokens (e.g. an
    insured's name) before sending text to an external LLM provider."""
    if not text:
        return text
    text = _RE_SSN.sub("[REDACTED_SSN]", text)
    text = _RE_EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _RE_PHONE.sub("[REDACTED_PHONE]", text)
    text = _RE_VIN.sub("[REDACTED_VIN]", text)
    if extra_tokens:
        for tok in extra_tokens:
            tok = (tok or "").strip()
            if len(tok) >= 3:
                text = re.sub(re.escape(tok), "[REDACTED_NAME]", text, flags=re.IGNORECASE)
    return text


def redact_claim_dict(claim: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of a claim/captured dict with PII-bearing fields
    replaced by tokens. Use this before serialising a claim into an LLM prompt."""
    if not isinstance(claim, dict):
        return claim
    redacted = dict(claim)
    name_tokens = [v for k, v in claim.items()
                   if isinstance(v, str) and k in ("reporter_name", "named_insured", "insured_name")]
    for k in ("reporter_name", "named_insured", "insured_name"):
        if redacted.get(k):
            redacted[k] = "[REDACTED_NAME]"
    for k in ("reporter_phone", "phone", "callback_number"):
        if redacted.get(k):
            redacted[k] = "[REDACTED_PHONE]"
    for k in ("reporter_email", "email"):
        if redacted.get(k):
            redacted[k] = "[REDACTED_EMAIL]"
    if redacted.get("loss_description"):
        redacted["loss_description"] = redact_text(str(redacted["loss_description"]), name_tokens)
    return redacted
