"""
FNOL Intelligence Platform — A12 SIU Case Builder
==================================================
Blueprint V2 §S4A alignment · Special Investigations Unit referral package.

Position in the architecture
-----------------------------
A12 is a post-pipeline agent that consumes S4A (Fraud Detection) output and
packages suspect claims for SIU referral. It does NOT re-run the fraud model;
it acts on the fraud signals already captured in the pipeline trace.

Blueprint rules implemented (§S4A):
  • fraudRiskScore > 0.75 → CRITICAL: SIU_HOLD, paymentHoldFlag = true,
    mandatory SIU referral within 4h
  • fraudRiskScore 0.50–0.75 → HIGH: enhanced review; adjuster decision
    required before settlement
  • Network graph: claimant shares provider/attorney with 3+ flagged claims
    → NETWORK_FLAG escalation regardless of composite score
  • impactSeverityScore < 2.0 AND high-severity injury language in
    lossDescription → NARRATIVE_MISMATCH flag, weight +0.15

Responsibilities
----------------
1. Signal extraction — maps S4A pipeline outputs to typed FraudSignal records
   with per-signal weights, descriptions, and evidentiary category.

2. Risk scoring — deterministic weighted composite per 8 fraud categories.
   No LLM in the scoring path. Mirrors Blueprint §S4A signal breakdown output.

3. SIU team routing — maps dominant fraud category to one of 4 SIU teams;
   deterministic round-robin investigator assignment from POC pool.

4. Evidence dossier — builds structured EvidenceItem list from active signals;
   adjusters may append additional items via the API.

5. Referral memo generation — LLM-drafted narrative per NAIC Model Bulletin
   §IV. Falls back to deterministic template when provider is mock.

6. Case lifecycle management — OPEN → UNDER_INVESTIGATION →
   CLEARED | CONFIRMED_FRAUD | CLOSED_INCONCLUSIVE. paymentHoldFlag stays
   locked until adjuster-confirmed disposition.

7. Decision Records — every A12 action emits an immutable DecisionRecord with
   rule_id, confidence, rationale, hitl_required, and model_version for DOI
   audit defensibility (CA, NY, FL enforce SIU referral documentation).

Public API
----------
open_case(claim_id, pipeline_trace, claim_payload) -> SIUCase
add_evidence(case_id, evidence_type, description, source) -> SIUCase
save_notes(case_id, notes) -> SIUCase
generate_referral(case_id) -> SIUCase
close_case(case_id, disposition, investigator_notes) -> SIUCase
get_case(case_id) -> SIUCase | None
get_case_by_claim(claim_id) -> SIUCase | None
list_cases(limit) -> List[Dict]
health() -> Dict

Production hardening (pre-go-live)
------------------------------------
- Replace BoundedStore with PostgreSQL/DynamoDB backed SIU platform write
- Add RBAC: only SIU investigators + assigned adjuster + supervisor
- Integrate ISO ClaimSearch live API (Verisk) for real-time prior loss match
- Integrate Shift Technology / FRISS network graph API for ring detection
- Encrypt referral text at rest (PII in memo, FCRA-regulated)
- Send referral to SIU platform (CMS, Verafin, or carrier's internal system)
- NAIC Model Bulletin §IV: log every referral with AI basis, model version,
  and human-review confirmation before transmitting to SIU platform
"""

from __future__ import annotations

import json
import re
import uuid
import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from fnol_llm_adapter import complete as llm_complete, resolve_provider
from fnol_runtime import BoundedStore, redact_text
from fnol_settings import settings

AGENT_ID = "A12"
AGENT_NAME = "SIU Case Builder"
AGENT_VERSION = "1.0.0"

# ───────────────────────────────────────────────────────────────────────────
# Blueprint signal definitions — §S4A fraud model output categories
# ───────────────────────────────────────────────────────────────────────────

# Each entry: (rule_id, display_label, base_weight, evidence_category)
# Weights are POC defaults. Production: calibrate against carrier loss data
# and validated against State Farm / Nationwide fraud benchmarks.
_SIGNAL_DEFINITIONS: Dict[str, Tuple[str, float, str]] = {
    "ISO_CLAIMSEARCH": (
        "ISO ClaimSearch match",
        0.22,
        "DATABASE",
    ),
    "FREQUENCY_ANOMALY": (
        "Prior claims frequency anomaly",
        0.18,
        "DATABASE",
    ),
    "COVERAGE_SEEKING": (
        "Short policy tenure at loss date",
        0.14,
        "ANALYSIS",
    ),
    "DOCUMENTATION_GAP": (
        "Insufficient photo documentation",
        0.10,
        "PHOTO",
    ),
    "NARRATIVE_MISMATCH": (
        "Telematics vs. injury narrative mismatch",
        0.18,
        "ANALYSIS",
    ),
    "LITIGATION_RISK": (
        "Attorney representation at FNOL",
        0.08,
        "STATEMENT",
    ),
    "TIMING_ANOMALY": (
        "Suspicious loss timing pattern",
        0.06,
        "ANALYSIS",
    ),
    "NETWORK_FLAG": (
        "Network graph / staged accident indicator",
        0.30,            # Highest — Blueprint mandates SIU regardless of score
        "DATABASE",
    ),
}

# ───────────────────────────────────────────────────────────────────────────
# Fraud band thresholds (Blueprint §S4A)
# ───────────────────────────────────────────────────────────────────────────

FRAUD_BAND_CRITICAL_THRESHOLD = 0.75   # SIU_HOLD + paymentHoldFlag = true
FRAUD_BAND_HIGH_THRESHOLD     = 0.50   # Enhanced review; eligible for A12
FRAUD_BAND_MEDIUM_THRESHOLD   = 0.30   # Advisory only; not eligible for A12

# ───────────────────────────────────────────────────────────────────────────
# SIU team routing (maps dominant fraud category to specialist team)
# ───────────────────────────────────────────────────────────────────────────

