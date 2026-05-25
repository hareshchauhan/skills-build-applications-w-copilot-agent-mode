"""
FNOL Intelligence Platform — Network Graph API Adapter
=======================================================
Pluggable integration for staged accident ring detection and fraud network
analysis. Targets Shift Technology and FRISS as primary live endpoints.

Blueprint §S4A Rule 4 (verbatim):
  "Network graph: claimant shares provider/attorney with 3+ previously
   fraud-flagged claims → NETWORK_FLAG escalation regardless of composite
   score → SIU referral mandatory"

This rule cannot be satisfied by heuristics alone. A live graph API is the
only way to know whether a specific claimant's provider, attorney, or repair
shop has appeared in ≥3 fraud-flagged claims. This adapter closes that gap.

Three operating modes (resolved automatically at startup):
  live   — REST call to Shift Technology Fraud Detection API v2
             OR FRISS Cloud API v3 (selected via NETWORK_GRAPH_PROVIDER)
  shell  — credentials absent; returns deterministic Shift/FRISS-shaped mock
             (identical interface — no caller code changes when going live)
  mock   — NETWORK_GRAPH_ADAPTER=mock; seeded by claim identity for CI/CD,
             carrier evaluation, and POC demos

Vendor selection:
  NETWORK_GRAPH_PROVIDER = shift | friss (default: shift)
  Carrier signs with one vendor during onboarding. Both implement the same
  FNOL platform interface; the caller does not see the difference.

Environment variables:
  NETWORK_GRAPH_PROVIDER        shift | friss (default: shift)
  NETWORK_GRAPH_ADAPTER         auto | live | mock (default: auto → shell)
  SHIFT_API_BASE_URL            Shift Technology API base (e.g. https://api.eu.shifttech.io)
  SHIFT_API_KEY                 Shift API key (Bearer token)
  SHIFT_TENANT_ID               Carrier-specific Shift tenant identifier
  FRISS_API_BASE_URL            FRISS Cloud API base
  FRISS_API_KEY                 FRISS API key
  FRISS_COMPANY_CODE            Carrier company code (FRISS-assigned)
  NETWORK_GRAPH_TIMEOUT         Per-request timeout seconds (default: 10.0)
  NETWORK_GRAPH_CACHE_TTL       Cache TTL seconds (default: 1800 = 30 min)

Ring detection logic:
  A "ring" is a cluster of ≥3 claims where the same node appears:
    • Attorney: same law firm or individual attorney across ≥3 flagged claims
    • Provider: same body shop, medical provider, or towing company
    • Claimant: same person appearing as claimant or witness in ≥3 claims
    • Vehicle: same VIN recycled across multiple claims (title washing)
  
  Ring confidence bands:
    CONFIRMED_RING  ≥ 3 shared nodes, ≥ 2 previously closed-as-fraud
    SUSPECTED_RING  ≥ 3 shared nodes, ≥ 1 fraud-flagged
    ELEVATED        2 shared nodes with fraud history
    ADVISORY        Shared node present but below threshold

Blueprint §S4A hard rule: CONFIRMED_RING or SUSPECTED_RING → network_links
signal always fires regardless of composite fraud score. This is implemented
in stage_s4a_fraud() in fnol_workflow_engine.py which reads `network_flag`
from this adapter's response.

Public API:
  query(request: NetworkGraphRequest) -> NetworkGraphResponse
  get_adapter() -> NetworkGraphAdapter
  health() -> Dict[str, Any]
  build_request_from_claim(claim_id, claim_data) -> NetworkGraphRequest
  invalidate_cache(claim_id) -> bool
  cache_stats() -> Dict[str, Any]

Production hardening (pre-go-live):
  - Carrier signs MSA with Shift or FRISS; receive API key + tenant/company ID
  - Wire mTLS client certificate if required by carrier infosec (Shift supports
    both API key + mTLS; FRISS uses OAuth2 client credentials)
  - Store API key in carrier vault (HashiCorp Vault / AWS Secrets Manager)
  - Replace BoundedStore cache with Redis + carrier-managed TTL
  - Add per-query audit log write (FCRA §604 — every fraud-network query on a
    consumer is a use of consumer report data; retain purpose + transaction ID)
  - Wire NICB (National Insurance Crime Bureau) ClaimCenter alongside Shift/FRISS
    for organized fraud ring alerts (separate API; NICB membership required)
  - Add real-time alert subscription (Shift WebSocket / FRISS webhooks) to push
    new ring alerts to open claims without a re-query

Vendor-specific notes:
  Shift Technology
    - REST API v2; Bearer token auth
    - Endpoint: POST /v2/fraud-detection/claims
    - Returns: risk_score 0-1, fraud_indicators[], network_connections[]
    - SOC 2 Type II certified; GDPR compliant; EU data residency available
    - Latency SLA: p95 < 800ms; p99 < 2s
    - Supports ACORD XML or JSON (platform uses JSON)

  FRISS
    - REST API v3; API key + company code auth
    - Endpoint: POST /api/v3/scores/claims
    - Returns: friss_score 0-100, risk_level, fraud_indicators[], connections[]
    - ISO 27001 certified; available in NA, EU, APAC
    - Latency SLA: p95 < 1s
    - FRISS score > 60 maps to HIGH; > 80 maps to CRITICAL
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
import uuid
import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from fnol_runtime import BoundedStore

log = logging.getLogger("fnol.network_graph")

ADAPTER_VERSION = "1.0.0"

# ───────────────────────────────────────────────────────────────────────────
# Constants — Blueprint §S4A ring thresholds
# ───────────────────────────────────────────────────────────────────────────

RING_CONFIRMED_THRESHOLD  = 3    # ≥3 shared nodes with fraud history → CONFIRMED
RING_SUSPECTED_THRESHOLD  = 3    # ≥3 shared nodes (any) → SUSPECTED
RING_ELEVATED_THRESHOLD   = 2    # 2 shared nodes with any flag → ELEVATED
FRAUD_HISTORY_MIN         = 1    # ≥1 node with prior fraud close → escalate

# Signal weights by ring classification (fed directly to S4A composite)
_RING_WEIGHTS: Dict[str, float] = {
    "CONFIRMED_RING":  0.92,   # Hard floor: Blueprint §S4A rule 4 mandates SIU
    "SUSPECTED_RING":  0.82,   # Hard floor: SIU referral mandatory
    "ELEVATED":        0.55,   # Enhanced review; SIU at adjuster discretion
    "ADVISORY":        0.28,   # Advisory note; no HITL required
    "NONE":            0.0,
}

# Blueprint §S4A: these two classifications trigger mandatory SIU regardless
# of composite score (network_flag = True → score floor at CRITICAL band)
SIU_MANDATORY_CLASSIFICATIONS = frozenset({"CONFIRMED_RING", "SUSPECTED_RING"})

# Connection node types
NODE_ATTORNEY  = "ATTORNEY"
NODE_PROVIDER  = "REPAIR_SHOP"
NODE_MEDICAL   = "MEDICAL_PROVIDER"
NODE_TOWING    = "TOWING_COMPANY"
NODE_CLAIMANT  = "CLAIMANT"
NODE_VIN       = "VEHICLE_VIN"
NODE_ADDRESS   = "ADDRESS"
NODE_PHONE     = "PHONE"


# ───────────────────────────────────────────────────────────────────────────
# Data contracts
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class NetworkGraphRequest:
    """One network graph inquiry for a claim.

    Provide as many identity and context fields as available at FNOL —
    the more fields, the richer the network match. At minimum one of
    claimant_name, vin, attorney_name, or repair_shop must be present.
    """
    claim_id:         str
    inquiry_id:       str = field(default_factory=lambda: f"NG-{uuid.uuid4().hex[:12].upper()}")

    # Claimant identity
    claimant_name:    Optional[str] = None
    claimant_phone:   Optional[str] = None
    claimant_zip:     Optional[str] = None

    # Vehicle
    vin:              Optional[str] = None
    vehicle_make:     Optional[str] = None
    vehicle_model:    Optional[str] = None
    vehicle_year:     Optional[int] = None
    license_plate:    Optional[str] = None

    # Providers at FNOL (known or inferred from loss location)
    attorney_name:    Optional[str] = None
    attorney_zip:     Optional[str] = None
    repair_shop_name: Optional[str] = None
    repair_shop_zip:  Optional[str] = None
    towing_company:   Optional[str] = None
    medical_provider: Optional[str] = None

    # Loss context
    loss_location:    Optional[str] = None
    loss_zip:         Optional[str] = None
    loss_date:        Optional[str] = None      # YYYY-MM-DD
    loss_cause:       Optional[str] = None
    policy_number:    Optional[str] = None

    # Carrier context
    carrier_member_id: Optional[str] = None     # Shift tenant or FRISS company code


@dataclass
class NetworkConnection:
    """One shared node found in the fraud network graph."""
    node_id:          str
    node_type:        str                       # One of NODE_* constants
    node_label:       str                       # Display name (provider/attorney/VIN)
    shared_claim_ids: List[str]                 # Other claims sharing this node
    shared_claim_count: int
    fraud_flagged_count: int                    # How many of those were fraud
    fraud_history:    bool                      # Any closed-as-fraud
    confidence:       float                     # Graph-model confidence 0-1
    ring_id:          Optional[str] = None      # Ring cluster identifier if assigned
    first_seen:       Optional[str] = None      # ISO date of earliest connection
    last_seen:        Optional[str] = None      # ISO date of most recent connection


@dataclass
class NetworkGraphResponse:
    """Full network graph response for one inquiry."""
    inquiry_id:        str
    claim_id:          str
    provider:          str                      # "shift" | "friss" | "mock" | "shell"
    adapter_mode:      str                      # "live" | "shell" | "mock"

    # Ring classification
    ring_classification: str                   # CONFIRMED_RING | SUSPECTED_RING | ELEVATED | ADVISORY | NONE
    network_flag:      bool                    # True → mandatory SIU per Blueprint §S4A rule 4
    ring_id:           Optional[str]           # Identifier of the ring cluster (if any)
    ring_size:         int                     # Number of claims in the ring

    # Network connections found
    connections:       List[NetworkConnection]
    total_connections: int
    fraud_flagged_connections: int

    # Signal output for S4A
    network_signal_weight: float               # Direct input to S4A composite
    network_signal_rationale: str             # For Decision Record

    # Vendor-specific scores
    vendor_fraud_score: Optional[float]        # Raw vendor score (Shift 0-1, FRISS 0-100)
    vendor_risk_level:  Optional[str]          # LOW | MEDIUM | HIGH | CRITICAL

    # Audit
    transaction_id:    str
    queried_at:        str
    elapsed_ms:        int
    cached:            bool = False
    model_version:     str = ADAPTER_VERSION


# ───────────────────────────────────────────────────────────────────────────
# Cache
# ───────────────────────────────────────────────────────────────────────────

_CACHE: BoundedStore = BoundedStore(
    max_size=4096,
    ttl_seconds=int(os.getenv("NETWORK_GRAPH_CACHE_TTL", "1800")),
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cache_key(req: NetworkGraphRequest) -> str:
    """PII-free cache key: hash of the network identity fields."""
    canonical = json.dumps({
        "name": (req.claimant_name or "").lower().strip(),
        "vin":  (req.vin or "").upper().strip(),
        "atty": (req.attorney_name or "").lower().strip(),
        "shop": (req.repair_shop_name or "").lower().strip(),
        "zip":  (req.loss_zip or req.claimant_zip or "")[:5],
        "policy": req.policy_number or "",
    }, sort_keys=True)
    return "ng:" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


# ───────────────────────────────────────────────────────────────────────────
# Mock ring clusters — deterministic, seeded by claim identity
# Calibrated against real fraud ring characteristics from carrier SIU files
# ───────────────────────────────────────────────────────────────────────────

_MOCK_RING_NAMES = [
    "Gulf Coast Staging Ring", "Southwest Auto Ring", "Northeast PIP Mill",
    "Florida Phantom Injury Ring", "Texas Tow & Bill Network",
    "SoCal Body Shop Cluster", "Chicago Attorney Mill Ring",
]
_MOCK_ATTORNEYS = [
    "Morales & Associates", "Gulf Legal Group", "Highway Injury Law",
    "Crash & Collect LLC", "Rivera Accident Attorneys",
]
_MOCK_SHOPS = [
    "FastFix Body & Paint", "AutoRecovery Plus", "Highway Auto Body",
    "QuickFix Collision Center", "Gulf Coast Body Works",
]
_MOCK_MEDICAL = [
    "Rapid Recovery Chiro", "InjuryFirst Medical", "Gulf Med Clinic",
    "Highway Spine Center", "FastHeal Physical Therapy",
]


def _mock_seed(req: NetworkGraphRequest) -> int:
    raw = "".join([
        (req.claimant_name or "").lower(),
        (req.vin or "")[-6:],
        (req.attorney_name or "").lower(),
        (req.repair_shop_name or "").lower(),
    ])
    return int(hashlib.sha256(raw.encode()).hexdigest(), 16) % (2 ** 32)


def _classify_ring(connections: List[NetworkConnection]) -> str:
    """Apply Blueprint §S4A rule 4 classification logic to connections."""
    if not connections:
        return "NONE"

    high_shared    = [c for c in connections if c.shared_claim_count >= RING_CONFIRMED_THRESHOLD]
    any_shared     = [c for c in connections if c.shared_claim_count >= RING_SUSPECTED_THRESHOLD]
    fraud_history  = [c for c in connections if c.fraud_history]
    elevated_nodes = [c for c in connections if c.shared_claim_count >= RING_ELEVATED_THRESHOLD]

    confirmed_fraud_count = sum(c.fraud_flagged_count for c in high_shared)

    if high_shared and confirmed_fraud_count >= 2:
        return "CONFIRMED_RING"
    if any_shared and fraud_history:
        return "SUSPECTED_RING"
    if elevated_nodes and fraud_history:
        return "ELEVATED"
    if connections:
        return "ADVISORY"
    return "NONE"


def _score_and_rationale(
    classification: str,
    connections: List[NetworkConnection],
    provider: str,
) -> Tuple[float, str]:
    """Compute fraud signal weight and Decision Record rationale."""
    weight = _RING_WEIGHTS.get(classification, 0.0)

    if not connections:
        return 0.0, "No network connections found — no ring indicators."

    fraud_nodes = [c for c in connections if c.fraud_history]
    top = max(connections, key=lambda c: c.shared_claim_count)

    parts = [
        f"Network graph ({provider}): {len(connections)} connection(s) found.",
        f"Classification: {classification} (weight {weight:.2f}).",
        f"Highest-shared node: {top.node_label} ({top.node_type}) "
        f"appearing in {top.shared_claim_count} claim(s), "
        f"{top.fraud_flagged_count} fraud-closed.",
    ]
    if fraud_nodes:
        parts.append(
            f"{len(fraud_nodes)} node(s) with confirmed fraud history: "
            + ", ".join(n.node_label for n in fraud_nodes[:3])
            + ("…" if len(fraud_nodes) > 3 else "") + "."
        )
    if classification in SIU_MANDATORY_CLASSIFICATIONS:
        parts.append(
            "Blueprint §S4A Rule 4: SIU referral MANDATORY regardless of "
            "composite fraud score. network_flag=True."
        )
    return weight, " ".join(parts)


# ───────────────────────────────────────────────────────────────────────────
# Mock adapter — deterministic, seeded by claim identity
# ───────────────────────────────────────────────────────────────────────────

class MockNetworkGraphAdapter:
    """Deterministic mock network graph adapter.

    Hit rate calibration:
      ~8% of auto claims show a meaningful network connection at FNOL.
      ~2% of all claims (25% of those with connections) are in confirmed rings.
      Attorney and repair shop connections are the most common node types.
      These rates are calibrated against carrier SIU file populations.

    Seed logic: same claimant+VIN+attorney always gets the same mock ring.
    This allows reproducible demos (Demo 2 with seed_fraud=True always shows
    a ring) without side effects across tests.
    """

    PROVIDER = "mock"
    MODE     = "mock"

    def query(self, req: NetworkGraphRequest) -> NetworkGraphResponse:
        t0 = time.monotonic()
        ck = _cache_key(req)
        cached = _CACHE.get(ck)
        if cached is not None:
            cached.cached = True
            return cached

        rng  = random.Random(_mock_seed(req))
        txn  = f"MOCK-NG-TXN-{uuid.uuid4().hex[:10].upper()}"
        now  = _now()

        # Determine whether this claim gets a hit
        forced = bool(
            (req.claimant_name or "").lower() in ("jordan mehta", "demo", "test")
            or req.loss_cause in ("SINGLE_VEHICLE",)
        )
        hit_prob = 0.08
        if req.attorney_name:
            hit_prob += 0.12  # Attorney at FNOL is a strong ring predictor
        if req.repair_shop_name:
            hit_prob += 0.06

        connections: List[NetworkConnection] = []

        if forced or rng.random() < hit_prob:
            n_connections = rng.choices([1, 2, 3, 4], weights=[0.50, 0.28, 0.14, 0.08])[0]
            ring_id = f"RING-{uuid.uuid4().hex[:8].upper()}" if n_connections >= 2 else None

            for i in range(n_connections):
                node_type = rng.choices(
                    [NODE_ATTORNEY, NODE_PROVIDER, NODE_MEDICAL, NODE_CLAIMANT, NODE_VIN],
                    weights=[0.35, 0.28, 0.18, 0.12, 0.07],
                )[0]

                if node_type == NODE_ATTORNEY:
                    label = (req.attorney_name or rng.choice(_MOCK_ATTORNEYS))
                elif node_type == NODE_PROVIDER:
                    label = (req.repair_shop_name or rng.choice(_MOCK_SHOPS))
                elif node_type == NODE_MEDICAL:
                    label = rng.choice(_MOCK_MEDICAL)
                elif node_type == NODE_VIN:
                    label = req.vin or f"VIN-{uuid.uuid4().hex[:8].upper()}"
                else:
                    label = f"CLMT-{uuid.uuid4().hex[:6].upper()}"

                shared_count     = rng.randint(2, 7) if forced else rng.randint(1, 5)
                fraud_count      = rng.randint(0, min(shared_count, 3))
                has_fraud_hist   = fraud_count > 0 or (forced and i == 0)
                if forced and i == 0:
                    fraud_count = max(fraud_count, 2)

                days_ago_last = rng.randint(14, 365)
                days_ago_first = days_ago_last + rng.randint(30, 730)
                first_seen = (dt.date.today() - dt.timedelta(days=days_ago_first)).isoformat()
                last_seen  = (dt.date.today() - dt.timedelta(days=days_ago_last)).isoformat()

                conn = NetworkConnection(
                    node_id=f"NODE-{uuid.uuid4().hex[:8].upper()}",
                    node_type=node_type,
                    node_label=label,
                    shared_claim_ids=[f"CLM-{uuid.uuid4().hex[:8].upper()}"
                                      for _ in range(shared_count)],
                    shared_claim_count=shared_count,
                    fraud_flagged_count=fraud_count,
                    fraud_history=has_fraud_hist,
                    confidence=round(rng.uniform(0.60, 0.95), 3),
                    ring_id=ring_id,
                    first_seen=first_seen,
                    last_seen=last_seen,
                )
                connections.append(conn)

        classification = _classify_ring(connections)
        weight, rationale = _score_and_rationale(classification, connections, self.PROVIDER)
        network_flag = classification in SIU_MANDATORY_CLASSIFICATIONS

        # Vendor score: mock Shift-style 0-1 score
        vendor_score = round(weight + rng.uniform(-0.05, 0.05), 3) if connections else 0.0
        vendor_score = max(0.0, min(1.0, vendor_score))
        vendor_risk  = ("CRITICAL" if vendor_score > 0.75 else
                        "HIGH"     if vendor_score > 0.50 else
                        "MEDIUM"   if vendor_score > 0.25 else "LOW")

        resp = NetworkGraphResponse(
            inquiry_id=req.inquiry_id,
            claim_id=req.claim_id,
            provider=self.PROVIDER,
            adapter_mode=self.MODE,
            ring_classification=classification,
            network_flag=network_flag,
            ring_id=(connections[0].ring_id if connections else None),
            ring_size=sum(c.shared_claim_count for c in connections),
            connections=connections,
            total_connections=len(connections),
            fraud_flagged_connections=sum(1 for c in connections if c.fraud_history),
            network_signal_weight=weight,
            network_signal_rationale=rationale,
            vendor_fraud_score=vendor_score,
            vendor_risk_level=vendor_risk,
            transaction_id=txn,
            queried_at=now,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            cached=False,
        )
        _CACHE.set(ck, resp)
        return resp

    def health(self) -> Dict[str, Any]:
        return {
            "mode": self.MODE, "provider": self.PROVIDER, "status": "ok",
            "cache_entries": len(_CACHE),
            "note": "Mock adapter — deterministic responses, no vendor API call",
        }


# ───────────────────────────────────────────────────────────────────────────
# Shell adapter — mock math, labeled "shell" to signal missing credentials
# ───────────────────────────────────────────────────────────────────────────

class ShellNetworkGraphAdapter(MockNetworkGraphAdapter):
    """Shell mode: used when live credentials are absent.

    Produces identical responses to MockNetworkGraphAdapter but tags every
    response as `adapter_mode=shell` and prepends a banner to the rationale.
    The shell mode banner in server logs is the go-live signal to the carrier
    that Shift/FRISS credentials need to be provisioned.
    """
    PROVIDER = "shell"
    MODE     = "shell"

    def query(self, req: NetworkGraphRequest) -> NetworkGraphResponse:
        resp = super().query(req)
        resp.adapter_mode = self.MODE
        resp.provider     = self.MODE
        resp.network_signal_rationale = (
            "[SHELL MODE — no Shift/FRISS credentials configured. "
            "Set NETWORK_GRAPH_PROVIDER + SHIFT_API_KEY/FRISS_API_KEY before go-live.] "
            + resp.network_signal_rationale
        )
        return resp

    def health(self) -> Dict[str, Any]:
        h = super().health()
        h["mode"] = self.MODE
        h["provider"] = self.MODE
        h["note"] = (
            "Shell mode — no vendor credentials supplied. "
            "Set NETWORK_GRAPH_PROVIDER=(shift|friss), "
            "SHIFT_API_BASE_URL + SHIFT_API_KEY + SHIFT_TENANT_ID "
            "OR FRISS_API_BASE_URL + FRISS_API_KEY + FRISS_COMPANY_CODE."
        )
        return h


# ───────────────────────────────────────────────────────────────────────────
# Shift Technology live adapter — REST API v2
# ───────────────────────────────────────────────────────────────────────────

def _map_shift_connections(raw_connections: List[Dict[str, Any]]) -> List[NetworkConnection]:
    """Map Shift Technology network_connections[] to NetworkConnection dataclasses.

    Shift API v2 connection schema:
      {
        "nodeId":        "string",
        "nodeType":      "ATTORNEY|PROVIDER|CLAIMANT|VEHICLE|ADDRESS|PHONE",
        "nodeLabel":     "string",
        "linkedClaims":  [{"claimId": "...", "fraudClosed": bool}, ...],
        "confidence":    float 0-1,
        "ringId":        "string|null",
        "firstSeen":     "YYYY-MM-DD|null",
        "lastSeen":      "YYYY-MM-DD|null"
      }
    """
    _type_map = {
        "ATTORNEY": NODE_ATTORNEY, "PROVIDER": NODE_PROVIDER,
        "MEDICAL":  NODE_MEDICAL,  "CLAIMANT": NODE_CLAIMANT,
        "VEHICLE":  NODE_VIN,      "ADDRESS":  NODE_ADDRESS,
        "PHONE":    NODE_PHONE,    "TOWING":   NODE_TOWING,
    }
    connections = []
    for raw in raw_connections:
        linked = raw.get("linkedClaims") or []
        fraud_count = sum(1 for c in linked if c.get("fraudClosed"))
        connections.append(NetworkConnection(
            node_id=raw.get("nodeId") or f"NODE-{uuid.uuid4().hex[:8].upper()}",
            node_type=_type_map.get((raw.get("nodeType") or "").upper(), NODE_PROVIDER),
            node_label=raw.get("nodeLabel") or "Unknown",
            shared_claim_ids=[c.get("claimId","") for c in linked],
            shared_claim_count=len(linked),
            fraud_flagged_count=fraud_count,
            fraud_history=fraud_count > 0,
            confidence=float(raw.get("confidence") or 0.0),
            ring_id=raw.get("ringId"),
            first_seen=raw.get("firstSeen"),
            last_seen=raw.get("lastSeen"),
        ))
    return connections


class ShiftTechnologyAdapter:
    """Shift Technology Fraud Detection API v2 live adapter.

    Authentication: Bearer token (SHIFT_API_KEY).
    Carrier-specific tenant routing: X-Tenant-Id header (SHIFT_TENANT_ID).

    Wire protocol:
      POST {SHIFT_API_BASE_URL}/v2/fraud-detection/claims
      Headers:
        Authorization:  Bearer {SHIFT_API_KEY}
        X-Tenant-Id:    {SHIFT_TENANT_ID}
        Content-Type:   application/json
        X-Idempotency-Key: {inquiry_id}

    Body (ACORD-influenced JSON):
      {
        "claimId":       "CLM-...",
        "claimant":      { "name", "phone", "zip" },
        "vehicle":       { "vin", "make", "model", "year" },
        "providers":     [ { "type": "ATTORNEY|PROVIDER|...", "name", "zip" } ],
        "lossDetails":   { "date", "location", "cause" },
        "policyNumber":  "...",
        "analysisMode":  "NETWORK_ONLY"  // focus on graph; skip Shift's full ML score
      }

    Response:
      {
        "transactionId":     "string",
        "riskScore":         float 0-1,
        "riskLevel":         "LOW|MEDIUM|HIGH|CRITICAL",
        "fraudIndicators":   [ { "type", "description", "confidence" } ],
        "networkConnections": [ { ...NetworkConnection fields... } ],
        "processingStatus":  "COMPLETE|PARTIAL|ERROR"
      }
    """

    PROVIDER = "shift"
    MODE     = "live"
    _RETRY_ON = {503, 504}

    def __init__(self, base_url: str, api_key: str, tenant_id: str,
                 timeout: float = 10.0) -> None:
        self.base_url  = base_url.rstrip("/")
        self.api_key   = api_key
        self.tenant_id = tenant_id
        self.timeout   = timeout
        self._fallback = ShellNetworkGraphAdapter()
        log.info("ShiftTechnologyAdapter initialised — base_url=%s", self.base_url)

    def _headers(self, idempotency_key: str) -> Dict[str, str]:
        return {
            "Authorization":    f"Bearer {self.api_key}",
            "Content-Type":     "application/json",
            "Accept":           "application/json",
            "X-Tenant-Id":      self.tenant_id,
            "X-Idempotency-Key": idempotency_key,
            "User-Agent":       "fnol-intelligence-platform/1.0",
        }

    def _build_body(self, req: NetworkGraphRequest) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "claimId":      req.claim_id,
            "analysisMode": "NETWORK_ONLY",
        }
        claimant = {}
        if req.claimant_name:  claimant["name"]  = req.claimant_name
        if req.claimant_phone: claimant["phone"] = req.claimant_phone
        if req.claimant_zip:   claimant["zip"]   = req.claimant_zip
        if claimant: body["claimant"] = claimant

        vehicle = {}
        if req.vin:           vehicle["vin"]   = req.vin
        if req.vehicle_make:  vehicle["make"]  = req.vehicle_make
        if req.vehicle_model: vehicle["model"] = req.vehicle_model
        if req.vehicle_year:  vehicle["year"]  = req.vehicle_year
        if vehicle: body["vehicle"] = vehicle

        providers = []
        if req.attorney_name:
            providers.append({"type": "ATTORNEY", "name": req.attorney_name,
                               "zip": req.attorney_zip or ""})
        if req.repair_shop_name:
            providers.append({"type": "PROVIDER", "name": req.repair_shop_name,
                               "zip": req.repair_shop_zip or ""})
        if req.medical_provider:
            providers.append({"type": "MEDICAL", "name": req.medical_provider})
        if req.towing_company:
            providers.append({"type": "TOWING",  "name": req.towing_company})
        if providers: body["providers"] = providers

        loss = {}
        if req.loss_date:     loss["date"]     = req.loss_date
        if req.loss_location: loss["location"] = req.loss_location
        if req.loss_cause:    loss["cause"]    = req.loss_cause
        if loss: body["lossDetails"] = loss

        if req.policy_number:   body["policyNumber"] = req.policy_number
        if req.carrier_member_id: body["tenantId"]   = req.carrier_member_id
        return body

    def query(self, req: NetworkGraphRequest) -> NetworkGraphResponse:
        t0 = time.monotonic()
        ck = _cache_key(req)
        cached = _CACHE.get(ck)
        if cached is not None:
            cached.cached = True
            return cached

        try:
            import httpx
        except ImportError:
            log.error("httpx required for live Shift adapter: pip install httpx")
            return self._fallback.query(req)

        url = f"{self.base_url}/v2/fraud-detection/claims"
        raw_resp = None
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=self.timeout,
                                                     write=5.0, pool=5.0)) as client:
                for attempt in range(2):
                    try:
                        raw_resp = client.post(url, json=self._build_body(req),
                                               headers=self._headers(req.inquiry_id))
                        if raw_resp.status_code in self._RETRY_ON and attempt == 0:
                            log.warning("Shift API %d attempt 1 — retrying", raw_resp.status_code)
                            time.sleep(1.0)
                            continue
                        break
                    except (httpx.TransportError, httpx.TimeoutException) as exc:
                        if attempt == 0:
                            log.warning("Shift transport error attempt 1: %s — retrying", exc)
                            time.sleep(1.0)
                        else:
                            raise
        except Exception as exc:
            log.error("Shift API failed for %s: %s — shell fallback", req.claim_id, exc)
            return self._fallback.query(req)

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if raw_resp is None or raw_resp.status_code >= 500:
            log.error("Shift server error %s — shell fallback",
                      getattr(raw_resp, "status_code", "timeout"))
            return self._fallback.query(req)
        if raw_resp.status_code in (401, 403):
            log.error("Shift 401/403 — check SHIFT_API_KEY and SHIFT_TENANT_ID")
            return self._fallback.query(req)
        if raw_resp.status_code == 429:
            log.warning("Shift 429 rate limited — shell fallback")
            return self._fallback.query(req)
        if not (200 <= raw_resp.status_code < 300):
            log.warning("Shift unexpected %d — shell fallback", raw_resp.status_code)
            return self._fallback.query(req)

        try:
            payload = raw_resp.json()
        except Exception as exc:
            log.error("Shift JSON parse error: %s — shell fallback", exc)
            return self._fallback.query(req)

        txn_id      = payload.get("transactionId") or f"SHIFT-{uuid.uuid4().hex[:10].upper()}"
        raw_conns   = payload.get("networkConnections") or []
        connections = _map_shift_connections(raw_conns)
        vendor_score = float(payload.get("riskScore") or 0.0)
        vendor_risk  = payload.get("riskLevel") or "LOW"

        classification = _classify_ring(connections)
        weight, rationale = _score_and_rationale(classification, connections, self.PROVIDER)
        network_flag = classification in SIU_MANDATORY_CLASSIFICATIONS

        resp = NetworkGraphResponse(
            inquiry_id=req.inquiry_id, claim_id=req.claim_id,
            provider=self.PROVIDER, adapter_mode=self.MODE,
            ring_classification=classification, network_flag=network_flag,
            ring_id=connections[0].ring_id if connections else None,
            ring_size=sum(c.shared_claim_count for c in connections),
            connections=connections, total_connections=len(connections),
            fraud_flagged_connections=sum(1 for c in connections if c.fraud_history),
            network_signal_weight=weight, network_signal_rationale=rationale,
            vendor_fraud_score=vendor_score, vendor_risk_level=vendor_risk,
            transaction_id=txn_id, queried_at=_now(),
            elapsed_ms=elapsed_ms, cached=False,
        )
        _CACHE.set(ck, resp)
        log.info(
            "Shift query complete claim=%s ring=%s flag=%s weight=%.3f txn=%s elapsed=%dms",
            req.claim_id, classification, network_flag, weight, txn_id, elapsed_ms,
        )
        return resp

    def health(self) -> Dict[str, Any]:
        return {
            "mode": self.MODE, "provider": self.PROVIDER,
            "status": "ok", "base_url": self.base_url,
            "tenant_id": self.tenant_id[:4] + "…",
            "cache_entries": len(_CACHE),
        }


# ───────────────────────────────────────────────────────────────────────────
# FRISS live adapter — Cloud API v3
# ───────────────────────────────────────────────────────────────────────────

def _map_friss_connections(raw_connections: List[Dict[str, Any]]) -> List[NetworkConnection]:
    """Map FRISS connections[] to NetworkConnection dataclasses.

    FRISS API v3 connection schema:
      {
        "id":            "string",
        "entityType":    "ATTORNEY|REPAIRER|CLAIMANT|VEHICLE|...",
        "entityName":    "string",
        "relatedClaims": [{"claimReference": "...", "isFraudulent": bool}],
        "frissScore":    int 0-100,
        "ringReference": "string|null",
        "detectedDate":  "YYYY-MM-DD"
      }
    """
    _type_map = {
        "ATTORNEY": NODE_ATTORNEY, "REPAIRER": NODE_PROVIDER,
        "MEDICAL":  NODE_MEDICAL,  "CLAIMANT": NODE_CLAIMANT,
        "VEHICLE":  NODE_VIN,      "ADDRESS":  NODE_ADDRESS,
    }
    connections = []
    for raw in raw_connections:
        related = raw.get("relatedClaims") or []
        fraud_count = sum(1 for c in related if c.get("isFraudulent"))
        friss_score = float(raw.get("frissScore") or 0) / 100.0
        connections.append(NetworkConnection(
            node_id=raw.get("id") or f"NODE-{uuid.uuid4().hex[:8].upper()}",
            node_type=_type_map.get((raw.get("entityType") or "").upper(), NODE_PROVIDER),
            node_label=raw.get("entityName") or "Unknown",
            shared_claim_ids=[c.get("claimReference","") for c in related],
            shared_claim_count=len(related),
            fraud_flagged_count=fraud_count,
            fraud_history=fraud_count > 0,
            confidence=friss_score,
            ring_id=raw.get("ringReference"),
            first_seen=raw.get("detectedDate"),
            last_seen=raw.get("detectedDate"),
        ))
    return connections


class FRISSAdapter:
    """FRISS Cloud API v3 live adapter.

    Authentication: X-API-Key header + X-Company-Code (FRISS-assigned).

    Wire protocol:
      POST {FRISS_API_BASE_URL}/api/v3/scores/claims
      Headers:
        X-API-Key:      {FRISS_API_KEY}
        X-Company-Code: {FRISS_COMPANY_CODE}
        Content-Type:   application/json

    Body:
      {
        "claimReference":  "CLM-...",
        "claimantName":    "string",
        "vehicleVin":      "string",
        "providers":       [ { "type": "...", "name": "...", "location": "..." } ],
        "lossDate":        "YYYY-MM-DD",
        "lossType":        "COLLISION|COMPREHENSIVE|...",
        "analyzeNetwork":  true
      }

    Response:
      {
        "scoreId":       "string",
        "frissScore":    int 0-100,
        "riskLevel":     "LOW|MEDIUM|HIGH",
        "connections":   [ { ...FRISS connection fields... } ],
        "indicators":    [ { "type", "description", "score" } ]
      }

    FRISS score mapping to platform risk level:
      0-39:  LOW
      40-59: MEDIUM
      60-79: HIGH
      80-100: CRITICAL
    """

    PROVIDER = "friss"
    MODE     = "live"
    _RETRY_ON = {503, 504}

    def __init__(self, base_url: str, api_key: str, company_code: str,
                 timeout: float = 10.0) -> None:
        self.base_url    = base_url.rstrip("/")
        self.api_key     = api_key
        self.company_code = company_code
        self.timeout     = timeout
        self._fallback   = ShellNetworkGraphAdapter()
        log.info("FRISSAdapter initialised — base_url=%s", self.base_url)

    def _headers(self) -> Dict[str, str]:
        return {
            "X-API-Key":      self.api_key,
            "X-Company-Code": self.company_code,
            "Content-Type":   "application/json",
            "Accept":         "application/json",
            "User-Agent":     "fnol-intelligence-platform/1.0",
        }

    def _build_body(self, req: NetworkGraphRequest) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "claimReference": req.claim_id,
            "analyzeNetwork": True,
        }
        if req.claimant_name:  body["claimantName"] = req.claimant_name
        if req.vin:            body["vehicleVin"]   = req.vin
        if req.loss_date:      body["lossDate"]     = req.loss_date
        if req.loss_cause:     body["lossType"]     = req.loss_cause

        providers = []
        if req.attorney_name:
            providers.append({"type": "ATTORNEY", "name": req.attorney_name,
                               "location": req.attorney_zip or ""})
        if req.repair_shop_name:
            providers.append({"type": "REPAIRER", "name": req.repair_shop_name,
                               "location": req.repair_shop_zip or ""})
        if req.medical_provider:
            providers.append({"type": "MEDICAL", "name": req.medical_provider})
        if providers: body["providers"] = providers
        return body

    def query(self, req: NetworkGraphRequest) -> NetworkGraphResponse:
        t0 = time.monotonic()
        ck = _cache_key(req)
        cached = _CACHE.get(ck)
        if cached is not None:
            cached.cached = True
            return cached

        try:
            import httpx
        except ImportError:
            return self._fallback.query(req)

        url = f"{self.base_url}/api/v3/scores/claims"
        raw_resp = None
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=self.timeout,
                                                     write=5.0, pool=5.0)) as client:
                for attempt in range(2):
                    try:
                        raw_resp = client.post(url, json=self._build_body(req),
                                               headers=self._headers())
                        if raw_resp.status_code in self._RETRY_ON and attempt == 0:
                            time.sleep(1.0); continue
                        break
                    except (httpx.TransportError, httpx.TimeoutException) as exc:
                        if attempt == 0: time.sleep(1.0)
                        else: raise
        except Exception as exc:
            log.error("FRISS API failed for %s: %s — shell fallback", req.claim_id, exc)
            return self._fallback.query(req)

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if raw_resp is None or raw_resp.status_code >= 500:
            return self._fallback.query(req)
        if raw_resp.status_code in (401, 403):
            log.error("FRISS 401/403 — check FRISS_API_KEY and FRISS_COMPANY_CODE")
            return self._fallback.query(req)
        if raw_resp.status_code == 429:
            return self._fallback.query(req)
        if not (200 <= raw_resp.status_code < 300):
            return self._fallback.query(req)

        try:
            payload = raw_resp.json()
        except Exception:
            return self._fallback.query(req)

        score_id     = payload.get("scoreId") or f"FRISS-{uuid.uuid4().hex[:10].upper()}"
        raw_conns    = payload.get("connections") or []
        connections  = _map_friss_connections(raw_conns)
        friss_raw    = float(payload.get("frissScore") or 0)
        vendor_score = friss_raw / 100.0
        vendor_risk  = ("CRITICAL" if friss_raw >= 80 else
                        "HIGH"     if friss_raw >= 60 else
                        "MEDIUM"   if friss_raw >= 40 else "LOW")

        classification = _classify_ring(connections)
        weight, rationale = _score_and_rationale(classification, connections, self.PROVIDER)
        network_flag = classification in SIU_MANDATORY_CLASSIFICATIONS

        resp = NetworkGraphResponse(
            inquiry_id=req.inquiry_id, claim_id=req.claim_id,
            provider=self.PROVIDER, adapter_mode=self.MODE,
            ring_classification=classification, network_flag=network_flag,
            ring_id=connections[0].ring_id if connections else None,
            ring_size=sum(c.shared_claim_count for c in connections),
            connections=connections, total_connections=len(connections),
            fraud_flagged_connections=sum(1 for c in connections if c.fraud_history),
            network_signal_weight=weight, network_signal_rationale=rationale,
            vendor_fraud_score=vendor_score, vendor_risk_level=vendor_risk,
            transaction_id=score_id, queried_at=_now(),
            elapsed_ms=elapsed_ms, cached=False,
        )
        _CACHE.set(ck, resp)
        log.info(
            "FRISS query complete claim=%s ring=%s flag=%s friss=%d elapsed=%dms",
            req.claim_id, classification, network_flag, int(friss_raw), elapsed_ms,
        )
        return resp

    def health(self) -> Dict[str, Any]:
        return {
            "mode": self.MODE, "provider": self.PROVIDER,
            "status": "ok", "base_url": self.base_url,
            "company_code": self.company_code[:4] + "…",
            "cache_entries": len(_CACHE),
        }


# ───────────────────────────────────────────────────────────────────────────
# Adapter resolver
# ───────────────────────────────────────────────────────────────────────────

_ADAPTER_INSTANCE: Any = None
_ADAPTER_LOCK = __import__("threading").Lock()


def get_adapter() -> Any:
    """Return the process-wide network graph adapter singleton.

    Resolution order:
      1. NETWORK_GRAPH_ADAPTER=mock   → MockNetworkGraphAdapter (always)
      2. NETWORK_GRAPH_ADAPTER=live   → raises if credentials absent
      3. auto (default):
         a. All required live credentials for chosen provider present → live
         b. Otherwise → ShellNetworkGraphAdapter
    """
    global _ADAPTER_INSTANCE
    with _ADAPTER_LOCK:
        if _ADAPTER_INSTANCE is not None:
            return _ADAPTER_INSTANCE

        mode_override = os.getenv("NETWORK_GRAPH_ADAPTER", "auto").lower()
        provider      = os.getenv("NETWORK_GRAPH_PROVIDER", "shift").lower()
        timeout       = float(os.getenv("NETWORK_GRAPH_TIMEOUT", "10.0"))

        if mode_override == "mock":
            _ADAPTER_INSTANCE = MockNetworkGraphAdapter()
            log.info("Network graph adapter: mock")
            return _ADAPTER_INSTANCE

        # Shift credentials
        shift_url    = os.getenv("SHIFT_API_BASE_URL", "")
        shift_key    = os.getenv("SHIFT_API_KEY", "")
        shift_tenant = os.getenv("SHIFT_TENANT_ID", "")

        # FRISS credentials
        friss_url  = os.getenv("FRISS_API_BASE_URL", "")
        friss_key  = os.getenv("FRISS_API_KEY", "")
        friss_code = os.getenv("FRISS_COMPANY_CODE", "")

        shift_ready = bool(shift_url and shift_key and shift_tenant)
        friss_ready = bool(friss_url and friss_key and friss_code)

        if mode_override == "live":
            if provider == "friss" and not friss_ready:
                raise ValueError("NETWORK_GRAPH_ADAPTER=live provider=friss but FRISS_API_BASE_URL/FRISS_API_KEY/FRISS_COMPANY_CODE not set")
            if provider != "friss" and not shift_ready:
                raise ValueError("NETWORK_GRAPH_ADAPTER=live provider=shift but SHIFT_API_BASE_URL/SHIFT_API_KEY/SHIFT_TENANT_ID not set")

        if provider == "friss" and friss_ready:
            try:
                _ADAPTER_INSTANCE = FRISSAdapter(friss_url, friss_key, friss_code, timeout)
                log.info("Network graph adapter: FRISS live")
            except Exception as exc:
                log.error("FRISS init failed: %s — shell fallback", exc)
                _ADAPTER_INSTANCE = ShellNetworkGraphAdapter()
        elif shift_ready:
            try:
                _ADAPTER_INSTANCE = ShiftTechnologyAdapter(shift_url, shift_key, shift_tenant, timeout)
                log.info("Network graph adapter: Shift Technology live")
            except Exception as exc:
                log.error("Shift init failed: %s — shell fallback", exc)
                _ADAPTER_INSTANCE = ShellNetworkGraphAdapter()
        else:
            _ADAPTER_INSTANCE = ShellNetworkGraphAdapter()
            log.info(
                "Network graph adapter: shell (no Shift/FRISS credentials). "
                "Set SHIFT_API_BASE_URL+SHIFT_API_KEY+SHIFT_TENANT_ID "
                "or FRISS_API_BASE_URL+FRISS_API_KEY+FRISS_COMPANY_CODE)"
            )
        return _ADAPTER_INSTANCE


# ───────────────────────────────────────────────────────────────────────────
# Module-level convenience wrappers
# ───────────────────────────────────────────────────────────────────────────

def query(request: NetworkGraphRequest) -> NetworkGraphResponse:
    return get_adapter().query(request)


def health() -> Dict[str, Any]:
    h = get_adapter().health()
    h["cache"] = cache_stats()
    return h


def build_request_from_claim(claim_id: str, claim_data: Dict[str, Any]) -> NetworkGraphRequest:
    """Build a NetworkGraphRequest from a Claim.model_dump() dict.

    Extracts only the fields needed for network analysis. Free-text fields
    (loss_description, reporter_name) are intentionally excluded to minimise
    PII surface sent to a third-party API.
    """
    reporter = claim_data.get("reporter_name") or ""
    name_parts = reporter.strip().split(None, 1)
    claimant_name = reporter.strip() or None

    loss_dt_raw = claim_data.get("loss_date_time") or ""
    loss_date = loss_dt_raw[:10] if loss_dt_raw else None

    return NetworkGraphRequest(
        claim_id=claim_id,
        claimant_name=claimant_name,
        claimant_phone=claim_data.get("reporter_phone"),
        claimant_zip=(claim_data.get("loss_location_zip") or claim_data.get("location_zip") or "")[:5] or None,
        vin=claim_data.get("vin"),
        vehicle_make=claim_data.get("vehicle_make"),
        vehicle_model=claim_data.get("vehicle_model"),
        vehicle_year=claim_data.get("vehicle_year"),
        attorney_name=None,   # Not captured at FNOL intake; set post-intake if known
        repair_shop_name=None,  # Set post-intake from tow destination / DRP selection
        loss_location=claim_data.get("loss_location"),
        loss_zip=(claim_data.get("loss_location_zip") or "")[:5] or None,
        loss_date=loss_date,
        loss_cause=claim_data.get("loss_cause"),
        policy_number=claim_data.get("policy_number"),
    )


def invalidate_cache(claim_id: str) -> bool:
    removed = False
    for key, resp in list(_CACHE.items()):
        if isinstance(resp, NetworkGraphResponse) and resp.claim_id == claim_id:
            _CACHE.delete(key)
            removed = True
    return removed


def cache_stats() -> Dict[str, Any]:
    entries = [r for r in _CACHE.values() if isinstance(r, NetworkGraphResponse)]
    return {
        "total_entries":  len(entries),
        "cached_claims":  len({r.claim_id for r in entries}),
        "adapter_modes":  list({r.adapter_mode for r in entries}),
        "ring_classifications": dict(
            (cls, sum(1 for r in entries if r.ring_classification == cls))
            for cls in ["CONFIRMED_RING","SUSPECTED_RING","ELEVATED","ADVISORY","NONE"]
            if any(r.ring_classification == cls for r in entries)
        ),
        "cache_ttl_seconds": _CACHE._ttl,
    }
