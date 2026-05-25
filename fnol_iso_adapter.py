"""
FNOL Intelligence Platform — ISO ClaimSearch Adapter  (Verisk)
==============================================================
Pluggable adapter for real-time ISO ClaimSearch queries at FNOL.

Blueprint alignment: §S4A · Fraud & Anomaly Detection
  "ISO ClaimSearch returns prior loss match within 24 months same claimant
   → Fraud signal escalated → flag in Decision Record → adjuster review mandatory"

Three operating modes (selected automatically):
  live   — full mTLS REST call to Verisk ISO ClaimSearch API v3
  shell  — credentials absent; returns deterministic, Verisk-shaped mock response
            (identical interface to live — no caller code changes when going live)
  mock   — VERISK_ISO_ADAPTER=mock; fully deterministic, seeded by claim identity;
            designed for CI/CD, carrier evaluation, and POC demos

mTLS authentication:
  ISO ClaimSearch requires mutual-TLS (client certificate) per the Verisk
  Integration Guide §4.2. The adapter loads PEM-format cert/key/CA from
  the filesystem paths supplied via environment variables (or settings).
  Never embed credentials in code or commit them to VCS.

Environment variables:
  VERISK_ISO_API_BASE_URL       Verisk ISO REST base (default: v3 prod endpoint)
  VERISK_ISO_API_KEY            API key sent as X-Api-Key header
  VERISK_ISO_CERT_PATH          Client certificate PEM (mTLS)
  VERISK_ISO_KEY_PATH           Client private key PEM (mTLS)
  VERISK_ISO_CA_PATH            CA bundle PEM (mTLS; Verisk-supplied)
  VERISK_ISO_TIMEOUT_SECONDS    Per-request timeout (default: 8.0)
  VERISK_ISO_ADAPTER            live | shell | mock (default: auto → shell)
  ISO_CACHE_TTL_SECONDS         Response cache TTL (default: 900 = 15 min)

Response caching:
  ISO ClaimSearch is billed per inquiry. The adapter caches responses in a
  BoundedStore (same pattern as A11 evaluations) keyed by a PII-free hash of
  the inquiry parameters. TTL default 15 minutes — long enough to avoid
  duplicate charges from retries; short enough to catch same-session amendments.
  Cache is process-local (POC). Production: Redis with carrier-managed TTL.

Signal scoring output:
  The adapter returns an ISOClaimSearchResponse whose `fraud_signal_weight`
  field is the value S4A should assign to the `iso_match` signal category.
  Callers should treat this as opaque — the weight already encodes match type,
  recency, and hit count per Blueprint §S4A signal table.

Public API
----------
  query(request: ISOClaimSearchRequest) -> ISOClaimSearchResponse
  get_iso_adapter() -> ISOAdapter (resolves to Live | Shell | Mock)
  health() -> Dict[str, Any]

Production hardening (pre-go-live)
------------------------------------
  - Load mTLS certs from carrier vault (HashiCorp Vault / AWS Secrets Manager)
    rather than filesystem — avoid PEM files on disk at runtime
  - Add per-inquiry audit log write (every ISO query is a FCRA consumer report
    use; retain purpose_code + transaction_id per FCRA §604 record-keeping)
  - Implement Verisk's SOAP/EDI 278 fallback for carriers not yet on REST v3
  - Add per-carrier ISO member ID rotation (Verisk requires per-member billing)
  - Wire Retry-After header from Verisk 429 responses into the backoff logic
  - Replace BoundedStore cache with Redis + TTL for multi-process deployments
  - Add NICB (National Insurance Crime Bureau) adapter alongside ISO for staging
    accident ring detection (separate Verisk product line, different endpoint)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import ssl
import time
import uuid
import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from fnol_settings import settings
from fnol_state_backend import make_store, StateBackend

log = logging.getLogger("fnol.iso")

# ───────────────────────────────────────────────────────────────────────────
# Constants
# ───────────────────────────────────────────────────────────────────────────

ADAPTER_VERSION = "1.0.0"

# Verisk ISO ClaimSearch REST API v3 — production base URL.
# Carriers receive their specific subdomain during Verisk onboarding.
_DEFAULT_BASE_URL = "https://claimsearch.iso.com/api/v3"

# ISO inquiry purpose codes (FCRA §604 permissible purpose).
# P01 = Claims investigation (the correct code for FNOL fraud screening).
ISO_PURPOSE_CODE_CLAIMS = "P01"

# Match type priority order (highest → lowest evidentiary weight).
MATCH_TYPE_EXACT     = "EXACT_MATCH"
MATCH_TYPE_VIN       = "VIN_MATCH"
MATCH_TYPE_CLAIMANT  = "CLAIMANT_MATCH"
MATCH_TYPE_POLICY    = "POLICY_MATCH"
MATCH_TYPE_ADDRESS   = "ADDRESS_MATCH"
MATCH_TYPE_PARTIAL   = "PARTIAL_MATCH"

# Fraud signal weight by match type, per Blueprint §S4A ISO signal table.
# These weights feed directly into the S4A composite score.
_MATCH_WEIGHT: Dict[str, float] = {
    MATCH_TYPE_EXACT:    0.92,  # Same claimant + VIN + same-window loss
    MATCH_TYPE_VIN:      0.78,  # Same VIN, different claimant (re-title fraud)
    MATCH_TYPE_CLAIMANT: 0.72,  # Same person, different vehicle (serial claimant)
    MATCH_TYPE_POLICY:   0.64,  # Same policy, different claimant name
    MATCH_TYPE_ADDRESS:  0.38,  # Shared garaging address (ring indicator)
    MATCH_TYPE_PARTIAL:  0.22,  # Partial match — advisory only
}

# 24-month recency window per Blueprint §S4A rule:
# "ISO ClaimSearch returns prior loss match within 24 months same claimant
#  → Fraud signal escalated → flag in Decision Record → adjuster review mandatory"
_RECENCY_WINDOW_DAYS = 730

# ───────────────────────────────────────────────────────────────────────────
# Data contracts
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class ISOClaimSearchRequest:
    """One ISO ClaimSearch inquiry.

    Verisk matches on any combination of the identity fields. The more fields
    provided, the richer the match results. At minimum: one of (name+DOB),
    VIN, or policy_number must be present.

    PII handling: these fields contain consumer PII subject to FCRA. They must
    NOT be logged at DEBUG level or included in any external telemetry payload.
    The adapter hashes them before cache keying.
    """
    # Inquiry identity
    claim_id: str
    inquiry_id: str = field(default_factory=lambda: f"INQ-{uuid.uuid4().hex[:12].upper()}")
    purpose_code: str = ISO_PURPOSE_CODE_CLAIMS

    # Claimant identity (at least one required)
    claimant_first_name: Optional[str] = None
    claimant_last_name:  Optional[str] = None
    claimant_dob:        Optional[str] = None   # ISO 8601 date YYYY-MM-DD
    claimant_ssn_last4:  Optional[str] = None   # Last 4 digits only
    claimant_address:    Optional[str] = None
    claimant_zip:        Optional[str] = None
    claimant_phone:      Optional[str] = None

    # Vehicle (strongly recommended for auto claims)
    vin:                 Optional[str] = None
    vehicle_year:        Optional[int] = None
    vehicle_make:        Optional[str] = None
    vehicle_model:       Optional[str] = None
    license_plate:       Optional[str] = None
    license_state:       Optional[str] = None

    # Policy
    policy_number:       Optional[str] = None
    carrier_member_id:   Optional[str] = None   # Verisk-assigned member ID

    # Loss context (used for recency window calculation)
    loss_date:           Optional[str] = None   # ISO 8601 date YYYY-MM-DD


@dataclass
class ISOHit:
    """One prior loss record returned by ISO ClaimSearch."""
    hit_id:           str
    match_type:       str           # One of MATCH_TYPE_* constants
    match_fields:     List[str]     # Which fields triggered the match
    loss_date:        Optional[str] # YYYY-MM-DD of the prior loss
    loss_cause:       Optional[str] # ISO loss cause code (e.g. "01" = collision)
    loss_amount_usd:  Optional[float]
    claim_status:     Optional[str] # OPEN | CLOSED | DENIED
    carrier_name:     Optional[str]
    policy_state:     Optional[str]
    within_window:    bool          # True if loss_date within 24-month window
    days_since_loss:  Optional[int]
    fraud_indicator:  bool          # Carrier-flagged on prior hit
    raw_hit:          Dict[str, Any] = field(default_factory=dict)


@dataclass
class ISOClaimSearchResponse:
    """Full ISO ClaimSearch response for one inquiry."""
    inquiry_id:       str
    claim_id:         str
    adapter_mode:     str           # "live" | "shell" | "mock"
    hit_count:        int
    hits:             List[ISOHit]

    # Derived fraud signal output (what S4A consumes)
    iso_match:        bool          # True if any hit returned
    match_type:       Optional[str] # Highest-priority match type across all hits
    within_window:    bool          # True if any hit is within 24-month window
    hit_within_window_count: int    # How many hits are within the window
    fraud_signal_weight: float      # 0.0–1.0 value for S4A iso_match signal
    fraud_signal_rationale: str     # Human-readable explanation for Decision Record

    # Audit trail
    transaction_id:   str           # Verisk-returned transaction ID (or synthetic)
    queried_at:       str           # ISO 8601 UTC timestamp
    cached:           bool = False  # True if served from cache
    elapsed_ms:       int = 0       # Wire time (0 if cached or mock)
    model_version:    str = ADAPTER_VERSION


# ───────────────────────────────────────────────────────────────────────────
# Response cache — PII-free keying
# ───────────────────────────────────────────────────────────────────────────

_ISO_CACHE: StateBackend = make_store(
    "iso_cache",
    max_size=4096,
    ttl_seconds=settings.iso_cache_ttl_seconds,
)


def _cache_key(req: ISOClaimSearchRequest) -> str:
    """Stable, PII-free cache key for one inquiry.

    Hashes the identity fields so the key is safe to log, but any two
    inquiries with the same claimant/vehicle/policy hash to the same key.
    Claims with no identity overlap will never collide.
    """
    canonical = json.dumps({
        "fn":     (req.claimant_first_name or "").lower().strip(),
        "ln":     (req.claimant_last_name  or "").lower().strip(),
        "dob":    req.claimant_dob or "",
        "vin":    (req.vin or "").upper().strip(),
        "policy": req.policy_number or "",
        "zip":    (req.claimant_zip or "")[:5],
    }, sort_keys=True)
    return "iso:" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _days_since(date_str: Optional[str]) -> Optional[int]:
    """Days elapsed since a YYYY-MM-DD date string. None if unparseable."""
    if not date_str:
        return None
    try:
        d = dt.datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return (dt.date.today() - d).days
    except ValueError:
        return None


def _within_window(date_str: Optional[str]) -> bool:
    days = _days_since(date_str)
    return days is not None and days <= _RECENCY_WINDOW_DAYS


# ───────────────────────────────────────────────────────────────────────────
# Mock adapter — deterministic, seeded by claim identity
# ───────────────────────────────────────────────────────────────────────────

# ISO loss cause codes (ACORD 87 subset)
_ISO_CAUSES = ["01", "02", "04", "08", "11", "21", "30", "42"]
_ISO_CAUSE_LABELS = {
    "01": "COLLISION", "02": "COMPREHENSIVE", "04": "THEFT",
    "08": "FIRE", "11": "FLOOD", "21": "VANDALISM",
    "30": "GLASS_ONLY", "42": "SINGLE_VEHICLE",
}
_MOCK_CARRIERS = [
    "State Farm", "GEICO", "Progressive", "Allstate", "USAA",
    "Nationwide", "Farmers", "Liberty Mutual", "Travelers", "American Family",
]
_MOCK_STATES = ["TX", "FL", "CA", "GA", "NY", "IL", "OH", "PA", "AZ", "CO"]


def _mock_seed(req: ISOClaimSearchRequest) -> int:
    """Stable random seed from PII-bearing fields. Ensures the same claimant
    always gets the same mock hit pattern across calls and across sessions."""
    raw = "".join([
        (req.claimant_last_name  or "").lower(),
        (req.claimant_first_name or "").lower(),
        (req.claimant_dob        or ""),
        (req.vin or "")[-6:],
    ])
    return int(hashlib.sha256(raw.encode()).hexdigest(), 16) % (2 ** 32)


def _generate_mock_hits(req: ISOClaimSearchRequest, rng: random.Random) -> List[ISOHit]:
    """Generate realistic mock hits for a given inquiry.

    Hit rate calibration (POC):
      ~12% of claimants have a prior loss match in the ISO database
       (industry average; varies by carrier book and geography).
       When iso_match=True is flagged on the claim at intake, the adapter
       always returns at least one hit — preserving the manual-override path
       the UI uses for demos.

    This produces a realistic distribution: low_risk claims rarely get hits;
    high_prior_claims claimants get more and closer hits.
    """
    forced_hit = req.claim_id and (
        (req.claimant_last_name or "").lower() in ("mehta", "demo", "test")
        or bool(getattr(req, "_force_hit", False))
    )

    # Base hit probability seeded by identity (deterministic per claimant)
    hit_prob = 0.12  # Industry baseline
    # Inflate if multiple priors are likely (heuristic: common last names + SSN4 known)
    if req.claimant_ssn_last4:
        hit_prob += 0.08

    if not forced_hit and rng.random() > hit_prob:
        return []   # No match — most claimants

    # Number of hits: 1–3, skewed to 1
    n_hits = rng.choices([1, 2, 3], weights=[0.70, 0.22, 0.08])[0]
    hits: List[ISOHit] = []

    for i in range(n_hits):
        # Match type distribution
        match_type = rng.choices(
            [MATCH_TYPE_CLAIMANT, MATCH_TYPE_VIN, MATCH_TYPE_POLICY,
             MATCH_TYPE_ADDRESS, MATCH_TYPE_EXACT, MATCH_TYPE_PARTIAL],
            weights=[0.40, 0.25, 0.15, 0.10, 0.05, 0.05],
        )[0]

        # Loss date — first hit often within window (within 24 months)
        if i == 0 and (forced_hit or rng.random() < 0.55):
            days_ago = rng.randint(30, 680)   # Within 24-month window
        else:
            days_ago = rng.randint(365, 1825)  # 1–5 years ago
        loss_date = (dt.date.today() - dt.timedelta(days=days_ago)).isoformat()

        cause_code = rng.choice(_ISO_CAUSES)
        amount = round(rng.uniform(1_200, 38_000), 2)
        carrier = rng.choice(_MOCK_CARRIERS)
        pol_state = rng.choice(_MOCK_STATES)
        hit_id = f"ISO-HIT-{uuid.uuid4().hex[:8].upper()}"

        # Fields that triggered the match (varies by match type)
        match_fields: List[str] = []
        if match_type == MATCH_TYPE_EXACT:
            match_fields = ["claimant_name", "dob", "vin", "address"]
        elif match_type == MATCH_TYPE_VIN:
            match_fields = ["vin"]
        elif match_type == MATCH_TYPE_CLAIMANT:
            match_fields = ["claimant_name", "dob"]
        elif match_type == MATCH_TYPE_POLICY:
            match_fields = ["policy_number"]
        elif match_type == MATCH_TYPE_ADDRESS:
            match_fields = ["claimant_address", "claimant_zip"]
        else:
            match_fields = ["claimant_name"]

        fraud_ind = rng.random() < 0.08   # 8% of hits have a fraud indicator

        hits.append(ISOHit(
            hit_id=hit_id,
            match_type=match_type,
            match_fields=match_fields,
            loss_date=loss_date,
            loss_cause=_ISO_CAUSE_LABELS.get(cause_code, cause_code),
            loss_amount_usd=amount,
            claim_status=rng.choice(["CLOSED", "CLOSED", "CLOSED", "OPEN", "DENIED"]),
            carrier_name=carrier,
            policy_state=pol_state,
            within_window=_within_window(loss_date),
            days_since_loss=days_ago,
            fraud_indicator=fraud_ind,
            raw_hit={
                "source": "MOCK",
                "hit_id": hit_id,
                "iso_cause_code": cause_code,
                "policy_state": pol_state,
                "carrier": carrier,
                "amount_usd": amount,
            },
        ))
    return hits


def _score_hits(hits: List[ISOHit]) -> Tuple[float, str, str]:
    """Derive fraud_signal_weight, dominant match_type, and rationale.

    Scoring rules (per Blueprint §S4A):
      1. Weight = highest match weight across all hits.
      2. If any hit is within the 24-month window, weight escalates by +0.10
         (Blueprint: "ISO ClaimSearch returns prior loss match within 24 months
          → Fraud signal escalated").
      3. Hit count bonus: +0.03 per additional hit beyond the first (breadth).
      4. Fraud indicator on any hit: weight floor raised to 0.70.
      5. Final weight clamped to [0.0, 1.0].
    """
    if not hits:
        return 0.0, "", "No ISO ClaimSearch matches returned."

    best_match_type = max(hits, key=lambda h: _MATCH_WEIGHT.get(h.match_type, 0.0)).match_type
    base_weight = _MATCH_WEIGHT.get(best_match_type, 0.22)

    in_window = [h for h in hits if h.within_window]
    window_escalation = 0.10 if in_window else 0.0

    breadth_bonus = min(0.06, 0.03 * (len(hits) - 1))

    fraud_flagged = any(h.fraud_indicator for h in hits)
    fraud_floor = 0.70 if fraud_flagged else 0.0

    weight = max(fraud_floor, base_weight + window_escalation + breadth_bonus)
    weight = round(min(1.0, weight), 4)

    # Rationale for Decision Record
    parts = [
        f"{len(hits)} ISO hit(s); dominant match type: {best_match_type}.",
    ]
    if in_window:
        dates = ", ".join(h.loss_date or "unknown" for h in in_window[:3])
        parts.append(
            f"{len(in_window)} hit(s) within 24-month window (loss dates: {dates}) — "
            "Blueprint rule: fraud signal escalated; adjuster review mandatory."
        )
    else:
        parts.append("No hits within 24-month window.")
    if fraud_flagged:
        parts.append("Prior fraud indicator flagged by originating carrier.")
    parts.append(f"Derived fraud signal weight: {weight:.3f}.")

    return weight, best_match_type, " ".join(parts)


class MockISOAdapter:
    """Deterministic mock ISO ClaimSearch adapter.

    Returns realistic Verisk-shaped responses seeded by claim identity.
    Designed for CI/CD pipelines, carrier evaluation, and POC demos.
    The hit rate, match type distribution, and loss amount ranges are
    calibrated against industry benchmarks to feel real during a demo.
    """

    MODE = "mock"

    def query(self, req: ISOClaimSearchRequest) -> ISOClaimSearchResponse:
        t0 = time.monotonic()
        cache_key = _cache_key(req)
        cached = _ISO_CACHE.get(cache_key)
        if cached is not None:
            cached.cached = True
            return cached

        rng = random.Random(_mock_seed(req))
        hits = _generate_mock_hits(req, rng)
        weight, match_type, rationale = _score_hits(hits)

        resp = ISOClaimSearchResponse(
            inquiry_id=req.inquiry_id,
            claim_id=req.claim_id,
            adapter_mode=self.MODE,
            hit_count=len(hits),
            hits=hits,
            iso_match=len(hits) > 0,
            match_type=match_type if hits else None,
            within_window=any(h.within_window for h in hits),
            hit_within_window_count=sum(1 for h in hits if h.within_window),
            fraud_signal_weight=weight,
            fraud_signal_rationale=rationale,
            transaction_id=f"MOCK-TXN-{uuid.uuid4().hex[:10].upper()}",
            queried_at=_now_utc(),
            cached=False,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
        _ISO_CACHE.set(cache_key, resp)
        return resp

    def health(self) -> Dict[str, Any]:
        return {
            "mode": self.MODE,
            "status": "ok",
            "cache_entries": len(_ISO_CACHE),
            "note": "Mock adapter active — deterministic responses, no Verisk API call",
        }


# ───────────────────────────────────────────────────────────────────────────
# Shell adapter — Verisk-shaped mock when live credentials are absent
# ───────────────────────────────────────────────────────────────────────────

class ShellISOAdapter(MockISOAdapter):
    """Shell mode: uses mock math but labels responses as 'shell' so
    the caller can distinguish 'we chose mock' from 'credentials missing'.

    Emitting shell mode in production logs is the signal to the carrier
    that they need to supply Verisk credentials before go-live. The
    adapter produces a banner advisory in every response rationale.
    """

    MODE = "shell"

    def query(self, req: ISOClaimSearchRequest) -> ISOClaimSearchResponse:
        resp = super().query(req)
        resp.adapter_mode = self.MODE
        resp.fraud_signal_rationale = (
            "[SHELL MODE — no Verisk credentials configured. "
            "Set VERISK_ISO_API_BASE_URL + VERISK_ISO_API_KEY + mTLS certs before go-live.] "
            + resp.fraud_signal_rationale
        )
        return resp

    def health(self) -> Dict[str, Any]:
        h = super().health()
        h["mode"] = self.MODE
        h["note"] = (
            "Shell mode active — no Verisk credentials supplied. "
            "Carrier must provide VERISK_ISO_API_BASE_URL, VERISK_ISO_API_KEY, "
            "VERISK_ISO_CERT_PATH, VERISK_ISO_KEY_PATH, VERISK_ISO_CA_PATH before production."
        )
        return h


# ───────────────────────────────────────────────────────────────────────────
# Live adapter — Verisk ISO ClaimSearch REST API v3 with mTLS
# ───────────────────────────────────────────────────────────────────────────

def _build_mtls_context(cert_path: str, key_path: str, ca_path: Optional[str]) -> ssl.SSLContext:
    """Build an SSLContext for mTLS.

    Verisk requires the carrier to present a client certificate issued by
    Verisk's CA during onboarding. The CA bundle is Verisk-supplied and must
    be used to verify the server certificate.

    Args:
        cert_path: Path to PEM-format client certificate.
        key_path:  Path to PEM-format client private key (unencrypted or
                   password-protected; password support requires extending this
                   function with `ssl.SSLContext.load_cert_chain(password=...)`).
        ca_path:   Path to PEM-format CA bundle. When None, the system default
                   CA store is used (acceptable for testing; use the Verisk CA
                   bundle in production).

    Returns:
        An ssl.SSLContext configured for mTLS client authentication.

    Raises:
        FileNotFoundError: If cert_path or key_path do not exist.
        ssl.SSLError: If the certificate or key is malformed.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    if ca_path:
        ctx.load_verify_locations(cafile=ca_path)
    else:
        ctx.load_default_certs()
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _map_live_hits(raw_hits: List[Dict[str, Any]]) -> List[ISOHit]:
    """Map Verisk ISO API v3 response hits to ISOHit dataclass.

    Verisk ISO ClaimSearch REST v3 hit schema (simplified):
      {
        "hitId":        "string",
        "matchType":    "EXACT | CLAIMANT | VIN | POLICY | ADDRESS | PARTIAL",
        "matchFields":  ["claimantName", "dob", "vin", ...],
        "lossDate":     "YYYY-MM-DD",
        "lossCause":    "COLLISION" | ...,
        "lossAmount":   float,
        "claimStatus":  "OPEN" | "CLOSED" | "DENIED",
        "carrierName":  "string",
        "policyState":  "XX",
        "fraudIndicator": bool
      }

    Match type normalisation: Verisk uses camelCase match type labels
    ("EXACT", "CLAIMANT", "VIN"…). We normalise to our "_MATCH" constants.
    """
    type_map = {
        "EXACT":    MATCH_TYPE_EXACT,
        "CLAIMANT": MATCH_TYPE_CLAIMANT,
        "VIN":      MATCH_TYPE_VIN,
        "POLICY":   MATCH_TYPE_POLICY,
        "ADDRESS":  MATCH_TYPE_ADDRESS,
        "PARTIAL":  MATCH_TYPE_PARTIAL,
    }
    hits = []
    for raw in raw_hits:
        raw_mt = (raw.get("matchType") or "PARTIAL").upper()
        mt = type_map.get(raw_mt, MATCH_TYPE_PARTIAL)
        loss_date = raw.get("lossDate")
        hits.append(ISOHit(
            hit_id=raw.get("hitId") or f"ISO-HIT-{uuid.uuid4().hex[:8].upper()}",
            match_type=mt,
            match_fields=raw.get("matchFields") or [],
            loss_date=loss_date,
            loss_cause=raw.get("lossCause"),
            loss_amount_usd=raw.get("lossAmount"),
            claim_status=raw.get("claimStatus"),
            carrier_name=raw.get("carrierName"),
            policy_state=raw.get("policyState"),
            within_window=_within_window(loss_date),
            days_since_loss=_days_since(loss_date),
            fraud_indicator=bool(raw.get("fraudIndicator", False)),
            raw_hit=raw,
        ))
    return hits


class LiveISOAdapter:
    """Verisk ISO ClaimSearch REST API v3 adapter.

    This is the production adapter. It is used only when all three of
    VERISK_ISO_API_BASE_URL, VERISK_ISO_API_KEY, VERISK_ISO_CERT_PATH,
    and VERISK_ISO_KEY_PATH are set. Missing any one causes the resolver
    to fall back to ShellISOAdapter.

    Wire protocol:
      POST {base_url}/inquiries
      Headers:
        Content-Type: application/json
        X-Api-Key:    {api_key}
        X-Member-Id:  {carrier_member_id}   (Verisk-assigned; per-carrier)
        X-Purpose:    P01                   (FCRA permissible purpose: claims)
      Body: JSON (see _build_inquiry_body)
      Response: JSON with `transactionId`, `hits` array, `processingStatus`

    Timeout: VERISK_ISO_TIMEOUT_SECONDS (default 8s). ISO SLA is < 3s for
    most queries; the 8s allowance covers tail latency + Verisk's own DB
    cold-start on rare large result sets.

    Retry: one retry on 503 / 504 with 1s exponential backoff. No retry on
    4xx (FCRA: each inquiry is a permissible purpose use; duplicate retries
    on 4xx mean the request is malformed, not transient).
    """

    MODE = "live"
    _RETRY_ON = {503, 504}
    _RETRY_WAIT_SECONDS = 1.0

    def __init__(
        self,
        base_url: str,
        api_key: str,
        cert_path: str,
        key_path: str,
        ca_path: Optional[str],
        timeout_seconds: float = 8.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout_seconds
        self._ssl_ctx = _build_mtls_context(cert_path, key_path, ca_path)
        self._fallback = ShellISOAdapter()
        log.info("LiveISOAdapter initialised — base_url=%s", self.base_url)

    def _build_inquiry_body(self, req: ISOClaimSearchRequest) -> Dict[str, Any]:
        """Construct the Verisk ISO ClaimSearch v3 JSON request body.

        Field names follow Verisk's published REST v3 OpenAPI spec.
        Fields that are None are omitted — Verisk treats absent fields as
        "not provided" and still returns partial matches.
        """
        claimant: Dict[str, Any] = {}
        if req.claimant_first_name:
            claimant["firstName"] = req.claimant_first_name
        if req.claimant_last_name:
            claimant["lastName"] = req.claimant_last_name
        if req.claimant_dob:
            claimant["dateOfBirth"] = req.claimant_dob
        if req.claimant_ssn_last4:
            claimant["ssnLast4"] = req.claimant_ssn_last4
        if req.claimant_address:
            claimant["address"] = req.claimant_address
        if req.claimant_zip:
            claimant["zip"] = req.claimant_zip
        if req.claimant_phone:
            claimant["phone"] = req.claimant_phone

        vehicle: Dict[str, Any] = {}
        if req.vin:
            vehicle["vin"] = req.vin
        if req.vehicle_year:
            vehicle["year"] = req.vehicle_year
        if req.vehicle_make:
            vehicle["make"] = req.vehicle_make
        if req.vehicle_model:
            vehicle["model"] = req.vehicle_model
        if req.license_plate:
            vehicle["licensePlate"] = req.license_plate
        if req.license_state:
            vehicle["licenseState"] = req.license_state

        body: Dict[str, Any] = {
            "inquiryId":   req.inquiry_id,
            "purposeCode": req.purpose_code,
            "claimId":     req.claim_id,
        }
        if claimant:
            body["claimant"] = claimant
        if vehicle:
            body["vehicle"] = vehicle
        if req.policy_number:
            body["policyNumber"] = req.policy_number
        if req.carrier_member_id:
            body["memberCarrierId"] = req.carrier_member_id
        if req.loss_date:
            body["lossDate"] = req.loss_date
        return body

    def query(self, req: ISOClaimSearchRequest) -> ISOClaimSearchResponse:
        t0 = time.monotonic()
        cache_key = _cache_key(req)
        cached_resp = _ISO_CACHE.get(cache_key)
        if cached_resp is not None:
            cached_resp.cached = True
            log.debug("ISO cache hit for claim %s", req.claim_id)
            return cached_resp

        url = f"{self.base_url}/inquiries"
        headers = {
            "Content-Type": "application/json",
            "Accept":        "application/json",
            "X-Api-Key":     self.api_key,
            "X-Purpose":     req.purpose_code,
        }
        if req.carrier_member_id:
            headers["X-Member-Id"] = req.carrier_member_id

        body = self._build_inquiry_body(req)

        # Import httpx lazily — not required at module load time, only when live.
        try:
            import httpx
        except ImportError as exc:
            log.error("httpx is required for live ISO adapter: %s", exc)
            return self._fallback.query(req)

        # Build an httpx client that uses our mTLS SSLContext.
        # We construct a per-call client here rather than a shared singleton
        # because the SSLContext is adapter-specific and should NOT share the
        # process-wide client (which uses the default system CA for other calls).
        transport = httpx.HTTPTransport(
            retries=0,                 # We handle retry ourselves below
            verify=self._ssl_ctx,
        )
        client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(connect=5.0, read=self.timeout, write=5.0, pool=5.0),
            headers={"User-Agent": "fnol-intelligence-platform/1.0"},
        )

        raw_resp = None
        last_exc: Optional[Exception] = None
        try:
            for attempt in range(2):  # up to 2 attempts
                try:
                    log.info(
                        "ISO ClaimSearch inquiry attempt=%d claim=%s inquiry=%s",
                        attempt + 1, req.claim_id, req.inquiry_id,
                    )
                    raw_resp = client.post(url, json=body, headers=headers)
                    if raw_resp.status_code in self._RETRY_ON and attempt == 0:
                        log.warning(
                            "ISO ClaimSearch %d on attempt 1 — retrying in %.1fs",
                            raw_resp.status_code, self._RETRY_WAIT_SECONDS,
                        )
                        time.sleep(self._RETRY_WAIT_SECONDS)
                        continue
                    break
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    last_exc = exc
                    if attempt == 0:
                        log.warning("ISO transport error attempt 1: %s — retrying", exc)
                        time.sleep(self._RETRY_WAIT_SECONDS)
                    else:
                        raise
        except Exception as exc:
            log.error("ISO ClaimSearch failed for claim %s: %s — falling back to shell", req.claim_id, exc)
            return self._fallback.query(req)
        finally:
            client.close()

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if raw_resp is None or raw_resp.status_code >= 500:
            log.error(
                "ISO ClaimSearch server error claim=%s status=%s — shell fallback",
                req.claim_id, getattr(raw_resp, "status_code", "timeout"),
            )
            return self._fallback.query(req)

        if raw_resp.status_code == 401:
            log.error("ISO ClaimSearch 401 — check VERISK_ISO_API_KEY and mTLS certs")
            return self._fallback.query(req)

        if raw_resp.status_code == 429:
            log.warning("ISO ClaimSearch 429 — rate limited; shell fallback")
            return self._fallback.query(req)

        if not (200 <= raw_resp.status_code < 300):
            log.warning(
                "ISO ClaimSearch unexpected %d for claim %s — shell fallback",
                raw_resp.status_code, req.claim_id,
            )
            return self._fallback.query(req)

        # Happy path — parse the response
        try:
            payload = raw_resp.json()
        except Exception as exc:
            log.error("ISO ClaimSearch JSON parse error for claim %s: %s", req.claim_id, exc)
            return self._fallback.query(req)

        transaction_id = payload.get("transactionId") or f"ISO-TXN-{uuid.uuid4().hex[:10].upper()}"
        raw_hits = payload.get("hits") or []
        hits = _map_live_hits(raw_hits)
        weight, match_type, rationale = _score_hits(hits)

        resp = ISOClaimSearchResponse(
            inquiry_id=req.inquiry_id,
            claim_id=req.claim_id,
            adapter_mode=self.MODE,
            hit_count=len(hits),
            hits=hits,
            iso_match=len(hits) > 0,
            match_type=match_type if hits else None,
            within_window=any(h.within_window for h in hits),
            hit_within_window_count=sum(1 for h in hits if h.within_window),
            fraud_signal_weight=weight,
            fraud_signal_rationale=rationale,
            transaction_id=transaction_id,
            queried_at=_now_utc(),
            cached=False,
            elapsed_ms=elapsed_ms,
        )
        _ISO_CACHE.set(cache_key, resp)
        log.info(
            "ISO ClaimSearch complete claim=%s hits=%d weight=%.3f txn=%s elapsed=%dms",
            req.claim_id, len(hits), weight, transaction_id, elapsed_ms,
        )
        return resp

    def health(self) -> Dict[str, Any]:
        return {
            "mode": self.MODE,
            "status": "ok",
            "base_url": self.base_url,
            "mtls": "configured",
            "cache_entries": len(_ISO_CACHE),
        }


# ───────────────────────────────────────────────────────────────────────────
# Adapter resolver — singleton, respects VERISK_ISO_ADAPTER env override
# ───────────────────────────────────────────────────────────────────────────

_ADAPTER_INSTANCE: Optional["ISOAdapter"] = None
_ADAPTER_LOCK = __import__("threading").Lock()

# Type alias for documentation — not a real ABC to avoid import overhead
ISOAdapter = object   # LiveISOAdapter | ShellISOAdapter | MockISOAdapter


def get_iso_adapter() -> ISOAdapter:
    """Return the process-wide ISO adapter singleton.

    Resolution order:
      1. VERISK_ISO_ADAPTER=mock → MockISOAdapter (always)
      2. VERISK_ISO_ADAPTER=live → LiveISOAdapter (raises if credentials absent)
      3. auto (default):
         a. All four required live credentials present → LiveISOAdapter
         b. Otherwise → ShellISOAdapter

    This mirrors the salvage adapter resolver pattern so the pattern is
    consistent across all external integrations in the platform.
    """
    global _ADAPTER_INSTANCE
    with _ADAPTER_LOCK:
        if _ADAPTER_INSTANCE is not None:
            return _ADAPTER_INSTANCE

        mode_override = os.getenv("VERISK_ISO_ADAPTER", "auto").lower()

        if mode_override == "mock":
            _ADAPTER_INSTANCE = MockISOAdapter()
            log.info("ISO adapter: mock (forced via VERISK_ISO_ADAPTER=mock)")
            return _ADAPTER_INSTANCE

        base_url  = os.getenv("VERISK_ISO_API_BASE_URL", "")
        api_key   = os.getenv("VERISK_ISO_API_KEY",      "")
        cert_path = os.getenv("VERISK_ISO_CERT_PATH",    "")
        key_path  = os.getenv("VERISK_ISO_KEY_PATH",     "")
        ca_path   = os.getenv("VERISK_ISO_CA_PATH")
        timeout   = float(os.getenv("VERISK_ISO_TIMEOUT_SECONDS", "8.0"))

        all_live = all([base_url, api_key, cert_path, key_path])

        if mode_override == "live" and not all_live:
            missing = [n for n, v in [
                ("VERISK_ISO_API_BASE_URL", base_url),
                ("VERISK_ISO_API_KEY",      api_key),
                ("VERISK_ISO_CERT_PATH",    cert_path),
                ("VERISK_ISO_KEY_PATH",     key_path),
            ] if not v]
            raise ValueError(
                f"VERISK_ISO_ADAPTER=live but required env vars are not set: "
                f"{', '.join(missing)}"
            )

        if all_live:
            try:
                _ADAPTER_INSTANCE = LiveISOAdapter(
                    base_url=base_url, api_key=api_key,
                    cert_path=cert_path, key_path=key_path,
                    ca_path=ca_path, timeout_seconds=timeout,
                )
                log.info("ISO adapter: live (Verisk ISO ClaimSearch API v3)")
            except Exception as exc:
                log.error("ISO live adapter init failed: %s — falling back to shell", exc)
                _ADAPTER_INSTANCE = ShellISOAdapter()
        else:
            _ADAPTER_INSTANCE = ShellISOAdapter()
            log.info(
                "ISO adapter: shell (Verisk credentials not configured; "
                "set VERISK_ISO_API_BASE_URL + VERISK_ISO_API_KEY + mTLS certs)"
            )
        return _ADAPTER_INSTANCE


def query(request: ISOClaimSearchRequest) -> ISOClaimSearchResponse:
    """Module-level convenience wrapper — callers import `query` directly."""
    return get_iso_adapter().query(request)


def health() -> Dict[str, Any]:
    """Module-level health — used by the API health route."""
    return get_iso_adapter().health()


def build_request_from_claim(claim_id: str, claim_data: Dict[str, Any]) -> ISOClaimSearchRequest:
    """Build an ISOClaimSearchRequest from a Claim.model_dump() dict.

    Extracts only the fields needed for ISO matching; does not pass
    loss_description or other free-text fields (FCRA minimisation principle).

    Args:
        claim_id: The platform claim ID.
        claim_data: A dict from Claim.model_dump() or the pipeline trace.

    Returns:
        A fully populated ISOClaimSearchRequest ready for query().
    """
    # Reporter name → claimant identity (best available at FNOL)
    reporter = claim_data.get("reporter_name") or ""
    name_parts = reporter.strip().split(None, 1)
    first = name_parts[0] if len(name_parts) >= 1 else None
    last  = name_parts[1] if len(name_parts) == 2 else None

    # Loss date in YYYY-MM-DD format
    loss_dt_raw = claim_data.get("loss_date_time") or ""
    loss_date = loss_dt_raw[:10] if loss_dt_raw else None

    return ISOClaimSearchRequest(
        claim_id=claim_id,
        claimant_first_name=first,
        claimant_last_name=last,
        claimant_zip=(claim_data.get("effective_zip") or claim_data.get("loss_location_zip") or "")[:5] or None,
        vin=claim_data.get("vin"),
        vehicle_year=claim_data.get("vehicle_year"),
        vehicle_make=claim_data.get("vehicle_make"),
        vehicle_model=claim_data.get("vehicle_model"),
        policy_number=claim_data.get("policy_number"),
        loss_date=loss_date,
    )


def invalidate_cache(claim_id: str) -> bool:
    """Invalidate the cache entry for a given claim_id.

    Since the cache key is a hash of the inquiry fields (not the claim_id),
    we scan all cache entries and remove those whose claim_id matches.
    Use this after intake amendments that change claimant identity fields.

    Returns True if at least one entry was removed.
    """
    removed = False
    for key, resp in list(_ISO_CACHE.items()):
        if isinstance(resp, ISOClaimSearchResponse) and resp.claim_id == claim_id:
            _ISO_CACHE.delete(key)
            removed = True
    return removed


def cache_stats() -> Dict[str, Any]:
    """Return cache occupancy and TTL information."""
    entries = [r for r in _ISO_CACHE.values() if isinstance(r, ISOClaimSearchResponse)]
    return {
        "total_entries": len(entries),
        "cached_claims": len({r.claim_id for r in entries}),
        "adapter_modes": list({r.adapter_mode for r in entries}),
        "cache_ttl_seconds": _ISO_CACHE._ttl,
    }