_TEAM_ROUTING: Dict[str, str] = {
    "NETWORK_FLAG":       "Organized Fraud Unit",
    "ISO_CLAIMSEARCH":    "Identity & Prior Loss Unit",
    "FREQUENCY_ANOMALY":  "Identity & Prior Loss Unit",
    "NARRATIVE_MISMATCH": "Claims Investigation Unit",
    "DOCUMENTATION_GAP":  "Claims Investigation Unit",
    "COVERAGE_SEEKING":   "Pattern Analysis Unit",
    "TIMING_ANOMALY":     "Pattern Analysis Unit",
    "LITIGATION_RISK":    "Pattern Analysis Unit",
}

# ───────────────────────────────────────────────────────────────────────────
# POC investigator pool (4 investigators, deterministic assignment)
# Production: query carrier's SIU workforce-management system
# ───────────────────────────────────────────────────────────────────────────

_INVESTIGATORS = [
    {
        "investigator_id": "SIU-INV-001",
        "name": "Sarah Chen",
        "team": "Organized Fraud Unit",
        "specialty": "Staged accidents, ring detection, network analysis",
        "sla_hours": 4,
    },
    {
        "investigator_id": "SIU-INV-002",
        "name": "Marcus Williams",
        "team": "Identity & Prior Loss Unit",
        "specialty": "ISO ClaimSearch, prior loss analysis, identity fraud",
        "sla_hours": 4,
    },
    {
        "investigator_id": "SIU-INV-003",
        "name": "Priya Patel",
        "team": "Claims Investigation Unit",
        "specialty": "Field investigation, recorded statements, IVI",
        "sla_hours": 4,
    },
    {
        "investigator_id": "SIU-INV-004",
        "name": "David Torres",
        "team": "Pattern Analysis Unit",
        "specialty": "Coverage seeking, timing analysis, litigation patterns",
        "sla_hours": 4,
    },
]

# ───────────────────────────────────────────────────────────────────────────
# Case disposition values
# ───────────────────────────────────────────────────────────────────────────

VALID_DISPOSITIONS = frozenset({
    "CLEARED",               # SIU cleared — payment hold released
    "CONFIRMED_FRAUD",       # Fraud confirmed — denial + law enforcement referral
    "CLOSED_INCONCLUSIVE",   # Insufficient evidence — adjuster decision required
})

# ───────────────────────────────────────────────────────────────────────────
# Data contracts
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class FraudSignal:
    """One discrete fraud indicator extracted from S4A pipeline outputs."""
    signal_id: str            # e.g. "ISO_CLAIMSEARCH"
    label: str                # Human-readable label
    active: bool              # Whether this signal fired for this claim
    weight: float             # Contribution to composite score (0.0–1.0)
    score_contribution: float # actual weight * active (0 or weight)
    evidence_category: str    # DATABASE | PHOTO | STATEMENT | ANALYSIS
    detail: str               # Specific detail for this claim
    rule_fired: str           # Blueprint rule reference


@dataclass
class EvidenceItem:
    """One piece of evidence in the SIU dossier."""
    evidence_id: str
    evidence_type: str        # DATABASE | PHOTO | STATEMENT | ANALYSIS | DOCUMENT
    description: str
    source: str               # e.g. "ISO ClaimSearch", "Pipeline S4A", "Adjuster"
    flagged: bool = True
    added_at: str = ""
    added_by: str = "A12"


@dataclass
class SIUDecisionRecord:
    """Immutable decision record per NAIC Model Bulletin §IV.
    Every A12 action emits one of these for DOI audit defensibility."""
    record_id: str
    stage_name: str
    rule_id: str
    decision: str
    confidence: float
    rationale: str
    hitl_required: bool
    model_version: str
    timestamp: str


@dataclass
class SIUCase:
    """Full SIU case record — all state for one suspect claim."""
    case_id: str
    claim_id: str

    # Risk assessment
    fraud_risk_score: float         # 0.0–1.0 composite
    fraud_band: str                 # CRITICAL | HIGH | MEDIUM | LOW
    payment_hold_flag: bool
    triggered_categories: List[str] # Active signal IDs

    # SIU routing
    siu_team: str
    investigator: Dict[str, Any]

    # Case lifecycle
    status: str                     # OPEN | UNDER_INVESTIGATION | CLEARED |
                                    # CONFIRMED_FRAUD | CLOSED_INCONCLUSIVE
    opened_at: str
    sla_deadline: str               # T+4h for CRITICAL per Blueprint §siu-hold-subprocess
    updated_at: str

    # Signals + evidence
    signals: List[FraudSignal]
    evidence_items: List[EvidenceItem]

    # Human content
    adjuster_notes: str = ""
    referral_memo: str = ""
    referral_generated_at: str = ""
    referral_reference: str = ""

    # Disposition
    disposition: Optional[str] = None           # One of VALID_DISPOSITIONS
    disposition_notes: str = ""
    disposition_at: Optional[str] = None
    hold_released_at: Optional[str] = None

    # Decision records
    decisions: List[SIUDecisionRecord] = field(default_factory=list)

    # Claim snapshot (for referral memo context; PII-scrubbed at read time)
    claim_snapshot: Dict[str, Any] = field(default_factory=dict)

    # Metadata
    model_version: str = AGENT_VERSION


# ───────────────────────────────────────────────────────────────────────────
# In-memory store (POC). Production: replace with SIU platform write-through.
# ───────────────────────────────────────────────────────────────────────────

_STORE: BoundedStore = BoundedStore(
    max_size=settings.fnol_tl_eval_max,        # reuse TL eval limits (POC)
    ttl_seconds=settings.fnol_tl_eval_ttl_seconds,
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sla_deadline(hours: int = 4) -> str:
    """SLA deadline: T+hours from now, ISO 8601 UTC."""
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)
    return deadline.isoformat(timespec="seconds").replace("+00:00", "Z")


def _put(case: SIUCase) -> SIUCase:
    case.updated_at = _now()
    _STORE.set(case.case_id, case)
    return case


