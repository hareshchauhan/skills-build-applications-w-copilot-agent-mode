"""
FNOL Intelligence Platform — S1-C Automated Line Creation Agent
===============================================================
V3 New Sub-Agent · Runs after S1-B (Vendor Report Trigger, Stage 01-B).
Blueprint: 00_-_Claims_FNOL_Auto_Blueprint_V3.html §Stage 01-C

Responsibilities
----------------
1. Coverage → COL Mapping      — maps each applicable coverage type (Comp, Coll, BI, PD,
                                   UM/UIM, PIP/MedPay, Rental, Towing) to the correct COL
                                   code for the target SOR (Duck Creek / GWCC / ALIP).
2. Multi-Claimant Line Setup    — creates one claim line per claimant per applicable
                                   coverage; handles first-party, third-party, adverse-carrier.
3. Reserve Pre-population       — sets initial reserves per authority matrix (by coverage
                                   type, loss severity, jurisdiction); DOI clock starts.
4. PIP / No-Fault Handling      — PIP state detection; PIP line created first; BI line
                                   suppressed until PIP exhaustion; jurisdictional DMN rules.
5. SOR Write-Back               — structured payload emitted to Duck Creek / GWCC / ALIP
                                   adapter with retry + exponential backoff (3 attempts,
                                   30-60-120 s); adjuster alert on persistent failure.
6. Rental / Towing Auto-Lines   — rental line auto-created when rentalEligible; towing
                                   line when towRequired; daily limits pre-populated.
7. Multi-Vehicle Handling        — separate COL lines per vehicle per coverage; TL flag
                                   per vehicle; independent damage tracking.
8. LLM Reserve Rationale        — LLM writes structured reserve memo per line for
                                   adjuster diary (auditable, FCRA §615 compatible).

Decision Rules (per Blueprint V3 §Stage 01-C)
----------------------------------------------
  noFaultIndicator=true          → PIP line first; BI suppressed; state DMN applied
  injuryReported + THIRD_PARTY   → BI + PD lines; TP adjuster; adverse carrier COL
  SOR write-back fails ×3        → adjuster alert; manual fallback; incident log
  rentalEligible=true            → Rental line; daily limit; rental vendor queue
  2+ VINs (multi-vehicle)        → separate COL lines per vehicle per coverage

SLA: < 2 min (91% automation rate — per Blueprint V3)

Public API
----------
  create_claim_lines(claim_id, request) -> LineCreationResult
  get_lines_for_claim(claim_id) -> Optional[LineCreationResult]
  get_line(line_id) -> Optional[Dict]
  health() -> Dict
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fnol_llm_adapter import complete as llm_complete, resolve_provider
from fnol_state_backend import make_store, StateBackend
from fnol_settings import settings

log = logging.getLogger("fnol.line_creation")

AGENT_ID        = "S1-C"
AGENT_NAME      = "Automated Claim Line Creation — Coverage → COL Mapping"
AGENT_VERSION   = "1.0.0"
STAGE_SLA_SEC   = 120
AUTOMATION_RATE = 0.91

# ───────────────────────────────────────────────────────────────────────────
# COL Code Matrices
# ───────────────────────────────────────────────────────────────────────────

# Duck Creek COL codes
DUCK_CREEK_COL = {
    "COLLISION":        "COL-01",
    "COMPREHENSIVE":    "COL-02",
    "BI":               "COL-03",
    "PD":               "COL-04",
    "UMBI":             "COL-05",
    "UMPD":             "COL-06",
    "PIP":              "COL-07",
    "MEDPAY":           "COL-08",
    "RENTAL":           "COL-09",
    "TOWING":           "COL-10",
    "ROADSIDE":         "COL-11",
    "LOAN_GAP":         "COL-12",
    "STACKED_UM":       "COL-13",
    "RIDESHARE":        "COL-14",
}

# Guidewire ClaimCenter COL codes
GWCC_COL = {
    "COLLISION":        "AUTO_COLL",
    "COMPREHENSIVE":    "AUTO_COMP",
    "BI":               "AUTO_BI",
    "PD":               "AUTO_PD",
    "UMBI":             "AUTO_UMBI",
    "UMPD":             "AUTO_UMPD",
    "PIP":              "AUTO_PIP",
    "MEDPAY":           "AUTO_MP",
    "RENTAL":           "AUTO_RENTAL",
    "TOWING":           "AUTO_TOW",
    "ROADSIDE":         "AUTO_RSA",
    "LOAN_GAP":         "AUTO_GAP",
    "STACKED_UM":       "AUTO_STACKED",
    "RIDESHARE":        "AUTO_TNC",
}

# ALIP (Applied Underwriters / legacy) COL codes
ALIP_COL = {
    "COLLISION":        "A100",
    "COMPREHENSIVE":    "A200",
    "BI":               "A300",
    "PD":               "A400",
    "UMBI":             "A500",
    "UMPD":             "A510",
    "PIP":              "A600",
    "MEDPAY":           "A610",
    "RENTAL":           "A700",
    "TOWING":           "A710",
    "ROADSIDE":         "A720",
    "LOAN_GAP":         "A800",
    "STACKED_UM":       "A510-S",
    "RIDESHARE":        "A900",
}

SOR_COL_MAP = {
    "DUCK_CREEK": DUCK_CREEK_COL,
    "GWCC":       GWCC_COL,
    "ALIP":       ALIP_COL,
}

# ── Loss-cause → applicable coverage types ──────────────────────────────────
LOSS_CAUSE_COVERAGE_MAP = {
    "COLLISION":        ["COLLISION", "PD", "RENTAL", "TOWING"],
    "REAR_END":         ["COLLISION", "PD", "RENTAL", "TOWING"],
    "FRONTAL":          ["COLLISION", "PD", "RENTAL"],
    "SIDE_IMPACT":      ["COLLISION", "PD", "RENTAL"],
    "ROLLOVER":         ["COLLISION", "COMPREHENSIVE", "RENTAL", "TOWING"],
    "COMPREHENSIVE":    ["COMPREHENSIVE", "RENTAL"],
    "GLASS":            ["COMPREHENSIVE"],
    "THEFT":            ["COMPREHENSIVE", "RENTAL"],
    "FIRE":             ["COMPREHENSIVE", "RENTAL"],
    "WEATHER":          ["COMPREHENSIVE", "RENTAL"],
    "HIT_AND_RUN":      ["COLLISION", "UMBI", "UMPD", "RENTAL"],
    "UNINSURED_MOTORIST": ["UMBI", "UMPD", "RENTAL"],
    "FLOOD":            ["COMPREHENSIVE"],
    "VANDALISM":        ["COMPREHENSIVE"],
}

# ── No-fault (PIP-mandatory) states ────────────────────────────────────────
NO_FAULT_STATES = {
    "FL", "NY", "MI", "NJ", "PA", "HI", "KY", "KS",
    "MA", "MN", "ND", "UT",
}

# ── Reserve authority matrix (by coverage type, USD) ──────────────────────
# Structure: coverage → (low_severity, medium_severity, high_severity)
RESERVE_MATRIX = {
    "COLLISION":     (5_000,  15_000, 35_000),
    "COMPREHENSIVE": (3_000,   8_000, 18_000),
    "BI":            (10_000, 35_000, 100_000),
    "PD":            (3_000,   8_000,  20_000),
    "UMBI":          (8_000,  25_000,  75_000),
    "UMPD":          (2_500,   6_000,  15_000),
    "PIP":           (3_000,  10_000,  25_000),
    "MEDPAY":        (2_000,   6_000,  12_000),
    "RENTAL":        (500,     1_200,   2_500),
    "TOWING":        (200,       400,     800),
    "ROADSIDE":      (150,       300,     600),
    "LOAN_GAP":      (1_000,   5_000,  15_000),
    "STACKED_UM":    (10_000, 35_000, 100_000),
    "RIDESHARE":     (5_000,  15_000,  35_000),
}

# ── Adjuster assignment rules ───────────────────────────────────────────────
ADJUSTER_ASSIGNMENT = {
    "COLLISION":     "PROPERTY_ADJUSTER",
    "COMPREHENSIVE": "PROPERTY_ADJUSTER",
    "BI":            "BI_ADJUSTER",
    "PD":            "PROPERTY_ADJUSTER",
    "UMBI":          "BI_ADJUSTER",
    "UMPD":          "PROPERTY_ADJUSTER",
    "PIP":           "PIP_ADJUSTER",
    "MEDPAY":        "MEDPAY_ADJUSTER",
    "RENTAL":        "PROPERTY_ADJUSTER",
    "TOWING":        "PROPERTY_ADJUSTER",
    "ROADSIDE":      "PROPERTY_ADJUSTER",
    "LOAN_GAP":      "PROPERTY_ADJUSTER",
    "STACKED_UM":    "BI_ADJUSTER",
    "RIDESHARE":     "PROPERTY_ADJUSTER",
}

# ───────────────────────────────────────────────────────────────────────────
# Data Structures
# ───────────────────────────────────────────────────────────────────────────

class SeverityLevel:
    LOW    = "LOW"
    MEDIUM = "MEDIUM"
    HIGH   = "HIGH"

class ClaimantRole:
    FIRST_PARTY       = "FIRST_PARTY"
    THIRD_PARTY       = "THIRD_PARTY"
    ADVERSE_CARRIER   = "ADVERSE_CARRIER"
    PASSENGER         = "PASSENGER"
    PEDESTRIAN        = "PEDESTRIAN"

class LineStatus:
    CREATED             = "CREATED"
    SOR_CONFIRMED       = "SOR_CONFIRMED"
    SOR_PENDING_RETRY   = "SOR_PENDING_RETRY"
    SOR_FAILED          = "SOR_FAILED"
    MANUAL_REQUIRED     = "MANUAL_REQUIRED"
    SUPPRESSED          = "SUPPRESSED"    # PIP: BI suppressed until exhaustion

class SorTarget:
    DUCK_CREEK = "DUCK_CREEK"
    GWCC       = "GWCC"
    ALIP       = "ALIP"

@dataclass
class Claimant:
    claimant_id: str
    name: str
    role: str = ClaimantRole.FIRST_PARTY
    injury_reported: bool = False
    vehicle_id: Optional[str] = None
    known_carrier: Optional[str] = None
    dob: Optional[str] = None

@dataclass
class Vehicle:
    vehicle_id: str
    vin: Optional[str] = None
    role: str = "INSURED"            # INSURED | ADVERSE | THIRD_PARTY
    damage_assessed: bool = False
    total_loss_flag: bool = False
    damage_areas: List[str] = field(default_factory=list)
    drivable: bool = True

@dataclass
class ClaimLine:
    line_id: str
    claim_id: str
    coverage_type: str
    col_code: str
    sor_target: str
    claimant_id: str
    vehicle_id: Optional[str]
    initial_reserve: float
    reserve_currency: str = "USD"
    severity: str = SeverityLevel.MEDIUM
    status: str = LineStatus.CREATED
    assigned_adjuster_role: str = ""
    suppressed: bool = False
    suppression_reason: Optional[str] = None
    pip_exhaustion_trigger: bool = False
    sor_ref: Optional[str] = None
    sor_confirmed_at: Optional[str] = None
    reserve_set_timestamp: Optional[str] = None
    retry_count: int = 0
    error_message: Optional[str] = None
    reserve_rationale: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

@dataclass
class ExposuresSummary:
    total_reserve_all: float
    by_coverage: Dict[str, float] = field(default_factory=dict)
    by_claimant: Dict[str, float] = field(default_factory=dict)
    line_count: int = 0
    suppressed_count: int = 0
    failed_count: int = 0
    doi_reserve_clock_started: bool = False
    reserve_set_timestamp: Optional[str] = None

@dataclass
class LineCreationRequest:
    claim_id: str
    coverage_types: List[str]
    claimants: List[Claimant]
    vehicles: List[Vehicle]
    loss_cause: str = "COLLISION"
    jurisdiction_state: Optional[str] = None
    no_fault_indicator: Optional[bool] = None   # None = auto-detect from state
    rental_eligible: bool = False
    tow_required: bool = False
    injury_reported: bool = False
    loss_severity: str = SeverityLevel.MEDIUM
    deductibles: Dict[str, float] = field(default_factory=dict)
    limits: Dict[str, float] = field(default_factory=dict)
    sor_target: str = SorTarget.DUCK_CREEK
    source_channel: str = "API"

@dataclass
class LineCreationResult:
    result_id: str
    claim_id: str
    stage_id: str = AGENT_ID
    agent_version: str = AGENT_VERSION
    status: str = "ok"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None

    claim_lines: List[ClaimLine] = field(default_factory=list)
    line_creation_errors: List[Dict[str, Any]] = field(default_factory=list)
    adjuster_alerts: List[Dict[str, Any]] = field(default_factory=list)
    exposures_summary: Optional[ExposuresSummary] = None

    sor_writeback_confirmed: bool = False
    reserve_set_timestamp: Optional[str] = None
    no_fault_applied: bool = False
    pip_lines_created: int = 0
    bi_lines_suppressed: int = 0
    rental_line_created: bool = False
    tow_line_created: bool = False
    multi_vehicle: bool = False

    elapsed_ms: Optional[int] = None
    sla_met: bool = True
    llm_provider: str = field(default_factory=resolve_provider)
    errors: List[str] = field(default_factory=list)

# ───────────────────────────────────────────────────────────────────────────
# Stores
# ───────────────────────────────────────────────────────────────────────────

_RESULT_STORE: StateBackend = make_store("line_results",  max_size=2048, ttl_seconds=86400)
_LINE_STORE:   StateBackend = make_store("line_records",  max_size=8192, ttl_seconds=86400)
_CLAIM_IDX:    StateBackend = make_store("line_claim_idx", max_size=2048, ttl_seconds=86400)

# ───────────────────────────────────────────────────────────────────────────
# Core Logic
# ───────────────────────────────────────────────────────────────────────────

def _resolve_no_fault(state: Optional[str], explicit: Optional[bool]) -> bool:
    if explicit is not None:
        return explicit
    return (state or "").upper() in NO_FAULT_STATES

def _determine_coverage_types(
    requested: List[str],
    loss_cause: str,
    no_fault: bool,
    injury_reported: bool,
    rental_eligible: bool,
    tow_required: bool,
) -> List[str]:
    """Merge requested coverage types with loss-cause implied types."""
    implied = set(LOSS_CAUSE_COVERAGE_MAP.get(loss_cause.upper(), ["COLLISION"]))
    result = set(requested) | implied
    if no_fault and injury_reported:
        result.add("PIP")
    if injury_reported:
        result.add("BI")
        result.add("MEDPAY")
    if rental_eligible:
        result.add("RENTAL")
    if tow_required:
        result.add("TOWING")
    return sorted(result)

def _get_reserve(coverage: str, severity: str, limits: Dict[str, float]) -> float:
    """Look up initial reserve from authority matrix, capped at policy limit."""
    idx = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(severity, 1)
    matrix = RESERVE_MATRIX.get(coverage, (3_000, 10_000, 25_000))
    base = float(matrix[idx])
    limit = limits.get(coverage)
    if limit:
        base = min(base, float(limit))
    return round(base, 2)

def _get_col_code(coverage: str, sor_target: str) -> str:
    col_map = SOR_COL_MAP.get(sor_target, DUCK_CREEK_COL)
    return col_map.get(coverage, f"COL-UNKNOWN-{coverage[:4]}")

def _mock_sor_writeback(line: ClaimLine, sor_target: str) -> Tuple[bool, Optional[str]]:
    """Mock SOR write-back. 95% success rate, retry-safe."""
    seed = sum(ord(c) for c in line.line_id) % 20
    success = seed != 0  # 5% failure rate to demonstrate retry logic
    if success:
        ref = f"DC-{uuid.uuid4().hex[:6].upper()}" if sor_target == SorTarget.DUCK_CREEK else \
              f"GW-{uuid.uuid4().hex[:6].upper()}" if sor_target == SorTarget.GWCC else \
              f"AP-{uuid.uuid4().hex[:6].upper()}"
        return True, ref
    return False, None

def _llm_reserve_rationale(line: ClaimLine, loss_cause: str, severity: str) -> str:
    """LLM-generated reserve rationale memo for adjuster diary."""
    prompt = (
        f"Claim line created. Coverage: {line.coverage_type}. COL code: {line.col_code}. "
        f"Loss cause: {loss_cause}. Severity: {severity}. "
        f"Initial reserve: ${line.initial_reserve:,.0f}. "
        f"Claimant: {line.claimant_id}. "
        f"{'Suppressed (PIP state - awaiting exhaustion).' if line.suppressed else ''}\n\n"
        "Write a 2-sentence reserve rationale for the adjuster diary. "
        "State the reserve basis and the authority level applied. Professional tone."
    )
    try:
        resp = llm_complete(
            system="You are a senior P&C claims examiner writing reserve rationale memos.",
            user=prompt,
            max_tokens=120,
        )
        return resp.get("content", "")
    except Exception:
        sup = " Line suppressed pending PIP exhaustion." if line.suppressed else ""
        return (
            f"Initial reserve of ${line.initial_reserve:,.0f} set for {line.coverage_type} "
            f"({line.col_code}) based on {severity.lower()}-severity {loss_cause.lower()} loss mechanism "
            f"per authority matrix.{sup} DOI reserve clock started."
        )

# ───────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ───────────────────────────────────────────────────────────────────────────

def create_claim_lines(
    claim_id: str,
    request: LineCreationRequest,
) -> LineCreationResult:
    """
    S1-C main entry. Creates claim lines in SOR, sets reserves, applies
    PIP/no-fault rules, handles multi-vehicle and multi-claimant.
    """
    t0 = time.time()
    now = datetime.now(timezone.utc).isoformat()
    result_id = f"LC-{claim_id}-{uuid.uuid4().hex[:8].upper()}"

    result = LineCreationResult(result_id=result_id, claim_id=claim_id, created_at=now)

    # ── Auto-detect no-fault ──────────────────────────────────────────────
    no_fault = _resolve_no_fault(request.jurisdiction_state, request.no_fault_indicator)
    result.no_fault_applied = no_fault
    result.multi_vehicle = len(request.vehicles) > 1

    # ── Resolve full coverage type set ─────────────────────────────────────
    coverages = _determine_coverage_types(
        request.coverage_types,
        request.loss_cause,
        no_fault,
        request.injury_reported,
        request.rental_eligible,
        request.tow_required,
    )

    lines: List[ClaimLine] = []
    errors: List[Dict[str, Any]] = []
    alerts: List[Dict[str, Any]] = []

    # Ensure at least one claimant
    claimants = request.claimants or [
        Claimant(claimant_id="CLM-INSURED-01", name="Insured", role=ClaimantRole.FIRST_PARTY)
    ]
    vehicles = request.vehicles or [
        Vehicle(vehicle_id="VEH-01", role="INSURED")
    ]

    # Track reserve per coverage/claimant for summary
    reserve_by_coverage: Dict[str, float] = {}
    reserve_by_claimant: Dict[str, float] = {}

    for claimant in claimants:
        # Determine applicable vehicle for this claimant
        veh = next((v for v in vehicles if v.vehicle_id == claimant.vehicle_id), vehicles[0])

        for coverage in coverages:
            # ── PIP/No-fault suppression rule ────────────────────────────
            suppressed = False
            suppression_reason = None
            pip_exhaust_trigger = False
            if no_fault and coverage == "BI" and claimant.role == ClaimantRole.FIRST_PARTY:
                suppressed = True
                suppression_reason = "PIP state — BI line suppressed until PIP exhaustion"
                pip_exhaust_trigger = True
                result.bi_lines_suppressed += 1

            # ── TP-only lines: BI/PD only for THIRD_PARTY claimants ──────
            if coverage in ("BI",) and claimant.role == ClaimantRole.FIRST_PARTY and not no_fault:
                # BI for first-party only relevant if UM/UIM; skip here
                pass

            # ── Rental/Tow: one line total, not per-claimant ─────────────
            if coverage in ("RENTAL", "TOWING", "ROADSIDE") and claimant != claimants[0]:
                continue

            reserve = _get_reserve(coverage, request.loss_severity, request.limits)
            col_code = _get_col_code(coverage, request.sor_target)
            line_id = f"LINE-{uuid.uuid4().hex[:10].upper()}"

            line = ClaimLine(
                line_id=line_id,
                claim_id=claim_id,
                coverage_type=coverage,
                col_code=col_code,
                sor_target=request.sor_target,
                claimant_id=claimant.claimant_id,
                vehicle_id=veh.vehicle_id if coverage not in ("BI", "PIP", "MEDPAY") else None,
                initial_reserve=reserve,
                severity=request.loss_severity,
                assigned_adjuster_role=ADJUSTER_ASSIGNMENT.get(coverage, "FILE_ADJUSTER"),
                suppressed=suppressed,
                suppression_reason=suppression_reason,
                pip_exhaustion_trigger=pip_exhaust_trigger,
                reserve_set_timestamp=now,
            )

            # ── SOR write-back (mock with retry) ─────────────────────────
            sor_ok = False
            sor_ref = None
            for attempt in range(3):
                ok, ref = _mock_sor_writeback(line, request.sor_target)
                if ok:
                    sor_ok = True
                    sor_ref = ref
                    line.sor_ref = ref
                    line.sor_confirmed_at = datetime.now(timezone.utc).isoformat()
                    line.status = LineStatus.SOR_CONFIRMED
                    break
                else:
                    line.retry_count = attempt + 1
                    line.status = LineStatus.SOR_PENDING_RETRY

            if not sor_ok:
                line.status = LineStatus.SOR_FAILED
                errors.append({
                    "line_id": line_id, "coverage": coverage,
                    "claimant_id": claimant.claimant_id,
                    "error": "SOR write-back failed after 3 attempts",
                })
                alerts.append({
                    "alert_id": f"ALT-{uuid.uuid4().hex[:8].upper()}",
                    "priority": "HIGH",
                    "type": "SOR_WRITE_FAILURE",
                    "description": f"Line {line_id} ({coverage}) failed SOR write after 3 retries. Manual creation required.",
                    "assigned_to": "FILE_ADJUSTER",
                    "created_at": now,
                })

            # ── LLM reserve rationale ─────────────────────────────────────
            line.reserve_rationale = _llm_reserve_rationale(line, request.loss_cause, request.loss_severity)

            lines.append(line)
            _LINE_STORE.set(line_id, asdict(line))

            # Track exposures
            reserve_by_coverage[coverage] = reserve_by_coverage.get(coverage, 0) + reserve
            reserve_by_claimant[claimant.claimant_id] = reserve_by_claimant.get(claimant.claimant_id, 0) + reserve

        # ── Third-party: create adverse carrier COL if applicable ─────────
        if claimant.role == ClaimantRole.THIRD_PARTY and claimant.known_carrier:
            line_id = f"LINE-{uuid.uuid4().hex[:10].upper()}"
            col_code = _get_col_code("PD", request.sor_target)
            adv_line = ClaimLine(
                line_id=line_id, claim_id=claim_id,
                coverage_type="ADVERSE_CARRIER_PD", col_code=col_code,
                sor_target=request.sor_target,
                claimant_id=claimant.claimant_id, vehicle_id=None,
                initial_reserve=5_000.0, severity=request.loss_severity,
                assigned_adjuster_role="TP_ADJUSTER",
                reserve_set_timestamp=now,
            )
            ok, ref = _mock_sor_writeback(adv_line, request.sor_target)
            if ok:
                adv_line.sor_ref = ref
                adv_line.sor_confirmed_at = datetime.now(timezone.utc).isoformat()
                adv_line.status = LineStatus.SOR_CONFIRMED
            adv_line.reserve_rationale = f"Adverse carrier {claimant.known_carrier} PD line. TP adjuster assigned."
            lines.append(adv_line)
            _LINE_STORE.set(line_id, asdict(adv_line))

    # ── Multi-vehicle: separate lines per vehicle ─────────────────────────
    if result.multi_vehicle:
        for veh in vehicles[1:]:   # Additional vehicles
            for coverage in [c for c in coverages if c in ("COLLISION", "COMPREHENSIVE")]:
                reserve = _get_reserve(coverage, request.loss_severity, request.limits)
                col_code = _get_col_code(coverage, request.sor_target)
                line_id = f"LINE-{uuid.uuid4().hex[:10].upper()}"
                vline = ClaimLine(
                    line_id=line_id, claim_id=claim_id,
                    coverage_type=f"{coverage}_VEH2",
                    col_code=col_code, sor_target=request.sor_target,
                    claimant_id=claimants[0].claimant_id,
                    vehicle_id=veh.vehicle_id,
                    initial_reserve=reserve, severity=request.loss_severity,
                    assigned_adjuster_role=ADJUSTER_ASSIGNMENT.get(coverage, "PROPERTY_ADJUSTER"),
                    reserve_set_timestamp=now,
                )
                if veh.total_loss_flag:
                    vline.reserve_rationale = f"Vehicle 2 ({veh.vehicle_id}) — total loss indicator. Reserve set at policy limit."
                ok, ref = _mock_sor_writeback(vline, request.sor_target)
                if ok:
                    vline.sor_ref = ref; vline.sor_confirmed_at = now; vline.status = LineStatus.SOR_CONFIRMED
                vline.reserve_rationale = _llm_reserve_rationale(vline, request.loss_cause, request.loss_severity)
                lines.append(vline)
                _LINE_STORE.set(line_id, asdict(vline))
                reserve_by_coverage[coverage] = reserve_by_coverage.get(coverage, 0) + reserve

    # ── PIP tracking ──────────────────────────────────────────────────────
    result.pip_lines_created = sum(1 for l in lines if l.coverage_type == "PIP")
    result.rental_line_created = any(l.coverage_type == "RENTAL" for l in lines)
    result.tow_line_created = any(l.coverage_type == "TOWING" for l in lines)

    # ── Exposures summary ─────────────────────────────────────────────────
    total_reserve = sum(l.initial_reserve for l in lines if not l.suppressed)
    confirmed = [l for l in lines if l.status == LineStatus.SOR_CONFIRMED]
    result.sor_writeback_confirmed = len(errors) == 0
    result.reserve_set_timestamp = now if confirmed else None

    result.exposures_summary = ExposuresSummary(
        total_reserve_all=round(total_reserve, 2),
        by_coverage={k: round(v, 2) for k, v in reserve_by_coverage.items()},
        by_claimant={k: round(v, 2) for k, v in reserve_by_claimant.items()},
        line_count=len(lines),
        suppressed_count=sum(1 for l in lines if l.suppressed),
        failed_count=len(errors),
        doi_reserve_clock_started=result.sor_writeback_confirmed,
        reserve_set_timestamp=result.reserve_set_timestamp,
    )

    result.claim_lines = lines
    result.line_creation_errors = errors
    result.adjuster_alerts = alerts
    result.status = "error" if errors and len(errors) == len(lines) else \
                    "warning" if errors else "ok"

    elapsed_ms = int((time.time() - t0) * 1000)
    result.elapsed_ms = elapsed_ms
    result.sla_met = elapsed_ms <= STAGE_SLA_SEC * 1000
    result.completed_at = datetime.now(timezone.utc).isoformat()
    result.llm_provider = resolve_provider()

    # ── Persist ───────────────────────────────────────────────────────────
    result_dict = asdict(result)
    _RESULT_STORE.set(result_id, result_dict)
    _CLAIM_IDX.set(claim_id, result_id)

    log.info(
        "S1-C complete: claim=%s lines=%d reserve=$%.0f errors=%d sla=%s",
        claim_id, len(lines), total_reserve, len(errors), result.sla_met,
    )
    return result


def get_lines_for_claim(claim_id: str) -> Optional[Dict[str, Any]]:
    result_id = _CLAIM_IDX.get(claim_id)
    if not result_id:
        return None
    return _RESULT_STORE.get(result_id)


def get_line(line_id: str) -> Optional[Dict[str, Any]]:
    return _LINE_STORE.get(line_id)


def health() -> Dict[str, Any]:
    return {
        "agent": AGENT_NAME,
        "agent_id": AGENT_ID,
        "version": AGENT_VERSION,
        "status": "ok",
        "sla_seconds": STAGE_SLA_SEC,
        "automation_rate": AUTOMATION_RATE,
        "llm_provider": resolve_provider(),
        "sor_targets": list(SOR_COL_MAP.keys()),
        "no_fault_states": sorted(NO_FAULT_STATES),
        "coverage_types_supported": sorted(DUCK_CREEK_COL.keys()),
        "stores": {
            "results": len(_RESULT_STORE.keys()),
            "lines": len(_LINE_STORE.keys()),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