def _make_decision_record(
    stage_name: str,
    rule_id: str,
    decision: str,
    confidence: float,
    rationale: str,
    hitl_required: bool,
) -> SIUDecisionRecord:
    return SIUDecisionRecord(
        record_id=f"DR-{uuid.uuid4().hex[:10].upper()}",
        stage_name=stage_name,
        rule_id=rule_id,
        decision=decision,
        confidence=confidence,
        rationale=rationale,
        hitl_required=hitl_required,
        model_version=AGENT_VERSION,
        timestamp=_now(),
    )


# ───────────────────────────────────────────────────────────────────────────
# Signal extraction — maps S4A outputs + claim payload to typed FraudSignals
# ───────────────────────────────────────────────────────────────────────────

def _extract_signals(s4a: Dict[str, Any], claim: Dict[str, Any]) -> List[FraudSignal]:
    """Convert raw S4A outputs and FNOL intake fields into FraudSignal records.

    Each signal is self-contained: label, weight, active flag, detail string,
    and the blueprint rule that triggered it. Active signals sum to the
    composite risk score via _compute_risk_score().
    """
    signals: List[FraudSignal] = []

    # Pull commonly needed values once
    iso_match           = bool(claim.get("iso_match") or s4a.get("iso_match"))
    prior_claims        = int(claim.get("prior_claims_count") or s4a.get("prior_claims_count") or 0)
    tenure_days         = int(claim.get("policy_tenure_days") or s4a.get("policy_tenure_days") or 365)
    photo_count         = int(claim.get("photo_count") or s4a.get("photo_count") or 0)
    photo_quality       = float(claim.get("photo_quality_score") or s4a.get("photo_quality_score") or 1.0)
    attorney            = bool(claim.get("attorney_represented"))
    late_night          = bool(s4a.get("late_night_loss"))
    network_flag        = bool(s4a.get("network_ring_flag") or s4a.get("staged_accident_flag"))
    impact_severity     = float((claim.get("telematics") or {}).get("impact_severity_score") or s4a.get("impact_severity_score") or 0)
    injury_reported     = bool(claim.get("injury_reported"))
    injury_severity     = (claim.get("injury_severity") or "").upper()
    loss_desc           = (claim.get("loss_description") or "").lower()
    seed_fraud          = bool(claim.get("seed_fraud"))

    # ── ISO_CLAIMSEARCH ──────────────────────────────────────────────────
    label, wt, cat = _SIGNAL_DEFINITIONS["ISO_CLAIMSEARCH"]
    active = iso_match
    detail = (
        "ISO ClaimSearch returned a prior loss match within 24 months for the same claimant or VIN. "
        "Blueprint rule: prior loss match → fraud signal escalated; adjuster review mandatory."
        if active else "No ISO ClaimSearch match on file."
    )
    signals.append(FraudSignal(
        signal_id="ISO_CLAIMSEARCH", label=label, active=active,
        weight=wt, score_contribution=wt if active else 0.0,
        evidence_category=cat, detail=detail,
        rule_fired="S4A:ISO_CS_MATCH" if active else "",
    ))

    # ── FREQUENCY_ANOMALY ────────────────────────────────────────────────
    label, wt, cat = _SIGNAL_DEFINITIONS["FREQUENCY_ANOMALY"]
    # Blueprint: >2 prior claims in 36 months is a meaningful frequency
    active = prior_claims >= 2
    weight_adj = wt + 0.04 if prior_claims >= 4 else wt   # escalate for high repeat
    detail = (
        f"{prior_claims} prior claims in the last 36 months — frequency anomaly. "
        "Blueprint rule: high prior claim frequency → fraud signal elevated."
        if active else f"{prior_claims} prior claim(s) — within normal range."
    )
    signals.append(FraudSignal(
        signal_id="FREQUENCY_ANOMALY", label=label, active=active,
        weight=weight_adj, score_contribution=weight_adj if active else 0.0,
        evidence_category=cat, detail=detail,
        rule_fired="S4A:PRIOR_FREQ" if active else "",
    ))

    # ── COVERAGE_SEEKING ─────────────────────────────────────────────────
    label, wt, cat = _SIGNAL_DEFINITIONS["COVERAGE_SEEKING"]
    active = tenure_days < 90
    detail = (
        f"Policy tenure only {tenure_days} days at date of loss — suspected coverage seeking. "
        "POC threshold: < 90 days triggers this signal."
        if active else f"Policy tenure {tenure_days} days — no coverage-seeking indicator."
    )
    signals.append(FraudSignal(
        signal_id="COVERAGE_SEEKING", label=label, active=active,
        weight=wt, score_contribution=wt if active else 0.0,
        evidence_category=cat, detail=detail,
        rule_fired="S4A:SHORT_TENURE" if active else "",
    ))

    # ── DOCUMENTATION_GAP ────────────────────────────────────────────────
    label, wt, cat = _SIGNAL_DEFINITIONS["DOCUMENTATION_GAP"]
    low_quality = photo_quality < 0.50
    low_count   = photo_count < 2
    active = low_quality or low_count
    parts = []
    if low_quality:
        parts.append(f"photo quality score {photo_quality:.2f} (threshold 0.50)")
    if low_count:
        parts.append(f"only {photo_count} photo(s) submitted")
    detail = (
        "Insufficient damage documentation: " + "; ".join(parts) + ". "
        "Low documentation at FNOL is correlated with opportunistic fraud."
        if active else f"Documentation adequate: {photo_count} photos, quality {photo_quality:.2f}."
    )
    signals.append(FraudSignal(
        signal_id="DOCUMENTATION_GAP", label=label, active=active,
        weight=wt, score_contribution=wt if active else 0.0,
        evidence_category=cat, detail=detail,
        rule_fired="S4A:DOC_GAP" if active else "",
    ))

    # ── NARRATIVE_MISMATCH ───────────────────────────────────────────────
    label, wt, cat = _SIGNAL_DEFINITIONS["NARRATIVE_MISMATCH"]
    high_severity_words = any(w in loss_desc for w in [
        "severe", "critical", "unconscious", "paralys", "hospital", "surgery",
        "broken", "fracture", "icu", "emergency", "ambulance", "spine", "neck",
    ])
    # Blueprint rule: impactSeverityScore < 2.0 AND high-severity injury language
    mismatch = injury_reported and high_severity_words and impact_severity < 2.0
    # Also seed_fraud amplifies; or no telematics signal on high-dollar claim
    no_telem_high_loss = (
        not (claim.get("telematics") or {}).get("crash_alert_received", False)
        and float(claim.get("estimated_loss_usd") or 0) > 5_000
    )
    active = mismatch or (seed_fraud and injury_reported) or no_telem_high_loss
    # Blueprint weight +0.15 on mismatch
    weight_adj = wt + 0.15 if mismatch else wt
    detail_parts = []
    if mismatch:
        detail_parts.append(
            f"High-severity injury language in narrative but telematics impact score only "
            f"{impact_severity:.1f}/10 (threshold < 2.0). Blueprint rule: weight +0.15."
        )
    if no_telem_high_loss:
        detail_parts.append(
            f"Loss estimated at ${float(claim.get('estimated_loss_usd',0)):,.0f} "
            "with no telematics crash alert — event plausibility gap."
        )
    detail = " ".join(detail_parts) if detail_parts else (
        "No telematics-narrative mismatch detected."
    )
    signals.append(FraudSignal(
        signal_id="NARRATIVE_MISMATCH", label=label, active=active,
        weight=weight_adj, score_contribution=weight_adj if active else 0.0,
        evidence_category=cat, detail=detail,
        rule_fired="S4A:NARR_MISMATCH" if active else "",
    ))

    # ── LITIGATION_RISK ──────────────────────────────────────────────────
    label, wt, cat = _SIGNAL_DEFINITIONS["LITIGATION_RISK"]
    active = attorney
    detail = (
        "Claimant retained attorney representation at FNOL stage — elevated litigation "
        "risk and indicator of pre-planned claim strategy."
        if active else "No attorney representation at FNOL."
    )
    signals.append(FraudSignal(
        signal_id="LITIGATION_RISK", label=label, active=active,
        weight=wt, score_contribution=wt if active else 0.0,
        evidence_category=cat, detail=detail,
        rule_fired="S4A:ATTORNEY_FLAG" if active else "",
    ))

    # ── TIMING_ANOMALY ───────────────────────────────────────────────────
    label, wt, cat = _SIGNAL_DEFINITIONS["TIMING_ANOMALY"]
    # Check late-night from S4A output or derive from loss_datetime
    loss_dt_str = claim.get("loss_date_time") or ""
    late_night_derived = False
    if loss_dt_str:
        try:
            ldt = dt.datetime.fromisoformat(loss_dt_str.replace("Z", "+00:00"))
            late_night_derived = ldt.hour >= 22 or ldt.hour < 5
        except (ValueError, AttributeError):
            pass
    active = late_night or late_night_derived
    detail = (
        "Loss reported during late-night / early-morning hours (22:00–05:00) — "
        "temporal pattern associated with reduced-witness, single-vehicle fraud scenarios."
        if active else "Loss time within normal daylight hours."
    )
    signals.append(FraudSignal(
        signal_id="TIMING_ANOMALY", label=label, active=active,
        weight=wt, score_contribution=wt if active else 0.0,
        evidence_category=cat, detail=detail,
        rule_fired="S4A:TIMING" if active else "",
    ))

    # ── NETWORK_FLAG ─────────────────────────────────────────────────────
    label, wt, cat = _SIGNAL_DEFINITIONS["NETWORK_FLAG"]
    active = network_flag or bool(s4a.get("siu_referral"))
    detail = (
        "Network graph analysis detected potential staged accident ring indicators — "
        "claimant shares provider/attorney affiliation with previously fraud-flagged claims. "
        "Blueprint rule: NETWORK_FLAG triggers SIU referral regardless of composite score."
        if active else "No network ring or staged accident indicators detected."
    )
    signals.append(FraudSignal(
        signal_id="NETWORK_FLAG", label=label, active=active,
        weight=wt, score_contribution=wt if active else 0.0,
        evidence_category=cat, detail=detail,
        rule_fired="S4A:NETWORK_RING" if active else "",
    ))

    return signals


def _compute_risk_score(signals: List[FraudSignal]) -> float:
    """Deterministic composite risk score 0.0–1.0.

    Sum of active signal weights, clamped to [0.0, 1.0]. Network flag
    is a hard floor: if active, score is always ≥ FRAUD_BAND_CRITICAL_THRESHOLD
    per Blueprint §S4A rule 4.
    """
    raw = sum(s.score_contribution for s in signals)
    score = min(raw, 1.0)
    # Blueprint: network ring → mandatory CRITICAL regardless of composite
    if any(s.signal_id == "NETWORK_FLAG" and s.active for s in signals):
        score = max(score, FRAUD_BAND_CRITICAL_THRESHOLD + 0.01)
    return round(score, 4)


def _band_from_score(score: float) -> str:
    if score > FRAUD_BAND_CRITICAL_THRESHOLD:
        return "CRITICAL"
    if score >= FRAUD_BAND_HIGH_THRESHOLD:
        return "HIGH"
    if score >= FRAUD_BAND_MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"


def _dominant_category(signals: List[FraudSignal]) -> str:
    """Return the signal_id of the highest-weight active signal."""
    active = [s for s in signals if s.active]
    if not active:
        return "COVERAGE_SEEKING"
    return max(active, key=lambda s: s.score_contribution).signal_id


def _route_team(signals: List[FraudSignal]) -> str:
    dominant = _dominant_category(signals)
    return _TEAM_ROUTING.get(dominant, "Pattern Analysis Unit")


def _assign_investigator(case_id: str, team: str) -> Dict[str, Any]:
    """Deterministic round-robin assignment from the POC investigator pool.
    If team matches a specialist, prefer that investigator; fall back to index.
    """
    # Prefer investigator whose team matches routing
    for inv in _INVESTIGATORS:
        if inv["team"] == team:
            return dict(inv)
    # Fall back to deterministic modular assignment by case serial
    idx = int(uuid.UUID(case_id).int % len(_INVESTIGATORS))
    return dict(_INVESTIGATORS[idx])


def _build_evidence_items(signals: List[FraudSignal], claim: Dict[str, Any]) -> List[EvidenceItem]:
    """Construct initial evidence dossier from active signals."""
    items: List[EvidenceItem] = []
    now = _now()
    for sig in signals:
        if not sig.active:
            continue
        items.append(EvidenceItem(
            evidence_id=f"EV-{uuid.uuid4().hex[:8].upper()}",
            evidence_type=sig.evidence_category,
            description=sig.detail,
            source=f"Pipeline S4A · Rule {sig.rule_fired}",
            flagged=True,
            added_at=now,
            added_by="A12_AUTO",
        ))
    return items


# ───────────────────────────────────────────────────────────────────────────
# LLM referral memo — system + user prompt, deterministic fallback
# ───────────────────────────────────────────────────────────────────────────

_REFERRAL_SYSTEM = """You are the FNOL Intelligence Platform SIU Case Builder (A12), generating
SIU referral memos for P&C auto insurance claims. Your output is a formal investigation
referral memo that will be reviewed by a licensed SIU investigator.

Rules:
- Output ONLY the referral memo text. No preamble, no markdown, no backticks.
- Never state a conclusion of guilt — only describe the signals and recommend investigation.
- Use formal claims-industry language aligned with NAIC Model Bulletin §IV on AI.
- Every dollar figure, date, and claim ID you include MUST come from the provided claim data.
- The memo must contain: (1) referral header, (2) claim summary, (3) fraud risk assessment,
  (4) signal breakdown, (5) evidence inventory, (6) recommended investigative steps,
  (7) payment hold status, (8) compliance attestation.
- Length: 400–600 words. No section is optional."""


def _build_referral_prompt(case: SIUCase) -> str:
    snap = case.claim_snapshot
    active_signals = [s for s in case.signals if s.active]
    signal_text = "\n".join(
        f"  - {s.label} (weight {s.weight:.2f}): {s.detail[:120]}"
        for s in active_signals
    )
    evidence_text = "\n".join(
        f"  - [{ev.evidence_type}] {ev.description[:100]}"
        for ev in case.evidence_items
    )
    adjuster_notes = case.adjuster_notes.strip() or "(none)"
    return (
        f"CASE ID: {case.case_id}\n"
        f"CLAIM ID: {case.claim_id}\n"
        f"POLICY NUMBER: {snap.get('policy_number', '—')}\n"
        f"LOSS CAUSE: {snap.get('loss_cause', '—')}\n"
        f"LOSS LOCATION: {snap.get('loss_location', '—')}\n"
        f"ESTIMATED LOSS: ${float(snap.get('estimated_loss_usd', 0)):,.0f}\n"
        f"SIU RISK SCORE: {case.fraud_risk_score:.2f} / 1.00 ({case.fraud_band})\n"
        f"PAYMENT HOLD: {'ACTIVE' if case.payment_hold_flag else 'NOT ACTIVE'}\n"
        f"ASSIGNED TEAM: {case.siu_team}\n"
        f"SLA DEADLINE: {case.sla_deadline}\n\n"
        f"ACTIVE FRAUD SIGNALS:\n{signal_text or '  (none active)'}\n\n"
        f"EVIDENCE INVENTORY:\n{evidence_text or '  (none yet)'}\n\n"
        f"ADJUSTER NOTES:\n  {adjuster_notes}\n\n"
        "Generate the formal SIU referral memo based on the above data."
    )


def _deterministic_referral(case: SIUCase) -> str:
    """Template-based fallback when LLM returns mock/template output."""
    snap = case.claim_snapshot
    now_date = dt.datetime.now(dt.timezone.utc).strftime("%B %d, %Y")
    active = [s for s in case.signals if s.active]
    flags = "\n".join(
        f"{i+1}. [{s.evidence_category}] {s.label}: {s.detail[:110]}"
        for i, s in enumerate(active)
    )
    evidence_list = "\n".join(
        f"  • [{ev.evidence_type}] {ev.description[:100]}"
        for ev in case.evidence_items
    )
    steps = (
        "1. Assign field investigator for site visit and scene documentation\n"
        "2. Request recorded statement from insured and all claimants\n"
        "3. Order Independent Vehicle Inspection (IVI) from approved appraiser\n"
        "4. Run full ISO ClaimSearch on all parties (insured, claimant, attorney)\n"
        "5. Obtain police report / incident report if applicable\n"
        "6. Social media OSINT review (public-facing content)\n"
        "7. Review repair facility / body shop records\n"
        "8. Network graph expansion: identify shared providers, attorneys, claimants\n"
    )
    if any(s.signal_id == "NETWORK_FLAG" and s.active for s in case.signals):
        steps += "9. Organized fraud ring analysis — coordinate with NICB\n"
    hold_line = (
        "PAYMENT HOLD STATUS: ACTIVE — no disbursement authorized until SIU clearance "
        "and adjuster confirmation per Blueprint §siu-hold-subprocess."
        if case.payment_hold_flag else
        "PAYMENT HOLD STATUS: Not active — enhanced review in progress."
    )
    return (
        f"SIU REFERRAL MEMORANDUM\n"
        f"═══════════════════════════════════════════\n"
        f"Referral Reference: {case.referral_reference}\n"
        f"Case ID:            {case.case_id}\n"
        f"Claim ID:           {case.claim_id}\n"
        f"Policy Number:      {snap.get('policy_number', '—')}\n"
        f"Date Generated:     {now_date}\n"
        f"Assigned Team:      {case.siu_team}\n"
        f"Investigator:       {case.investigator.get('name', '—')} "
        f"({case.investigator.get('investigator_id', '—')})\n"
        f"SLA Deadline:       {case.sla_deadline}\n"
        f"Status:             {case.status}\n\n"
        f"I. CLAIM SUMMARY\n"
        f"───────────────\n"
        f"Loss Cause:         {snap.get('loss_cause', '—')}\n"
        f"Loss Location:      {snap.get('loss_location', '—')}\n"
        f"Estimated Loss:     ${float(snap.get('estimated_loss_usd', 0)):,.0f}\n"
        f"Vehicle ACV:        ${float(snap.get('vehicle_acv_usd', 0)):,.0f}\n"
        f"Reporter:           [REDACTED per FCRA §615]\n\n"
        f"II. FRAUD RISK ASSESSMENT\n"
        f"────────────────────────\n"
        f"SIU Risk Score:     {case.fraud_risk_score:.2f} / 1.00\n"
        f"Fraud Band:         {case.fraud_band}\n"
        f"{hold_line}\n\n"
        f"III. ACTIVE FRAUD SIGNALS\n"
        f"──────────────────────────\n"
        f"{flags or '(No specific signals — baseline referral)'}\n\n"
        f"IV. EVIDENCE INVENTORY\n"
        f"──────────────────────\n"
        f"{evidence_list or '  (No additional evidence yet — awaiting field investigation)'}\n\n"
        f"{'V. ADJUSTER NOTES' if case.adjuster_notes.strip() else ''}\n"
        f"{'──────────────────' if case.adjuster_notes.strip() else ''}\n"
        f"{case.adjuster_notes.strip() if case.adjuster_notes.strip() else ''}\n"
        f"\nVI. RECOMMENDED INVESTIGATIVE STEPS\n"
        f"─────────────────────────────────────\n"
        f"{steps}\n"
        f"VII. COMPLIANCE ATTESTATION\n"
        f"────────────────────────────\n"
        f"This referral is generated by the FNOL Intelligence Platform A12 SIU Case "
        f"Builder in accordance with NAIC Model Bulletin §IV (Use of Artificial "
        f"Intelligence Systems in Insurance). All AI signals are advisory; the assigned "
        f"SIU investigator exercises independent professional judgment. This referral and "
        f"all underlying AI signals are documented in the Platform Decision Log for DOI "
        f"audit defensibility. Adverse action (if any) will trigger an FCRA §615 notice.\n\n"
        f"Generated by: FNOL Intelligence Platform · A12 SIU Case Builder v{AGENT_VERSION}\n"
        f"Accenture Insurance Claims Intelligence Practice"
    )


# ───────────────────────────────────────────────────────────────────────────
# Public API
# ───────────────────────────────────────────────────────────────────────────

def open_case(
    claim_id: str,
    pipeline_trace: Dict[str, Any],
    claim_payload: Optional[Dict[str, Any]] = None,
) -> SIUCase:
    """Open an SIU case from a pipeline trace.

    Called by the API route POST /api/v1/fnol/siu/open. Reads S4A stage
    outputs from the pipeline trace, extracts fraud signals, computes
    composite risk, routes to SIU team, and stores the case.

    Idempotent on claim_id — if a case already exists for this claim,
    return the existing case without creating a duplicate.

    Args:
        claim_id: The claim being referred.
        pipeline_trace: Full pipeline trace dict (from _PIPELINE_TRACES store).
        claim_payload: Original intake payload (from pipeline_trace["claim_payload"]).
                       Falls back to pipeline_trace["claim_record"] if omitted.

    Raises:
        ValueError: If fraud band is LOW or MEDIUM (claim not SIU-eligible).
        KeyError: If S4A stage is missing from the pipeline trace.
    """
    # Idempotency: return existing case for this claim
    existing = get_case_by_claim(claim_id)
    if existing:
        return existing

    # Extract S4A outputs from pipeline trace
    stages = {s.get("stage_id"): s for s in pipeline_trace.get("stages", [])}
    s4a_stage = stages.get("S4A")
    if not s4a_stage:
        raise KeyError(f"S4A stage not found in pipeline trace for claim {claim_id}")

    s4a_out: Dict[str, Any] = s4a_stage.get("outputs") or {}

    # Resolve claim payload
    payload: Dict[str, Any] = dict(
        claim_payload
        or pipeline_trace.get("claim_payload")
        or pipeline_trace.get("claim_record")
        or {}
    )
    payload["claim_id"] = claim_id

    # ── Signal extraction ────────────────────────────────────────────────
    signals = _extract_signals(s4a_out, payload)
    score   = _compute_risk_score(signals)
    band    = _band_from_score(score)

    # ── Eligibility gate ─────────────────────────────────────────────────
    # Blueprint: only HIGH and CRITICAL trigger A12. MEDIUM/LOW stay in S4A.
    if band not in ("HIGH", "CRITICAL"):
        raise ValueError(
            f"Claim {claim_id} fraud band is {band} — A12 SIU Case Builder "
            f"requires HIGH (≥ {FRAUD_BAND_HIGH_THRESHOLD}) or CRITICAL "
            f"(> {FRAUD_BAND_CRITICAL_THRESHOLD}). This claim remains on the "
            f"standard adjuster enhanced-review track."
        )

    # ── Team routing + investigator assignment ────────────────────────────
    team         = _route_team(signals)
    case_id      = f"SIU-{uuid.uuid4().hex[:10].upper()}"
    investigator = _assign_investigator(case_id, team)
    sla_hours    = investigator.get("sla_hours", 4)

    # ── Evidence dossier ─────────────────────────────────────────────────
    evidence = _build_evidence_items(signals, payload)

    # ── Payment hold ─────────────────────────────────────────────────────
    payment_hold = (
        band == "CRITICAL"
        or bool(s4a_out.get("payment_hold_flag"))
        or bool(s4a_out.get("siu_referral"))
    )

    # ── Triggered categories ─────────────────────────────────────────────
    triggered = [s.signal_id for s in signals if s.active]

    # ── Claim snapshot (PII-scrubbed for the referral-memo LLM path) ─────
    snapshot = {
        "policy_number":     payload.get("policy_number"),
        "loss_cause":        payload.get("loss_cause"),
        "loss_location":     payload.get("loss_location"),
        "estimated_loss_usd": payload.get("estimated_loss_usd"),
        "vehicle_acv_usd":   payload.get("vehicle_acv_usd"),
        "vehicle_class":     payload.get("vehicle_class"),
        "policy_tenure_days": payload.get("policy_tenure_days"),
        "prior_claims_count": payload.get("prior_claims_count"),
        "drivable_indicator": payload.get("drivable_indicator"),
        "injury_reported":   payload.get("injury_reported"),
        "injury_severity":   payload.get("injury_severity"),
    }

    # ── Decision Records ─────────────────────────────────────────────────
    dr_open = _make_decision_record(
        stage_name="SIU Case Opening",
        rule_id=f"A12:OPEN_{band}",
        decision=f"SIU_CASE_OPENED_{band}",
        confidence=round(score, 3),
        rationale=(
            f"Composite fraud risk score {score:.3f} exceeds {band} threshold "
            f"({FRAUD_BAND_CRITICAL_THRESHOLD if band == 'CRITICAL' else FRAUD_BAND_HIGH_THRESHOLD:.2f}). "
            f"Active signals: {', '.join(triggered) or 'none'}. "
            f"Routed to {team}. "
            f"{'Payment hold activated per Blueprint §siu-hold-subprocess.' if payment_hold else 'Enhanced review — no payment hold.'}"
        ),
        hitl_required=True,  # Always HITL for SIU (Blueprint §siu-hold-subprocess)
    )
    dr_assign = _make_decision_record(
        stage_name="SIU Investigator Assignment",
        rule_id="A12:INVESTIGATOR_ASSIGN",
        decision=f"ASSIGNED_{investigator['investigator_id']}",
        confidence=0.90,
        rationale=(
            f"Dominant fraud category '{_dominant_category(signals)}' maps to '{team}'. "
            f"Investigator {investigator['name']} ({investigator['investigator_id']}) assigned "
            f"from POC pool. SLA: {sla_hours}h per Blueprint §siu-hold-subprocess."
        ),
        hitl_required=False,
    )

    case = SIUCase(
        case_id=case_id,
        claim_id=claim_id,
        fraud_risk_score=score,
        fraud_band=band,
        payment_hold_flag=payment_hold,
        triggered_categories=triggered,
        siu_team=team,
        investigator=investigator,
        status="OPEN",
        opened_at=_now(),
        sla_deadline=_sla_deadline(sla_hours),
        updated_at=_now(),
        signals=signals,
        evidence_items=evidence,
        decisions=[dr_open, dr_assign],
        claim_snapshot=snapshot,
    )
    return _put(case)


def add_evidence(
    case_id: str,
    evidence_type: str,
    description: str,
    source: str = "ADJUSTER",
) -> SIUCase:
    """Append an evidence item to the case dossier.

    Args:
        case_id: SIU case identifier.
        evidence_type: DATABASE | PHOTO | STATEMENT | ANALYSIS | DOCUMENT.
        description: Free-text description of the evidence.
        source: Origin of the evidence (e.g. "Field investigator", "ISO API").

    Raises:
        KeyError: If case_id not found.
    """
    case = _STORE.get(case_id)
    if case is None:
        raise KeyError(f"SIU case {case_id} not found")
    valid_types = {"DATABASE", "PHOTO", "STATEMENT", "ANALYSIS", "DOCUMENT", "OTHER"}
    ev_type = (evidence_type or "DOCUMENT").upper()
    if ev_type not in valid_types:
        ev_type = "DOCUMENT"
    item = EvidenceItem(
        evidence_id=f"EV-{uuid.uuid4().hex[:8].upper()}",
        evidence_type=ev_type,
        description=(description or "").strip()[:500],
        source=(source or "ADJUSTER").strip()[:100],
        flagged=True,
        added_at=_now(),
        added_by="ADJUSTER",
    )
    case.evidence_items.append(item)
    dr = _make_decision_record(
        stage_name="Evidence Added",
        rule_id="A12:EVIDENCE_ADD",
        decision="EVIDENCE_ITEM_ADDED",
        confidence=1.0,
        rationale=f"Adjuster added [{ev_type}] evidence item: {description[:80]}",
        hitl_required=False,
    )
    case.decisions.append(dr)
    return _put(case)


def save_notes(case_id: str, notes: str) -> SIUCase:
    """Persist adjuster notes to the case record.

    Raises:
        KeyError: If case_id not found.
    """
    case = _STORE.get(case_id)
    if case is None:
        raise KeyError(f"SIU case {case_id} not found")
    case.adjuster_notes = (notes or "").strip()[:2000]
    return _put(case)


def generate_referral(case_id: str) -> SIUCase:
    """Generate the SIU referral memo via LLM, with deterministic fallback.

    LLM is used ONLY for the narrative draft. All dollar figures, signal weights,
    claim fields, and investigator assignments come from deterministic logic.
    Falls back to _deterministic_referral() when provider is mock or LLM returns
    template-shaped output (per A11 letter generation pattern).

    Raises:
        KeyError: If case_id not found.
    """
    case = _STORE.get(case_id)
    if case is None:
        raise KeyError(f"SIU case {case_id} not found")

    ref_ref = f"SIU-REF-{case.claim_id}-{uuid.uuid4().hex[:6].upper()}"
    case.referral_reference = ref_ref

    # Scrub PII from claim snapshot before sending to LLM
    safe_snap = dict(case.claim_snapshot)
    # Note: reporter_name and reporter_phone are NOT in snapshot (by design).
    # loss_description is also excluded — it may contain PII not redacted
    # by the time A12 sees it. Referral is drafted from structured fields only.

    try:
        user_prompt = _build_referral_prompt(case)
        result = llm_complete(_REFERRAL_SYSTEM, user_prompt, max_tokens=800)
        memo = (result.text or "").strip()

        # Detect template-shaped / mock output and fall back
        template_indicators = [
            "[INSURED_NAME]", "[CLAIM_NUMBER]", "{claim", "<<", "PLACEHOLDER",
        ]
        if not memo or any(t in memo for t in template_indicators) or len(memo) < 100:
            memo = _deterministic_referral(case)
    except Exception:
        memo = _deterministic_referral(case)

    case.referral_memo = memo
    case.referral_generated_at = _now()

    # Update status to UNDER_INVESTIGATION once referral is generated
    if case.status == "OPEN":
        case.status = "UNDER_INVESTIGATION"

    dr = _make_decision_record(
        stage_name="Referral Memo Generated",
        rule_id="A12:REFERRAL_GENERATED",
        decision="REFERRAL_MEMO_READY",
        confidence=0.92,
        rationale=(
            f"SIU referral memo generated via LLM (provider: {resolve_provider()}) "
            f"with deterministic fallback guard. Reference: {ref_ref}. "
            f"Memo length: {len(memo)} chars. Status → UNDER_INVESTIGATION. "
            f"NAIC Model Bulletin §IV: AI-generated memo requires SIU investigator review "
            f"before transmission to external SIU platform."
        ),
        hitl_required=True,  # Investigator must review before transmitting
    )
    case.decisions.append(dr)
    return _put(case)


def close_case(
    case_id: str,
    disposition: str,
    investigator_notes: str = "",
) -> SIUCase:
    """Close the SIU case with a disposition decision.

    Disposition options (Blueprint §siu-hold-subprocess outcome):
      - CLEARED: SIU cleared — releases payment hold, resumes settlement
      - CONFIRMED_FRAUD: Fraud confirmed — claim denial + law enforcement referral
      - CLOSED_INCONCLUSIVE: Insufficient evidence — adjuster makes final call

    Args:
        case_id: SIU case identifier.
        disposition: One of VALID_DISPOSITIONS.
        investigator_notes: Free-text notes from the SIU investigator.

    Raises:
        KeyError: If case_id not found.
        ValueError: If disposition is not one of VALID_DISPOSITIONS.
    """
    case = _STORE.get(case_id)
    if case is None:
        raise KeyError(f"SIU case {case_id} not found")
    disposition = (disposition or "").upper().strip()
    if disposition not in VALID_DISPOSITIONS:
        raise ValueError(
            f"Invalid disposition '{disposition}'. "
            f"Valid values: {', '.join(sorted(VALID_DISPOSITIONS))}"
        )

    case.disposition = disposition
    case.disposition_notes = (investigator_notes or "").strip()[:2000]
    case.disposition_at = _now()
    case.status = disposition  # Status collapses to disposition value

    # Release payment hold if cleared
    if disposition == "CLEARED":
        case.payment_hold_flag = False
        case.hold_released_at = _now()

    dr = _make_decision_record(
        stage_name="SIU Case Closure",
        rule_id=f"A12:CLOSE_{disposition}",
        decision=disposition,
        confidence=1.0,   # Human-confirmed disposition
        rationale=(
            f"SIU case closed with disposition: {disposition}. "
            + (f"Payment hold released at {case.hold_released_at}. "
               if disposition == "CLEARED" else "")
            + (f"Claim denial + law enforcement referral triggered. "
               if disposition == "CONFIRMED_FRAUD" else "")
            + (f"Adjuster decision required on inconclusive finding. "
               if disposition == "CLOSED_INCONCLUSIVE" else "")
            + f"Investigator notes: {investigator_notes[:80] or '(none)'}. "
            f"FCRA §615 adverse action notice: "
            f"{'required' if disposition in ('CONFIRMED_FRAUD', 'CLOSED_INCONCLUSIVE') else 'not applicable'}."
        ),
        hitl_required=(disposition != "CLEARED"),
    )
    case.decisions.append(dr)
    return _put(case)


# ───────────────────────────────────────────────────────────────────────────
# Read API
# ───────────────────────────────────────────────────────────────────────────

def get_case(case_id: str) -> Optional[SIUCase]:
    return _STORE.get(case_id)


def get_case_by_claim(claim_id: str) -> Optional[SIUCase]:
    for case in _STORE.values():
        if isinstance(case, SIUCase) and case.claim_id == claim_id:
            return case
    return None


def list_cases(limit: int = 50) -> List[Dict[str, Any]]:
    """Return a summary list of SIU cases, newest first, up to limit."""
    cases = [c for c in _STORE.values() if isinstance(c, SIUCase)]
    cases.sort(key=lambda c: c.opened_at, reverse=True)
    result = []
    for case in cases[:limit]:
        result.append({
            "case_id":           case.case_id,
            "claim_id":          case.claim_id,
            "fraud_band":        case.fraud_band,
            "fraud_risk_score":  case.fraud_risk_score,
            "payment_hold_flag": case.payment_hold_flag,
            "status":            case.status,
            "siu_team":          case.siu_team,
            "investigator_name": case.investigator.get("name"),
            "triggered_categories": case.triggered_categories,
            "sla_deadline":      case.sla_deadline,
            "opened_at":         case.opened_at,
            "disposition":       case.disposition,
            "referral_reference": case.referral_reference,
            "evidence_count":    len(case.evidence_items),
        })
    return result


def _serialise_case(case: SIUCase) -> Dict[str, Any]:
    """Convert SIUCase dataclass to a JSON-serialisable dict."""
    d = asdict(case)
    return d


def health() -> Dict[str, Any]:
    cases = list(_STORE.values())
    siu_cases = [c for c in cases if isinstance(c, SIUCase)]
    return {
        "agent": AGENT_NAME,
        "agent_id": AGENT_ID,
        "version": AGENT_VERSION,
        "cases_in_store": len(siu_cases),
        "llm_provider": resolve_provider(),
    }
