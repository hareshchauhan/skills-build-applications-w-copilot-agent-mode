"""
FNOL Intelligence Platform — Workflow Engine
============================================
Orchestrates the full 9-stage Blueprint pipeline across 8 specialized agents.

Stages (per Blueprint V2 §03 Process Blueprint, L2 maturity):
  S0   Pre-FNOL / Crash Detection         (FNOL Intake Agent)
  S1   FNOL Capture & Validation          (FNOL Intake Agent)
  S2   Coverage Verification & Reservation (Coverage & Liability Agent)
  S3   Triage, Complexity, Assignment     (Triage & Assignment Agent)
  S4A  Fraud & Anomaly Detection          (Fraud Detection Agent)        [parallel w/ S4B]
  S4B  AI-Powered Damage Assessment       (Damage Estimation Agent)      [parallel w/ S4A]
  S5   BI Evaluation & Liability          (Coverage & Liability Agent + BI Model)
  S6   Settlement & Payment Authorization (Settlement Agent)
  S7   Subrogation & Recovery             (Subrogation Agent)

Every stage emits a Decision Record (auditable trail) and a deterministic
status update.  All thresholds are POC defaults — they MUST be calibrated
against a carrier's own loss data and approved by claims leadership before
production deployment.

Threading: S4A and S4B execute concurrently via concurrent.futures.

POC values vs. production:
  - Authority matrix dollar thresholds are illustrative.
  - Fraud thresholds (0.50 / 0.75) are industry-informed starting points.
  - STP threshold ($15k PD) is illustrative and must be validated by state.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from fnol_llm_adapter import complete as llm_complete, resolve_provider
import fnol_iso_adapter as iso_adapter
import fnol_network_graph_adapter as ng_adapter
from fnol_sor_adapter import get_sor_adapter
from fnol_claim import (
    Claim,
    LossCauseCd, LossTypeCd,
    normalise_loss_cause_cd, derive_loss_type_cd,
    CoverageCd, AcvSourceCd, RorTriggerCd, ClaimantCoverage,
)


# ───────────────────────────────────────────────────────────────────────────
# POC THRESHOLDS — externalize to config in production
# ───────────────────────────────────────────────────────────────────────────

THRESHOLDS = {
    "stp_property_damage_cap_usd": 15_000,
    "fraud_review_band":  0.50,
    "fraud_hold_band":    0.75,
    "triage_stp_ceiling": 3.0,
    "triage_complex_floor": 6.0,
    "stp_confidence_floor": 0.85,
    "total_loss_ratio": 0.75,
    "subro_score_arbitration_floor": 0.70,
    "subro_recovery_floor_usd": 2_500,
    "subro_open_loss_floor_usd": 1_000,
    "subro_fault_floor": 0.50,
    "photo_quality_floor": 0.60,
    "photo_count_floor": 4,
    "litigation_propensity_floor": 0.60,
}


# ───────────────────────────────────────────────────────────────────────────
# Decision Record
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class DecisionRecord:
    stage_id: str
    stage_name: str
    agent: str
    decision: str
    confidence: float
    rationale: str
    inputs_hash: str
    model_version: str
    timestamp: str
    hitl_required: bool = False
    overrides: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class StageResult:
    stage_id: str
    stage_name: str
    agent: str
    status: str                        # ok | warning | hitl | hold | skipped | error
    started_at: str
    completed_at: str
    duration_ms: int
    outputs: Dict[str, Any] = field(default_factory=dict)
    decisions: List[DecisionRecord] = field(default_factory=list)
    advisories: List[str] = field(default_factory=list)
    error: Optional[str] = None


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def _hash(payload: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]

def _safe_get(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _parse_iso_dt(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp tolerating trailing `Z` and missing
    seconds. Returns a timezone-aware datetime (UTC for naive inputs)."""
    if not s:
        return None
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # Tolerate "YYYY-MM-DDTHH:MM" (no seconds) and "YYYY-MM-DD" date-only.
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_policy_in_force(in_force_from: Optional[str], in_force_to: Optional[str],
                         loss_date_time: Optional[str]) -> bool:
    """Datetime-aware policy-in-force check. The legacy string-lexicographic
    compare was fragile across `Z` vs `+00:00`, missing-seconds, and date-only
    formats — and could silently mis-order them, mis-deciding coverage."""
    loss_dt = _parse_iso_dt(loss_date_time)
    start_dt = _parse_iso_dt(in_force_from)
    end_dt = _parse_iso_dt(in_force_to)
    if loss_dt is None or start_dt is None or end_dt is None:
        return False
    return start_dt <= loss_dt <= end_dt


# ───────────────────────────────────────────────────────────────────────────
# Stage 0 — Pre-FNOL / Crash Detection
# ───────────────────────────────────────────────────────────────────────────

def stage_s0_pre_fnol(claim: Claim, context: Dict[str, Any]) -> StageResult:
    t0 = time.time()
    started = _utcnow_iso()
    tel = claim.telematics
    outputs: Dict[str, Any] = {}
    advisories: List[str] = []

    if tel is None:
        return StageResult("S0", "Pre-FNOL / Crash Detection", "FNOL Intake Agent",
                           "skipped", started, _utcnow_iso(),
                           int((time.time() - t0) * 1000),
                           {"reason": "no telematics payload provided"})

    fired = tel.crash_alert_received
    delta_v = tel.delta_v_mph
    impact_severity = tel.impact_severity_score
    airbag = tel.airbag_deployed
    consent = tel.consent_given

    if not fired:
        return StageResult("S0", "Pre-FNOL / Crash Detection", "FNOL Intake Agent",
                           "skipped", started, _utcnow_iso(),
                           int((time.time() - t0) * 1000),
                           {"reason": "no telematics crash alert present"})

    # ── ACORD Gap 6 — Granular consent gate ──────────────────────────────
    # telematics_data_scope takes precedence over binary consent_given.
    # ai_usable is True for FULL and IMPACT_ONLY scopes; False for
    # LOCATION_ONLY and NONE. Falls back to consent_given when scope absent.
    ai_usable = getattr(tel, "ai_usable", consent)  # property on extended model
    crash_source = getattr(tel, "crash_notification_source_cd", None)
    crash_source_val = (crash_source.value
                        if hasattr(crash_source, "value")
                        else str(crash_source or "UNKNOWN"))
    data_scope = getattr(tel, "telematics_data_scope", None)
    data_scope_val = (data_scope.value
                      if hasattr(data_scope, "value")
                      else str(data_scope or "NONE"))
    oem_event_id = getattr(tel, "oem_event_id", None)

    if not ai_usable:
        advisories.append(
            f"telematics data scope '{data_scope_val}' — "
            "telematics signal excluded from AI inputs (consent gate active)"
        )

    high_severity = impact_severity >= 7.0 or (delta_v >= 25 and airbag)
    outputs.update({
        "loss_event_id":             f"LE-{uuid.uuid4().hex[:8].upper()}",
        "high_severity_flag":        high_severity,
        "delta_v_mph":               delta_v,
        "impact_severity_score":     impact_severity,
        "airbag_deployed":           airbag,
        "telematics_used_in_ai":     ai_usable,       # Gap 6: scope-aware
        "crash_notification_source": crash_source_val, # Gap 6: new
        "telematics_data_scope":     data_scope_val,   # Gap 6: new
        "oem_event_id":              oem_event_id,     # Gap 6: new
    })
    # Surface location when scope permits (FULL or LOCATION_ONLY)
    if getattr(tel, "location_available", False):
        outputs["loss_location_lat"] = tel.location_lat
        outputs["loss_location_lon"] = tel.location_lon

    decisions = [DecisionRecord(
        "S0", "Pre-FNOL", "FNOL Intake Agent",
        "HIGH_SEVERITY_PREASSIGN" if high_severity else "STANDARD_PREFILL",
        0.95 if high_severity else 0.80,
        f"impact_severity={impact_severity}, deltaV={delta_v}, airbag={airbag}, "
        f"source={crash_source_val}, scope={data_scope_val}, ai_usable={ai_usable}",
        _hash(tel.model_dump()), "rules-v1", _utcnow_iso(),
    )]
    return StageResult("S0", "Pre-FNOL / Crash Detection", "FNOL Intake Agent",
                       "ok", started, _utcnow_iso(), int((time.time() - t0) * 1000),
                       outputs, decisions, advisories)


# ───────────────────────────────────────────────────────────────────────────
# Stage 1 — FNOL Capture & Validation
# ───────────────────────────────────────────────────────────────────────────

REQUIRED_INTAKE_FIELDS = (
    "policy_number", "loss_date_time", "loss_location", "loss_cause",
    "loss_description", "reporter_name", "reporter_phone",
)

def stage_s1_fnol_capture(claim: Claim, context: Dict[str, Any]) -> StageResult:
    t0 = time.time()
    started = _utcnow_iso()
    sor = get_sor_adapter()
    advisories: List[str] = []
    claim_d = claim.model_dump()
    missing = [k for k in REQUIRED_INTAKE_FIELDS if not claim_d.get(k)]

    policy = sor.lookup_policy(claim.policy_number or "")
    in_force = False
    if policy:
        in_force = _is_policy_in_force(
            policy.get("in_force_from"),
            policy.get("in_force_to"),
            claim.loss_date_time,
        )

    intake_quality = max(0.0, 1.0 - 0.1 * len(missing))
    nlp_injury_signal = bool(re.search(
        r"\b(injur|hurt|pain|hospital|ambulance|broken|fracture|whiplash|bleed)\b",
        (claim.loss_description or "").lower()
    ))
    injury_reported = bool(claim.injury_reported) or nlp_injury_signal

    # ── ACORD Gap 5 — ACV sentinel check ─────────────────────────────────
    # effective_acv_usd returns None when vehicle_acv_usd is None or 0.
    # Emit advisory so downstream S4B guard fires correctly; do NOT supply
    # a placeholder value — an honest None is safer than a wrong number.
    acv = claim.effective_acv_usd  # type: ignore[attr-defined]
    if acv is None:
        advisories.append(
            "vehicle_acv_usd not captured at intake — "
            "TL determination will be deferred to S4B/A11 adjuster review. "
            "Set acv_source_cd=CLAIMANT if insured provides ACV during intake."
        )

    # ── ACORD Gap 5 — Intake-visible ROR triggers ─────────────────────────
    # Stamp ror_trigger_cds with anything detectable from the intake payload
    # before S2 runs. S2 adds further triggers (EXCLUSION, POLICY_LAPSE) from
    # the policy snapshot. Using setattr because validate_assignment=False.
    _ror_triggers: List[str] = [
        t.value if hasattr(t, "value") else str(t)
        for t in (claim.ror_trigger_cds or [])  # type: ignore[attr-defined]
    ]
    if claim.attorney_represented and RorTriggerCd.ATTORNEY_INVOLVED.value not in _ror_triggers:
        _ror_triggers.append(RorTriggerCd.ATTORNEY_INVOLVED.value)
    _pax_count = len(getattr(claim, "passengers", []))
    _party_count = len(getattr(claim, "parties", []))
    if (_pax_count + _party_count) > 2 and RorTriggerCd.MULTI_CLAIMANT.value not in _ror_triggers:
        _ror_triggers.append(RorTriggerCd.MULTI_CLAIMANT.value)
    if _ror_triggers:
        setattr(claim, "ror_trigger_cds",
                [RorTriggerCd(t) for t in _ror_triggers
                 if t in RorTriggerCd._value2member_map_])

    # ── ACORD Gap 3 — Derive coded loss fields at intake ─────────────────
    if claim.loss_cause_cd is None:
        _cause_cd = normalise_loss_cause_cd(claim.loss_cause)
        setattr(claim, "loss_cause_cd", _cause_cd)
    else:
        _cause_cd = claim.loss_cause_cd

    if claim.loss_type_cd is None:
        _third_party = bool(
            claim.third_party_carrier or claim.third_party_policy_number
            or getattr(claim, "other_vehicle", None)
        )
        _type_cd = derive_loss_type_cd(
            _cause_cd,
            injury_reported=injury_reported,
            third_party_present=_third_party,
        )
        setattr(claim, "loss_type_cd", _type_cd)
    else:
        _type_cd = claim.loss_type_cd
    if missing:
        advisories.append(f"missing fields: {missing}")
        status = "hitl"
    if not in_force:
        advisories.append("policy not in force at loss_date_time — coverage dispute pathway")
        status = "warning"

    outputs = {
        "policy_in_force": in_force,
        "fnol_status": "DISPUTE" if not in_force else ("VALIDATED" if not missing else "INCOMPLETE"),
        "intake_quality_score": round(intake_quality, 2),
        "injury_reported": injury_reported,
        "duplicate_flag": False,
        "doi_acknowledgement_dispatched": True,
        "acknowledgement_timestamp": _utcnow_iso(),
        "policy_snapshot": policy,
        # ACORD Gap 3 — coded loss fields
        "loss_cause_cd": _cause_cd.value if hasattr(_cause_cd, "value") else str(_cause_cd),
        "loss_type_cd":  _type_cd.value  if hasattr(_type_cd,  "value") else str(_type_cd),
        # ACORD Gap 5 — coverage & financial fields
        "vehicle_acv_usd":    acv,                         # None = MISSING (honest sentinel)
        "acv_source_cd":      (claim.acv_source_cd.value   # type: ignore[attr-defined]
                               if hasattr(getattr(claim, "acv_source_cd", None), "value")
                               else str(getattr(claim, "acv_source_cd", "MISSING"))),
        "ror_trigger_cds":    [t.value if hasattr(t, "value") else str(t)
                               for t in getattr(claim, "ror_trigger_cds", [])],
        "claimant_coverage_count": len(getattr(claim, "claimant_asserted_coverages", [])),
    }
    decisions = [DecisionRecord(
        "S1", "FNOL Capture", "FNOL Intake Agent",
        outputs["fnol_status"], intake_quality,
        f"missing={missing}, in_force={in_force}, injury={injury_reported}",
        _hash(claim_d), "rules-v1", _utcnow_iso(),
        hitl_required=(status == "hitl"),
    )]
    return StageResult("S1", "FNOL Capture & Validation", "FNOL Intake Agent",
                       status, started, _utcnow_iso(), int((time.time() - t0) * 1000),
                       outputs, decisions, advisories)


# ───────────────────────────────────────────────────────────────────────────
# Stage S1A — Document Assist & Intelligent Classification
# ───────────────────────────────────────────────────────────────────────────

def stage_s1a_doc_assist(claim: Claim, context: Dict[str, Any]) -> StageResult:
    """S1-A: Document Assist & Intelligent Classification.

    Runs immediately after S1 FNOL Capture.  Processes any documents bundled
    with the intake payload through multi-modal LLM classification, OCR field
    extraction, quality scoring (< 0.60 → re-submission request), SOR task
    auto-creation, and priority alert dispatch for litigation documents.

    In the POC the standard FNOL submission carries no binary attachments, so
    the stage initialises the doc-assist session and emits an advisory pointing
    adjusters to the /doc-assist UI tab.  Documents uploaded post-FNOL are
    processed independently via the REST API.
    """
    # Lazy import — avoids a circular dep; mirrors the A12 pattern.
    try:
        import sys as _sys, pathlib as _pl
        _da = str(_pl.Path(__file__).parent / "agents" / "doc_assist")
        if _da not in _sys.path:
            _sys.path.insert(0, _da)
        from agents.doc_assist import fnol_doc_assist_agent as _da_mod  # type: ignore[import]
    except Exception as _ie:
        return StageResult(
            "S1A", "Document Assist & Intelligent Classification", "Document Assist Agent",
            "error", _utcnow_iso(), _utcnow_iso(), 0,
            {}, [], [f"s1a_import_failed: {_ie}"], error=str(_ie),
        )

    t0 = time.time()
    started = _utcnow_iso()
    advisories: List[str] = []

    # Pull documents from S1 outputs (future wiring) or empty list for base POC.
    s1_out    = context.get("S1", {}) or {}
    documents = s1_out.get("documents", []) or []

    claim_context: Dict[str, Any] = {
        "policy_number":        claim.policy_number,
        "loss_cause":           claim.loss_cause,
        "injury_reported":      bool(getattr(claim, "injury_reported", False)),
        "attorney_represented": bool(getattr(claim, "attorney_represented", False)),
        "loss_type_cd":         s1_out.get("loss_type_cd", ""),
    }

    try:
        result = _da_mod.process_claim_documents(
            claim_id      = claim.claim_id or "UNKNOWN",
            documents     = documents,
            claim_context = claim_context,
        )
    except Exception as exc:
        return StageResult(
            "S1A", "Document Assist & Intelligent Classification", "Document Assist Agent",
            "error", started, _utcnow_iso(), int((time.time() - t0) * 1000),
            {}, [], [f"s1a_process_failed: {type(exc).__name__}"],
            error=f"{type(exc).__name__}",
        )

    outputs: Dict[str, Any] = {
        "documents_processed": result.processed_count,
        "alerts_dispatched":   len(result.alerts_dispatched),
        "tasks_created":       len(result.tasks_created),
        "litigation_flag":     result.litigation_flag,
        "automation_rate":     result.automation_rate,
        "sla_met":             result.sla_met,
        "missing_types": (result.missing_docs.missing if result.missing_docs else []),
    }

    if result.litigation_flag:
        advisories.append(
            "Litigation document detected — priority alert dispatched to legal team ≤ 30 min."
        )
    resubmit_count = len([d for d in result.documents if d.requires_resubmission])
    if resubmit_count:
        advisories.append(
            f"{resubmit_count} document(s) below quality threshold (< 0.60) — "
            "resubmission request queued."
        )
    if not documents:
        advisories.append(
            "No documents bundled at FNOL intake — S1-A session initialised; "
            "upload documents via Doc Assist tab or POST /api/v1/fnol/doc-assist/classify."
        )

    decisions: List[DecisionRecord] = [DecisionRecord(
        stage_id    = "S1A",
        stage_name  = "Document Assist & Intelligent Classification",
        agent       = "Document Assist Agent",
        decision    = "DOC_ASSIST_READY" if not documents else "DOC_ASSIST_PROCESSED",
        confidence  = 1.0 if not documents else result.automation_rate,
        rationale   = (
            f"docs={result.processed_count}, litigation={result.litigation_flag}, "
            f"alerts={len(result.alerts_dispatched)}, tasks={len(result.tasks_created)}"
        ),
        inputs_hash   = _hash(claim.model_dump()),
        model_version = "doc-assist-v1.0-poc",
        timestamp     = _utcnow_iso(),
        hitl_required = result.litigation_flag,
    )]

    stage_status = "hitl" if result.litigation_flag else "stp"
    return StageResult(
        "S1A", "Document Assist & Intelligent Classification", "Document Assist Agent",
        stage_status, started, _utcnow_iso(), int((time.time() - t0) * 1000),
        outputs, decisions, advisories,
    )


# ───────────────────────────────────────────────────────────────────────────
# Stage 2 — Coverage Verification & Reservation
# ───────────────────────────────────────────────────────────────────────────

def stage_s2_coverage(claim: Claim, context: Dict[str, Any]) -> StageResult:
    t0 = time.time()
    started = _utcnow_iso()
    s1_out = context.get("S1", {}) or {}
    policy = s1_out.get("policy_snapshot") or {}
    covs = policy.get("coverages") or {}
    advisories: List[str] = []

    exclusions = policy.get("exclusions") or []
    ror_letter = None
    ror_triggers: List[str] = list(
        t.value if hasattr(t, "value") else str(t)
        for t in getattr(claim, "ror_trigger_cds", [])
    )
    if exclusions or not s1_out.get("policy_in_force"):
        if not s1_out.get("policy_in_force") and RorTriggerCd.POLICY_LAPSE.value not in ror_triggers:
            ror_triggers.append(RorTriggerCd.POLICY_LAPSE.value)
        for _ in exclusions:
            if RorTriggerCd.EXCLUSION.value not in ror_triggers:
                ror_triggers.append(RorTriggerCd.EXCLUSION.value)
        ror_letter = (
            "RESERVATION OF RIGHTS — coverage analysis pending. "
            f"Triggers: {exclusions or ['policy_not_in_force']}. "
            "All actions taken without prejudice."
        )
        advisories.append("ROR letter drafted; adjuster review required within 2h")
        # Stamp coded triggers back onto the claim for SOR write-back
        try:
            setattr(claim, "ror_trigger_cds",
                    [RorTriggerCd(t) for t in ror_triggers
                     if t in RorTriggerCd._value2member_map_])
        except Exception:
            pass

    # PD reserve: scale to the estimated loss (with a safety factor) and cap at
    # the policy collision limit. The previous hard cap of $8,500 silently
    # under-reserved any claim with a higher limit (e.g. Tesla @ $80k collision).
    collision_limit = int(covs.get("collision", {}).get("limit") or 0)
    estimated_loss = int(claim.estimated_loss_usd or 0)
    pd_reserve_target = max(int(estimated_loss * 1.5), 8_500) if estimated_loss > 0 else 8_500
    pd_reserve = min(pd_reserve_target, collision_limit) if collision_limit > 0 else pd_reserve_target
    bi_reserve  = 0
    if claim.injury_reported or s1_out.get("injury_reported"):
        bi_reserve = min(35_000, int(covs.get("bi_per_person") or 0))
    rental_reserve = int(covs.get("rental_daily") or 0) * 7

    no_fault_states = {"FL", "MI", "NY", "NJ", "PA", "KY", "MA", "MN", "ND", "UT", "HI", "KS"}
    state = policy.get("jurisdiction_state")
    no_fault = state in no_fault_states

    # Deductible: prefer claimant-asserted COLL deductible over policy snapshot
    # when claimant has pre-populated claimant_asserted_coverages. S2 adjudicated
    # value (from policy snapshot) remains authoritative; this is for the advisory.
    _coll_ded_policy = _safe_get(covs, "collision", "deductible", default=0)
    _coll_ded_asserted = None
    for _cov in getattr(claim, "claimant_asserted_coverages", []):
        _ccd = _cov.coverage_cd.value if hasattr(getattr(_cov, "coverage_cd", None), "value") else str(getattr(_cov, "coverage_cd", ""))
        if _ccd == "COLL" and getattr(_cov, "deductible_usd", None) is not None:
            _coll_ded_asserted = float(_cov.deductible_usd)
            break
    if _coll_ded_asserted is not None and _coll_ded_asserted != _coll_ded_policy:
        advisories.append(
            f"Claimant-asserted COLL deductible ${_coll_ded_asserted:,.0f} differs from "
            f"policy snapshot ${_coll_ded_policy:,.0f} — adjuster verification required"
        )

    coverages_active = {
        k: v for k, v in covs.items()
        if v and (not isinstance(v, dict) or v.get("limit"))
    }
    outputs = {
        "coverage_verified": bool(policy) and not exclusions,
        "no_fault_indicator": no_fault,
        "coverages_active": coverages_active,
        "deductible_collision": _coll_ded_policy,
        "deductible_collision_asserted": _coll_ded_asserted,
        "bi_limit_per_person": covs.get("bi_per_person", 0),
        "reserves": {
            "pd": pd_reserve, "bi": bi_reserve, "rental": rental_reserve,
            "total_initial": pd_reserve + bi_reserve + rental_reserve,
        },
        "ror_letter_drafted": bool(ror_letter),
        "ror_letter_text": ror_letter,
        "ror_trigger_cds": ror_triggers,
        "exclusions_triggered": exclusions,
    }
    decisions = [DecisionRecord(
        "S2", "Coverage Verification", "Coverage & Liability Agent",
        "COVERED" if outputs["coverage_verified"] else "DISPUTED",
        0.92 if outputs["coverage_verified"] else 0.55,
        f"exclusions={exclusions}, no_fault={no_fault}, reserves={outputs['reserves']}",
        _hash({"policy": policy.get("policy_number"), "claim": claim.claim_id}),
        "rules-v1", _utcnow_iso(),
        hitl_required=bool(exclusions),
    )]
    status = "warning" if exclusions else "ok"
    return StageResult("S2", "Coverage Verification & Reservation", "Coverage & Liability Agent",
                       status, started, _utcnow_iso(), int((time.time() - t0) * 1000),
                       outputs, decisions, advisories)


# ───────────────────────────────────────────────────────────────────────────
# Stage 3 — Triage, Complexity Scoring, Adjuster Assignment
# ───────────────────────────────────────────────────────────────────────────

ADJUSTER_POOL = [
    {"id": "ADJ-001", "name": "Morgan Halevi",   "tier": "T1_STP",   "authority_usd": 5_000},
    {"id": "ADJ-014", "name": "Priya Krishnan",  "tier": "T2_STD",   "authority_usd": 15_000},
    {"id": "ADJ-023", "name": "Dante Okafor",    "tier": "T3_BI",    "authority_usd": 50_000},
    {"id": "ADJ-031", "name": "Linnea Hernández","tier": "T4_COMPLEX","authority_usd": 250_000},
    {"id": "ADJ-049", "name": "Yusuf Marchetti", "tier": "T5_CAT",   "authority_usd": 100_000},
]

def stage_s3_triage(claim: Claim, context: Dict[str, Any]) -> StageResult:
    t0 = time.time()
    started = _utcnow_iso()
    s1_out = context.get("S1", {}) or {}
    s2_out = context.get("S2", {}) or {}

    injury_signal   = 1.0 if (claim.injury_reported or s1_out.get("injury_reported")) else 0.0
    fatality_signal = 1.0 if claim.fatality_indicator else 0.0
    cov_complex     = 0.7 if s2_out.get("exclusions_triggered") else 0.2
    est_value_norm  = min(1.0, (claim.estimated_loss_usd or 5_000) / 75_000)
    liability_clear = 0.8 if claim.liability_clear else 0.4

    # Composite (per Blueprint Section 03 §S3 weighting)
    raw = (injury_signal * 3.0
           + est_value_norm * 2.5
           + (1 - liability_clear) * 2.0
           + 0.5 * cov_complex
           + (fatality_signal * 4.0))
    triage_score = round(min(10.0, raw), 2)
    confidence = round(0.92 - 0.4 * abs(triage_score - 5) / 5, 2)

    if fatality_signal or triage_score > THRESHOLDS["triage_complex_floor"]:
        track, tier = "T4_COMPLEX", "T4_COMPLEX"
    elif injury_signal:
        track, tier = "T3_BI_LIABILITY", "T3_BI"
    elif triage_score <= THRESHOLDS["triage_stp_ceiling"]:
        track, tier = "STP_EXPRESS", "T1_STP"
    else:
        track, tier = "T2_STANDARD", "T2_STD"

    adjuster = next((a for a in ADJUSTER_POOL if a["tier"] == tier), ADJUSTER_POOL[1])

    stp_eligible = (track == "STP_EXPRESS"
                    and not injury_signal
                    and claim.estimated_loss_usd <= THRESHOLDS["stp_property_damage_cap_usd"])
    stp_confidence = 0.90 if stp_eligible else 0.0

    outputs = {
        "triage_score": triage_score,
        "track_confidence": confidence,
        "recommended_track": track,
        "stp_eligible": stp_eligible,
        "stp_confidence": stp_confidence,
        "assigned_adjuster": adjuster,
        "litigation_propensity_score": round(0.15 + 0.4 * injury_signal + 0.3 * fatality_signal, 2),
        "reserve_guidance_usd": s2_out.get("reserves", {}).get("total_initial", 0),
        "cat_event_code": None,
    }
    decisions = [DecisionRecord(
        "S3", "Triage", "Triage & Assignment Agent",
        f"ROUTE_{track}", confidence,
        f"score={triage_score}, injury={injury_signal}, fatality={fatality_signal}, "
        f"est_loss=${claim.estimated_loss_usd}",
        _hash({"s1": s1_out.get("fnol_status"), "s2": s2_out.get("coverage_verified")}),
        "triage-rules-v1", _utcnow_iso(),
        hitl_required=(confidence < 0.70),
    )]
    return StageResult("S3", "Triage, Complexity & Assignment", "Triage & Assignment Agent",
                       "ok" if confidence >= 0.70 else "hitl",
                       started, _utcnow_iso(), int((time.time() - t0) * 1000),
                       outputs, decisions)


# ───────────────────────────────────────────────────────────────────────────
# Stage 4A — Fraud & Anomaly Detection  (40-signal composite)
# ───────────────────────────────────────────────────────────────────────────

FRAUD_SIGNAL_CATEGORIES = (
    "narrative_mismatch", "iso_match", "network_links", "provider_pattern",
    "timing_anomaly", "geographic_anomaly", "claimant_history", "telematics_mismatch",
)

def stage_s4a_fraud(claim: Claim, context: Dict[str, Any]) -> StageResult:
    """Fraud & Anomaly Detection — ISO ClaimSearch live + 8-signal composite."""
    t0 = time.time()
    started = _utcnow_iso()
    s0 = context.get("S0", {}) or {}
    advisories: List[str] = []
    signals: Dict[str, float] = {k: 0.0 for k in FRAUD_SIGNAL_CATEGORIES}

    desc    = (claim.loss_description or "").lower()
    delta_v = float(s0.get("delta_v_mph") or 0)

    # ── ISO ClaimSearch (live adapter: mock/shell/live per env config) ─────
    iso_result = None
    iso_txn_id = "NOT_QUERIED"
    iso_mode   = "not_queried"
    try:
        iso_req = iso_adapter.ISOClaimSearchRequest(
            claim_id=claim.claim_id or "UNKNOWN",
            claimant_first_name=(claim.reporter_name or "").split(None, 1)[0] or None,
            claimant_last_name=(claim.reporter_name or "").split(None, 1)[1] if " " in (claim.reporter_name or "") else None,
            claimant_zip=(claim.effective_zip or "")[:5] or None,
            vin=claim.vin, vehicle_year=claim.vehicle_year,
            vehicle_make=claim.vehicle_make, vehicle_model=claim.vehicle_model,
            policy_number=claim.policy_number,
            loss_date=(claim.loss_date_time or "")[:10] or None,
        )
        iso_result = iso_adapter.query(iso_req)
        iso_txn_id = iso_result.transaction_id
        iso_mode   = iso_result.adapter_mode
        signals["iso_match"] = iso_result.fraud_signal_weight
        if iso_result.iso_match:
            msg = f"ISO ClaimSearch: {iso_result.hit_count} hit(s) (mode={iso_mode}, txn={iso_txn_id})"
            if iso_result.within_window:
                msg += f" — {iso_result.hit_within_window_count} within 24-month window. Blueprint: fraud signal escalated."
            advisories.append(msg)
        elif claim.iso_match and signals["iso_match"] < 0.25:
            signals["iso_match"] = 0.25
            advisories.append("Intake iso_match=True but live query returned no hits — fallback weight 0.25.")
    except Exception as exc:
        advisories.append(f"ISO query failed ({type(exc).__name__}) — intake iso_match fallback.")
        if claim.iso_match:
            signals["iso_match"] = 0.70

    # ── Narrative–telematics mismatch ──────────────────────────────────────
    high_sev_words = bool(re.search(
        r"\b(catastrophic|severe|totaled|disabling|critical|paralys|spinal|icu"
        r"|unconscious|surgery|fracture|broken|emergency|ambulance)\b", desc))
    if delta_v > 0 and delta_v < 5 and high_sev_words:
        signals["narrative_mismatch"] = 0.85
        advisories.append("Narrative–telematics mismatch: low deltaV + high-severity language.")

    # Telematics mismatch: no crash alert + high loss + low impact score
    telem = claim.telematics
    impact = float((telem.impact_severity_score if telem else 0) or 0)
    no_crash = not (telem.crash_alert_received if telem else False)
    if no_crash and claim.estimated_loss_usd > 10_000 and impact < 2.0:
        signals["telematics_mismatch"] = 0.72
        advisories.append(f"No crash alert, impact {impact:.1f}/10, est loss ${claim.estimated_loss_usd:,.0f}.")

    # ── Prior claims frequency ──────────────────────────────────────────────
    prior = claim.prior_claims_count
    if prior >= 3:
        signals["claimant_history"] = min(0.80, 0.20 + 0.12 * prior)
    elif prior > 0:
        signals["claimant_history"] = 0.15

    # ── Provider pattern ────────────────────────────────────────────────────
    if claim.attorney_represented and claim.injury_reported:
        signals["provider_pattern"] = 0.50

    # ── Timing anomaly ──────────────────────────────────────────────────────
    if claim.policy_tenure_days < 30 and claim.estimated_loss_usd > 7_500:
        signals["timing_anomaly"] = 0.60

    # ── Network graph — Shift Technology / FRISS (live adapter) ────────────
    # Blueprint §S4A Rule 4: "Network graph: claimant shares provider/attorney
    # with 3+ previously fraud-flagged claims → NETWORK_FLAG escalation
    # regardless of composite score → SIU referral mandatory."
    #
    # The adapter is called synchronously within the S4A stage.
    # Mode resolves automatically: live (Shift/FRISS) | shell | mock.
    # seed_fraud=True is preserved as a demo override that forces a
    # SUSPECTED_RING result even when the network adapter returns NONE,
    # allowing demo runs without live vendor credentials.
    ng_result = None
    ng_txn_id = "NOT_QUERIED"
    ng_mode   = "not_queried"
    try:
        ng_req = ng_adapter.build_request_from_claim(
            claim.claim_id or "UNKNOWN",
            claim.model_dump(mode="python"),
        )
        ng_result = ng_adapter.query(ng_req)
        ng_txn_id = ng_result.transaction_id
        ng_mode   = ng_result.adapter_mode

        if ng_result.network_flag:
            # CONFIRMED_RING or SUSPECTED_RING — hard floor regardless of composite
            signals["network_links"] = ng_result.network_signal_weight
            advisories.append(ng_result.network_signal_rationale)
        elif ng_result.ring_classification in ("ELEVATED", "ADVISORY"):
            signals["network_links"] = ng_result.network_signal_weight
            advisories.append(ng_result.network_signal_rationale)

    except Exception as exc:
        advisories.append(f"Network graph query failed ({type(exc).__name__}) — seed_fraud fallback.")
        ng_result = None

    # seed_fraud demo override: force minimum SUSPECTED_RING weight when no
    # live connection fires (preserves POC demo without vendor credentials)
    if claim.seed_fraud and signals.get("network_links", 0.0) < 0.82:
        signals["network_links"] = 0.82
        advisories.append(
            "Network ring indicator active (seed_fraud=True — POC demo override). "
            "Production: replace with live Shift Technology / FRISS ring result."
        )

    # ── Composite ──────────────────────────────────────────────────────────
    populated = [(k, v) for k, v in signals.items() if v > 0]
    if populated:
        breadth_bonus = min(0.15, 0.03 * (len(populated) - 1))
        score = round(min(1.0, sum(v for _, v in populated) / len(populated) + breadth_bonus), 4)
    else:
        score = 0.0
    # Network flag = hard floor per Blueprint §S4A rule 4
    if signals.get("network_links", 0) > 0:
        score = max(score, THRESHOLDS["fraud_hold_band"] + 0.01)

    if score > THRESHOLDS["fraud_hold_band"]:
        band, action = "CRITICAL", "SIU_HOLD"
    elif score > THRESHOLDS["fraud_review_band"]:
        band, action = "HIGH", "ENHANCED_REVIEW"
    elif score > 0.25:
        band, action = "MEDIUM", "ADVISORY_NOTE"
    else:
        band, action = "LOW", "PASS"

    triggered = [k for k, v in signals.items() if v > 0]

    outputs = {
        "fraud_risk_score":      score,
        "fraud_risk_band":       band,
        "fraud_composite_score": score,        # alias for A12 SIU Case Builder
        "fraud_band":            band,          # alias for UI nav badge
        "action":                action,
        "signal_breakdown":      signals,
        "triggered_categories":  triggered,
        "payment_hold_flag":     action == "SIU_HOLD",
        "siu_referral":          action == "SIU_HOLD",
        "fraud_model_version":   "composite-v2.0-iso-wired",
        "iso_adapter_mode":      iso_mode,
        "iso_transaction_id":    iso_txn_id,
        "iso_hit_count":         iso_result.hit_count if iso_result else 0,
        "iso_match":             iso_result.iso_match if iso_result else claim.iso_match,
        "iso_within_window":     iso_result.within_window if iso_result else False,
        # Network graph outputs
        "network_graph_adapter_mode":    ng_mode,
        "network_graph_transaction_id":  ng_txn_id,
        "network_flag":                  ng_result.network_flag if ng_result else (signals.get("network_links",0) > 0),
        "ring_classification":           ng_result.ring_classification if ng_result else ("SUSPECTED_RING" if claim.seed_fraud else "NONE"),
        "ring_id":                       ng_result.ring_id if ng_result else None,
        "ring_size":                     ng_result.ring_size if ng_result else 0,
        "network_connections_count":     ng_result.total_connections if ng_result else 0,
        "network_signal_weight":         signals.get("network_links", 0.0),
        "network_signal_rationale":      (ng_result.network_signal_rationale if ng_result else ""),
        "vendor_risk_level":             (ng_result.vendor_risk_level if ng_result else None),
    }
    decisions = [DecisionRecord(
        "S4A", "Fraud Detection", "Fraud Detection Agent",
        action, round(1.0 - abs(0.5 - score), 4),
        (f"score={score:.4f}, band={band}, triggered={triggered}, "
         f"iso_mode={iso_mode}, iso_txn={iso_txn_id}, "
         f"iso_hits={iso_result.hit_count if iso_result else 0}, "
         f"iso_in_window={iso_result.within_window if iso_result else False}, "
         f"ng_mode={ng_mode}, ng_txn={ng_txn_id}, "
         f"ring={ng_result.ring_classification if ng_result else 'N/A'}, "
         f"network_flag={ng_result.network_flag if ng_result else signals.get('network_links',0)>0}"),
        _hash(claim.model_dump()), "fraud-composite-v2", _utcnow_iso(),
        hitl_required=(action in ("SIU_HOLD", "ENHANCED_REVIEW")),
    )]
    return StageResult("S4A", "Fraud & Anomaly Detection", "Fraud Detection Agent",
                       "hold" if action == "SIU_HOLD" else
                       ("hitl" if action == "ENHANCED_REVIEW" else "ok"),
                       started, _utcnow_iso(), int((time.time() - t0) * 1000),
                       outputs, decisions, advisories)


# ───────────────────────────────────────────────────────────────────────────
# Stage 4B — AI-Powered Damage Assessment
# ───────────────────────────────────────────────────────────────────────────

def stage_s4b_damage(claim: Claim, context: Dict[str, Any]) -> StageResult:
    t0 = time.time()
    started = _utcnow_iso()
    photo_count = claim.photo_count
    photo_quality = claim.photo_quality_score or 0.75
    drivable = claim.drivable_indicator
    advisories: List[str] = []

    # POC heuristic estimate
    base = claim.estimated_loss_usd or 0
    if base <= 0:
        base = 4_000  # last-resort default for damage-range heuristic only
        advisories.append("estimated_loss_usd missing/zero — using POC default for damage range")
    low  = round(base * 0.85, 2)
    high = round(base * 1.20, 2)
    # ── ACV — use effective_acv_usd which returns None when not captured ──
    # CRITICAL BUG FIX (Gap 5): the previous `claim.vehicle_acv_usd or 0`
    # pattern silently returned 0.0 when the field was defaulted to 0.0,
    # hitting the `if acv <= 0` guard correctly but also hitting it for
    # claims that had an explicit 0 ACV (salvage / write-off pre-tagged).
    # effective_acv_usd returns None for "not captured" and 0.0 only when
    # the caller explicitly set vehicle_acv_usd=0 (true write-off ACV).
    acv = getattr(claim, "effective_acv_usd", None)
    if acv is None:
        # Refuse to make a TL determination on missing ACV. The previous code
        # defaulted to $18k, which silently miscategorised every claim without
        # an ACV (always TL for high estimates, never TL for low ones).
        advisories.append("vehicle_acv_usd missing/zero — TL determination deferred to A11/adjuster")
        tl = False
    else:
        tl = (base / acv) >= THRESHOLDS["total_loss_ratio"]

    if photo_quality < THRESHOLDS["photo_quality_floor"] or photo_count < THRESHOLDS["photo_count_floor"]:
        advisories.append("photo quality/count below threshold — re-photo request dispatched")

    s0 = context.get("S0", {}) or {}
    delta_v = float(s0.get("delta_v_mph") or 0)
    if delta_v > 25 and base < 3_000:
        advisories.append("deltaV>25 with low estimate — hidden damage review recommended")

    outputs = {
        "ai_damage_estimate_low_usd": low,
        "ai_damage_estimate_high_usd": high,
        "ai_damage_estimate_point_usd": round((low + high) / 2, 2),
        "total_loss": tl,
        "acv_usd": acv,
        "drivable_indicator": drivable,
        "rental_authorized": not drivable,
        "photo_quality_score": photo_quality,
        "photo_count": photo_count,
        "drp_shop_recommended": "DRP-Houston-North-08" if not tl else None,
        "specialist_appraiser_required": claim.vehicle_class in ("EXOTIC", "CLASSIC", "HEAVY"),
        "model_version": "cv-damage-v1.0-poc",
    }
    decisions = [DecisionRecord(
        "S4B", "Damage Assessment", "Damage Estimation Agent",
        "TOTAL_LOSS" if tl else "REPAIRABLE",
        photo_quality, f"range=${low}-${high}, tl={tl}, drivable={drivable}",
        _hash(claim.model_dump()), "cv-damage-v1.0-poc", _utcnow_iso(),
        hitl_required=(photo_quality < THRESHOLDS["photo_quality_floor"]),
    )]
    return StageResult("S4B", "AI-Powered Damage Assessment", "Damage Estimation Agent",
                       "ok" if photo_quality >= THRESHOLDS["photo_quality_floor"] else "hitl",
                       started, _utcnow_iso(), int((time.time() - t0) * 1000),
                       outputs, decisions, advisories)


# ───────────────────────────────────────────────────────────────────────────
# Stage 5 — BI Evaluation & Liability Determination
# ───────────────────────────────────────────────────────────────────────────

def stage_s5_bi_liability(claim: Claim, context: Dict[str, Any]) -> StageResult:
    t0 = time.time()
    started = _utcnow_iso()
    s1 = context.get("S1", {}) or {}
    s2 = context.get("S2", {}) or {}
    s3 = context.get("S3", {}) or {}

    advisories: List[str] = []
    if not (claim.injury_reported or s1.get("injury_reported")):
        return StageResult("S5", "BI Evaluation & Liability", "Coverage & Liability Agent",
                           "skipped", started, _utcnow_iso(),
                           int((time.time() - t0) * 1000),
                           {"reason": "no injury reported — BI stage not applicable"})

    # Adverse-fault (POC: derive from liability_clear flag + simple modifiers)
    adverse_fault = 30 if claim.liability_clear else 60
    if claim.rear_ended_other:    adverse_fault = max(adverse_fault, 80)
    if claim.rear_ended_by_other: adverse_fault = min(adverse_fault, 20)

    severity = (claim.injury_severity or "MINOR").upper()
    severity_map = {"MINOR": (1_500, 8_000),
                    "MODERATE": (8_000, 25_000),
                    "SEVERE": (35_000, 120_000),
                    "FATAL":  (250_000, 1_500_000)}
    p10, p90 = severity_map.get(severity, (1_500, 8_000))

    bi_reserve = s2.get("reserves", {}).get("bi", 0)
    bi_limit = int(s2.get("bi_limit_per_person") or 0)
    # Compare projection against the policy limit regardless of whether a BI
    # reserve has been set yet — a missing reserve must not mask a real
    # tender-of-limits scenario.
    tender_limits = bool(bi_limit > 0 and p90 > bi_limit)
    if tender_limits:
        advisories.append("settlement projection exceeds BI per-person limit — tender limits analysis required")
    if not bi_reserve and p90 > 0 and bi_limit > 0:
        advisories.append("BI reserve not set despite projected exposure — adjuster review recommended")

    if claim.attorney_represented:
        advisories.append("attorney represented — AI provides comparable data only; no auto-offer")

    outputs = {
        "adverse_fault_pct": adverse_fault,
        "liability_determination": ("INSURED_PRIMARILY_AT_FAULT" if adverse_fault < 50
                                    else "ADVERSE_PRIMARILY_AT_FAULT"),
        "settlement_p10_usd": p10,
        "settlement_p90_usd": p90,
        "injury_severity": severity,
        "tender_limits_flag": tender_limits,
        "ime_referral_recommended": severity in ("MODERATE", "SEVERE") and claim.treatment_outlier,
        "subrogation_handoff": adverse_fault >= 50,
        "model_version": "bi-eval-v1.0-poc",
    }
    decisions = [DecisionRecord(
        "S5", "BI Evaluation", "Coverage & Liability Agent + BI Model",
        outputs["liability_determination"], 0.78,
        f"fault={adverse_fault}%, severity={severity}, "
        f"range=${p10:,}-${p90:,}, tender={tender_limits}",
        _hash(claim.model_dump()), "bi-eval-v1.0-poc", _utcnow_iso(),
        hitl_required=bool(claim.attorney_represented or severity in ("SEVERE", "FATAL")),
    )]
    return StageResult("S5", "BI Evaluation & Liability", "Coverage & Liability Agent",
                       "hitl" if decisions[0].hitl_required else "ok",
                       started, _utcnow_iso(), int((time.time() - t0) * 1000),
                       outputs, decisions, advisories)


# ───────────────────────────────────────────────────────────────────────────
# Stage 6 — Settlement & Payment Authorization
# ───────────────────────────────────────────────────────────────────────────

def stage_s6_settlement(claim: Claim, context: Dict[str, Any]) -> StageResult:
    t0 = time.time()
    started = _utcnow_iso()
    s2 = context.get("S2", {}) or {}
    s3 = context.get("S3", {}) or {}
    s4a = context.get("S4A", {}) or {}
    s4b = context.get("S4B", {}) or {}
    s5 = context.get("S5", {}) or {}
    advisories: List[str] = []

    if s4a.get("payment_hold_flag"):
        outputs = {
            "settlement_status": "PAYMENT_HELD",
            "reason": "fraud_hold — SIU clearance required before any disbursement",
            "amount_authorized_usd": 0,
        }
        return StageResult("S6", "Settlement & Payment", "Settlement Agent",
                           "hold", started, _utcnow_iso(),
                           int((time.time() - t0) * 1000),
                           outputs,
                           [DecisionRecord("S6", "Settlement", "Settlement Agent",
                                           "PAYMENT_BLOCKED", 1.0,
                                           "paymentHoldFlag=true",
                                           _hash(claim.model_dump()), "settle-v1", _utcnow_iso(),
                                           hitl_required=True)],
                           ["payment blocked — fraud hold active"])

    pd_point = float(s4b.get("ai_damage_estimate_point_usd", 0))
    deductible = float(s2.get("deductible_collision") or 0)
    bi_offer = 0.0
    if s5.get("settlement_p10_usd") is not None:
        bi_offer = float(s5.get("settlement_p10_usd", 0))

    stp_ok = (s3.get("stp_eligible")
              and s3.get("stp_confidence", 0) >= THRESHOLDS["stp_confidence_floor"]
              and pd_point <= THRESHOLDS["stp_property_damage_cap_usd"]
              and not s5.get("injury_severity"))

    # Only PD is auto-authorized under STP. BI requires explicit adjuster
    # authorization in a downstream step — never pre-sum it into the
    # authorized amount, or UIs/co-pilot will surface it as approved.
    pd_authorized = round(max(0.0, pd_point - deductible), 2)
    bi_recommended = round(bi_offer, 2)
    amount_authorized = pd_authorized if stp_ok else 0.0
    method = "ACH" if stp_ok else "ADJUSTER_DETERMINED"

    if not stp_ok:
        advisories.append("adjuster approval required — non-STP path")
    if bi_recommended > 0:
        advisories.append(
            f"BI recommendation ${bi_recommended:,.2f} pending separate adjuster authorization"
        )

    outputs = {
        "settlement_status": "AUTHORIZED_STP" if stp_ok else "PENDING_ADJUSTER_APPROVAL",
        "amount_authorized_usd": round(amount_authorized, 2),
        "pd_authorized_usd": pd_authorized if stp_ok else 0.0,
        "bi_recommended_usd": bi_recommended,
        "bi_authorized_usd": 0.0,  # always 0 here; BI requires its own auth step
        "components": {"pd_net_of_deductible": pd_authorized,
                       "bi_initial_offer": bi_recommended},
        "deductible_applied_usd": deductible,
        "payment_method": method,
        "fcra_adverse_action_notice": False,
        "release_required": True,
    }

    # ── Duck Creek payment write-back ────────────────────────────────────
    # For STP claims: authorize and immediately disburse via SOR adapter.
    # For non-STP: authorize with PENDING_ADJUSTER_APPROVAL status; disbursement
    # triggered by adjuster via POST /api/v1/fnol/payments/disburse.
    # Blueprint §S6: "Payment disbursement pipeline (EFT, check) not connected"
    # is closed here — the SOR adapter shells to mock when DC creds absent.
    payment_id: Optional[str] = None
    payment_adapter_mode: str = "not_attempted"
    if amount_authorized > 0:
        try:
            from fnol_sor_adapter import get_sor_adapter as _get_sor
            from fnol_sor_adapter import PaymentRequest as _PR
            sor = _get_sor()
            payee = claim.reporter_name or "Insured"
            pay_req = _PR(
                claim_id=claim.claim_id or "UNKNOWN",
                payment_type="STP" if stp_ok else "PD",
                payment_method=method,
                amount_usd=amount_authorized,
                payee_name=payee,
                memo=f"{'STP auto-pay' if stp_ok else 'PD authorization'} — claim {claim.claim_id}",
                authority_tier="AUTO" if stp_ok else "ADJUSTER",
                coverage_part="collision",
                deductible_applied=deductible,
            )
            pay_resp = sor.authorize_payment(pay_req)
            payment_id = pay_resp.payment_id
            payment_adapter_mode = pay_resp.adapter_mode
            outputs["payment_id"]           = payment_id
            outputs["payment_status"]       = pay_resp.status
            outputs["payment_adapter_mode"] = payment_adapter_mode
            outputs["sor_payment_id"]       = pay_resp.sor_payment_id
            outputs["expected_settle_date"] = pay_resp.expected_settle_date

            # STP: immediately disburse
            if stp_ok and pay_resp.status == "AUTHORIZED":
                disburse_resp = sor.disburse_payment(payment_id)
                outputs["payment_status"]  = disburse_resp.status
                outputs["disbursed_at"]    = disburse_resp.disbursed_at
                advisories.append(
                    f"STP payment {payment_id} authorized and disbursed via "
                    f"{pay_resp.adapter_mode} adapter (method={method}, "
                    f"amount=${amount_authorized:,.2f}, settle={pay_resp.expected_settle_date})."
                )
            else:
                advisories.append(
                    f"Payment {payment_id} authorized (status={pay_resp.status}); "
                    "awaiting adjuster approval before disbursement."
                )
        except Exception as pay_exc:
            advisories.append(
                f"Payment write-back error ({type(pay_exc).__name__}: {pay_exc}) — "
                "claim record updated; manual disbursement required."
            )
            outputs["payment_error"] = str(pay_exc)
            payment_adapter_mode = "error"

    outputs["payment_adapter_mode"] = payment_adapter_mode
    decisions = [DecisionRecord(
        "S6", "Settlement", "Settlement Agent",
        outputs["settlement_status"],
        0.92 if stp_ok else 0.65,
        f"pd_authorized=${pd_authorized}, ded=${deductible}, bi_recommended=${bi_recommended}, stp={stp_ok}",
        _hash(claim.model_dump()), "settle-v1", _utcnow_iso(),
        hitl_required=not stp_ok,
    )]
    return StageResult("S6", "Settlement & Payment Authorization", "Settlement Agent",
                       "ok" if stp_ok else "hitl",
                       started, _utcnow_iso(), int((time.time() - t0) * 1000),
                       outputs, decisions, advisories)


# ───────────────────────────────────────────────────────────────────────────
# Stage 7 — Subrogation & Recovery
# ───────────────────────────────────────────────────────────────────────────

def stage_s7_subrogation(claim: Claim, context: Dict[str, Any]) -> StageResult:
    t0 = time.time()
    started = _utcnow_iso()
    s5 = context.get("S5", {}) or {}
    s6 = context.get("S6", {}) or {}
    advisories: List[str] = []

    adverse_fault = float(s5.get("adverse_fault_pct", 0))
    paid = float(s6.get("amount_authorized_usd", 0))
    third_party_known = bool(claim.third_party_carrier) or bool(claim.third_party_policy_number)

    if not (adverse_fault >= 50 and paid >= THRESHOLDS["subro_open_loss_floor_usd"]):
        return StageResult("S7", "Subrogation & Recovery", "Subrogation Agent",
                           "skipped", started, _utcnow_iso(),
                           int((time.time() - t0) * 1000),
                           {"reason": "subro thresholds not met (fault<50% or paid<$1k)"})

    score = min(1.0, (adverse_fault / 100.0) * (0.6 if third_party_known else 0.4) + (paid / 50_000))
    score = round(min(1.0, score), 2)

    recovery_potential = round(paid * (adverse_fault / 100.0) * 0.85, 2)
    open_arbitration = (score > THRESHOLDS["subro_score_arbitration_floor"]
                        and recovery_potential >= THRESHOLDS["subro_recovery_floor_usd"])
    if claim.vehicle_recall_indicator:
        advisories.append("vehicleRecallIndicator=true — product liability referral; standard subro on hold")

    demand_letter = (
        f"DEMAND LETTER (draft) — recovery of ${recovery_potential:,.2f} from adverse "
        f"carrier {claim.third_party_carrier or 'UNKNOWN'} regarding insured "
        f"{claim.reporter_name or 'UNKNOWN'}, loss date {claim.loss_date_time}, "
        f"liability {adverse_fault}% adverse fault.  Carrier letterhead, signature pending."
    )
    outputs = {
        "subrogation_score": score,
        "recovery_potential_usd": recovery_potential,
        "open_arbitration": open_arbitration,
        "arbitration_forum": "AAA" if open_arbitration else None,
        "demand_letter_drafted": True,
        "demand_letter_text": demand_letter,
        "statute_of_limitations_alert": False,
        "product_liability_referral": claim.vehicle_recall_indicator,
        "model_version": "subro-v1.0-poc",
    }
    decisions = [DecisionRecord(
        "S7", "Subrogation", "Subrogation Agent",
        "OPEN_FILE" if open_arbitration else "MONITOR",
        score, f"fault={adverse_fault}%, paid=${paid:,}, recovery=${recovery_potential:,}",
        _hash(claim.model_dump()), "subro-v1.0-poc", _utcnow_iso(),
    )]
    return StageResult("S7", "Subrogation & Recovery", "Subrogation Agent",
                       "ok", started, _utcnow_iso(), int((time.time() - t0) * 1000),
                       outputs, decisions, advisories)


# ───────────────────────────────────────────────────────────────────────────
# Pipeline orchestrator
# ───────────────────────────────────────────────────────────────────────────

PIPELINE_VERSION = "4.3.0-S1A-doc-assist"

# ───────────────────────────────────────────────────────────────────────────
# A11 — Total-Loss & Salvage Orchestrator (conditional stage)
# Runs after S4B when total_loss=True. Produces a TL evaluation, refined ACV,
# branded-title recommendation, two settlement options, and (when 'auto'
# vendor mode is selected) a shadow-quoted salvage assignment.
# ───────────────────────────────────────────────────────────────────────────

def stage_a11_total_loss(claim: Claim, context: Dict[str, Any]) -> StageResult:
    import fnol_total_loss_agent as a11
    t0 = time.time()
    started = _utcnow_iso()
    s4b = context.get("S4B", {}) or {}
    state = claim.effective_state

    advisories: List[str] = []
    try:
        ev = a11.evaluate(claim, s4b, state=state)
    except Exception as e:
        return StageResult(
            "A11", a11.AGENT_NAME, "Total-Loss & Salvage Orchestrator",
            "error", started, _utcnow_iso(), int((time.time() - t0) * 1000),
            outputs={}, decisions=[], advisories=[f"a11_evaluate_failed: {e!s}"],
            error=str(e),
        )

    # Auto-quote salvage when total loss confirmed (production: gate on
    # adjuster confirmation; POC: shadow-quote immediately for the demo)
    try:
        ev = a11.assign_salvage(ev.evaluation_id, vendor="auto")
    except Exception as e:
        advisories.append(f"salvage_quote_failed: {e!s}")

    outputs = a11.to_stage_outputs(ev)
    if ev.salvage_assignment:
        outputs["salvage_vendor"] = ev.salvage_assignment.get("vendor")
        outputs["salvage_lot_id"] = ev.salvage_assignment.get("vendor_lot_id")
        outputs["salvage_yard"] = ev.salvage_assignment.get("yard_location")
        outputs["salvage_expected_net_usd"] = ev.salvage_assignment.get("expected_net_return_usd")
        outputs["salvage_expected_sale_date"] = ev.salvage_assignment.get("expected_sale_date")

    # HITL when:
    #  - A11's TLT verdict disagrees with S4B's flag (either direction), OR
    #  - the observed ratio is within ±5% of the state TLT (true borderline).
    borderline = abs(ev.tlt_percentage_observed - ev.tlt_pct) <= 0.05
    claim_hash = _hash(claim.model_dump())
    decisions = [DecisionRecord(
        "A11", "Total-Loss Determination", "Total-Loss & Salvage Orchestrator",
        "TOTAL_LOSS" if ev.is_total_loss else "REPAIRABLE",
        ev.confidence, ev.rationale,
        claim_hash, a11.AGENT_VERSION, _utcnow_iso(),
        hitl_required=ev.tl_disagreement_with_s4b or borderline,
    )]
    if ev.salvage_assignment:
        decisions.append(DecisionRecord(
            "A11", "Salvage Vendor Assignment", "Total-Loss & Salvage Orchestrator",
            f"ASSIGN_{ev.salvage_assignment.get('vendor', 'AUTO')}",
            float(ev.salvage_assignment.get("confidence") or 0.85),
            ev.salvage_assignment.get("rationale", ""),
            claim_hash, a11.AGENT_VERSION, _utcnow_iso(),
        ))

    if ev.tl_disagreement_with_s4b:
        advisories.append(
            f"TL disagreement: S4B flag={ev.s4b_total_loss_flag} vs A11 verdict="
            f"{'TOTAL_LOSS' if ev.is_total_loss else 'REPAIRABLE'} — escalate to adjuster"
        )
    elif borderline:
        advisories.append(
            f"Borderline TLT: observed {ev.tlt_percentage_observed*100:.1f}% vs "
            f"threshold {ev.tlt_pct*100:.0f}% — adjuster review recommended"
        )

    if ev.tl_disagreement_with_s4b or borderline:
        status = "hitl"
    elif advisories:
        status = "warning"
    else:
        status = "ok"

    return StageResult(
        "A11", a11.AGENT_NAME, "Total-Loss & Salvage Orchestrator",
        status, started, _utcnow_iso(), int((time.time() - t0) * 1000),
        outputs, decisions, advisories,
    )

# ───────────────────────────────────────────────────────────────────────────
# A12 — SIU Case Builder (conditional stage)
# ───────────────────────────────────────────────────────────────────────────
# Auto-opens an SIU case when S4A fraud band is HIGH or CRITICAL. The SIU
# agent reads pipeline trace shape `{"stages": [...], "claim_payload": ...}`
# — at stage-execution time the engine's `trace` list is not visible to
# stage functions, so we build a minimal synthetic trace from `context`.

def stage_a12_siu_open(claim: Claim, context: Dict[str, Any]) -> StageResult:
    import fnol_siu_agent as siu
    t0 = time.time()
    started = _utcnow_iso()
    advisories: List[str] = []
    s4a_outputs = context.get("S4A", {}) or {}

    synthetic_trace = {
        "stages": [{"stage_id": "S4A", "outputs": s4a_outputs}],
        "claim_payload": claim.model_dump(mode="python"),
    }

    try:
        case = siu.open_case(
            claim_id=claim.claim_id or "UNKNOWN",
            pipeline_trace=synthetic_trace,
            claim_payload=claim.model_dump(mode="python"),
        )
    except ValueError as e:
        # Fraud band not SIU-eligible (LOW/MEDIUM) — skip gracefully.
        return StageResult(
            "A12", "SIU Case Builder", "SIU Case Builder",
            "skipped", started, _utcnow_iso(),
            int((time.time() - t0) * 1000),
            outputs={"reason": str(e)}, decisions=[], advisories=[],
        )
    except Exception as e:
        return StageResult(
            "A12", "SIU Case Builder", "SIU Case Builder",
            "error", started, _utcnow_iso(),
            int((time.time() - t0) * 1000),
            outputs={}, decisions=[],
            advisories=[f"a12_open_case_failed: {type(e).__name__}"],
            error=f"{type(e).__name__}",
        )

    outputs = {
        "case_id":           case.case_id,
        "fraud_band":        case.fraud_band,
        "fraud_risk_score":  case.fraud_risk_score,
        "payment_hold_flag": case.payment_hold_flag,
        "siu_team":          case.siu_team,
        "investigator_id":   (case.investigator or {}).get("investigator_id"),
        "investigator_name": (case.investigator or {}).get("name"),
        "sla_deadline":      case.sla_deadline,
        "triggered_categories": case.triggered_categories,
        "evidence_item_count":  len(case.evidence_items or []),
    }
    if case.payment_hold_flag:
        advisories.append(
            f"SIU case {case.case_id} opened with payment hold — "
            f"settlement disbursement blocked until adjuster disposition."
        )
    else:
        advisories.append(f"SIU case {case.case_id} opened (no hold).")

    # Translate the SIU agent's own DecisionRecords into engine-shaped ones so
    # they flow through the governance hook with the rest of the pipeline.
    decisions: List[DecisionRecord] = []
    claim_hash = _hash(claim.model_dump())
    for d in (case.decisions or []):
        decisions.append(DecisionRecord(
            stage_id="A12",
            stage_name=getattr(d, "stage_name", "SIU Decision"),
            agent="SIU Case Builder",
            decision=getattr(d, "decision", "SIU_DECISION"),
            confidence=float(getattr(d, "confidence", 0.0) or 0.0),
            rationale=getattr(d, "rationale", ""),
            inputs_hash=claim_hash,
            model_version=getattr(d, "model_version", "siu-v1.0-poc"),
            timestamp=getattr(d, "timestamp", _utcnow_iso()),
            hitl_required=bool(getattr(d, "hitl_required", True)),
        ))

    status = "hold" if case.payment_hold_flag else "hitl"
    return StageResult(
        "A12", "SIU Case Builder", "SIU Case Builder",
        status, started, _utcnow_iso(),
        int((time.time() - t0) * 1000),
        outputs, decisions, advisories,
    )


# ───────────────────────────────────────────────────────────────────────────
# Declarative pipeline registry
# ───────────────────────────────────────────────────────────────────────────
# Each stage is defined once in PIPELINE_STAGES below. Adding a new agent
# (e.g. A12 SIU, A13 Repair Network) is a single-line addition; the runner
# handles dependency order, parallel execution, conditional skipping, and
# per-stage error isolation generically.

StageFn = Callable[[Claim, Dict[str, Any]], StageResult]
StageCondition = Callable[[Dict[str, Any]], bool]


@dataclass(frozen=True)
class StageDef:
    """One stage in the declarative pipeline.

    - `id`           : stable identifier surfaced in the trace (S0, S4A, A11…)
    - `fn`           : implementation; must accept (claim, context) and return StageResult
    - `agent`        : display name for the trace
    - `parallel_group`: stages sharing the same non-None group execute concurrently
                       (within the topological position determined by the registry order)
    - `condition`    : optional predicate over `context`; when False the stage is skipped
    """
    id: str
    fn: StageFn
    agent: str
    parallel_group: Optional[str] = None
    condition: Optional[StageCondition] = None


PIPELINE_STAGES: List[StageDef] = [
    StageDef("S0",  stage_s0_pre_fnol,       "FNOL Intake Agent"),
    StageDef("S1",  stage_s1_fnol_capture,   "FNOL Intake Agent"),
    StageDef("S1A", stage_s1a_doc_assist,    "Document Assist Agent"),
    StageDef("S2",  stage_s2_coverage,       "Coverage & Liability Agent"),
    StageDef("S3",  stage_s3_triage,         "Triage & Assignment Agent"),
    StageDef("S4A", stage_s4a_fraud,         "Fraud Detection Agent",   parallel_group="S4"),
    StageDef("S4B", stage_s4b_damage,        "Damage Estimation Agent", parallel_group="S4"),
    StageDef("A11", stage_a11_total_loss,    "Total-Loss & Salvage Orchestrator",
             condition=lambda ctx: bool(ctx.get("S4B", {}).get("total_loss"))),
    # A12 auto-opens an SIU case when fraud band is HIGH or CRITICAL. Runs
    # before S5/S6 so a payment hold (when band=CRITICAL) is in place before
    # settlement evaluation. Manual `POST /api/v1/fnol/siu/open` is still
    # supported for adjuster-initiated cases.
    StageDef("A12", stage_a12_siu_open,      "SIU Case Builder",
             condition=lambda ctx: (ctx.get("S4A", {}).get("fraud_risk_band")
                                    in ("HIGH", "CRITICAL"))),
    StageDef("S5",  stage_s5_bi_liability,   "Coverage & Liability Agent + BI Model"),
    StageDef("S6",  stage_s6_settlement,     "Settlement Agent"),
    StageDef("S7",  stage_s7_subrogation,    "Subrogation Agent"),
]


def _log_decisions_to_governance(claim: Claim, result: StageResult) -> None:
    """Feed every DecisionRecord a stage emitted into the governance chain.
    Lazy import + per-decision try/except so a governance failure never
    affects the pipeline. The SHA-256 chain stays empty if governance is
    not importable — that's intentional (governance is optional infra)."""
    if not result.decisions:
        return
    try:
        import fnol_governance_agent as _gov
    except Exception:
        return
    cid = claim.claim_id or "UNKNOWN"
    for d in result.decisions:
        try:
            _gov.log_decision(
                claim_id=cid,
                stage_id=d.stage_id,
                rule_id=d.stage_name or d.stage_id,
                decision=d.decision,
                confidence=float(d.confidence or 0.0),
                rationale=d.rationale or "",
                hitl_required=bool(d.hitl_required),
                model_version=d.model_version or "unknown",
                input_hash=d.inputs_hash or "",
            )
        except Exception:
            # Governance must never break the pipeline — swallow silently.
            # (A future enhancement could emit a counter for missed entries.)
            pass


def _safe_run_stage(stage: StageDef, claim: Claim,
                    context: Dict[str, Any]) -> StageResult:
    """Run a stage with error isolation. A raised exception becomes an
    `error` StageResult instead of aborting the entire pipeline. After the
    stage returns, every DecisionRecord it emitted is forwarded to the
    governance SHA-256 chain (best-effort; governance failure is silent)."""
    started_at = _utcnow_iso()
    t_local = time.time()
    try:
        result = stage.fn(claim, context)
    except Exception as e:
        result = StageResult(
            stage.id, stage.id, stage.agent, "error",
            started_at, _utcnow_iso(),
            int((time.time() - t_local) * 1000),
            outputs={}, decisions=[],
            advisories=[f"{stage.id.lower()}_failed: {type(e).__name__}"],
            error=f"{type(e).__name__}",
        )
    _log_decisions_to_governance(claim, result)
    return result


def run_pipeline(claim: Claim) -> Dict[str, Any]:
    """Run the full pipeline driven by PIPELINE_STAGES; returns pipeline
    trace + final claim record. Stages with a shared `parallel_group` run
    concurrently; stages with a `condition` are skipped when the predicate
    is False (e.g. A11 only fires on S4B.total_loss=True).
    """
    pipeline_start = time.time()
    started = _utcnow_iso()

    # Stamp identity + lifecycle fields. Claim is mutable; assigning here is
    # safe because run_pipeline owns the lifecycle.
    if not claim.claim_id:
        claim.claim_id = f"CLM-{uuid.uuid4().hex.upper()}"
    claim.status = "INTAKE"
    if not claim.created_at:
        claim.created_at = _utcnow_iso()

    sor = get_sor_adapter()
    sor.create_claim(claim.to_sor_payload())

    trace: List[StageResult] = []
    context: Dict[str, Dict[str, Any]] = {}

    # Walk the registry, batching adjacent stages with the same parallel_group.
    i = 0
    while i < len(PIPELINE_STAGES):
        stage = PIPELINE_STAGES[i]
        group = stage.parallel_group
        if group is None:
            # Sequential stage. Honour condition.
            if stage.condition and not stage.condition(context):
                i += 1
                continue
            result = _safe_run_stage(stage, claim, context)
            trace.append(result)
            context[stage.id] = result.outputs
            i += 1
        else:
            # Collect consecutive stages in the same parallel group.
            batch: List[StageDef] = []
            j = i
            while j < len(PIPELINE_STAGES) and PIPELINE_STAGES[j].parallel_group == group:
                if PIPELINE_STAGES[j].condition is None or PIPELINE_STAGES[j].condition(context):
                    batch.append(PIPELINE_STAGES[j])
                j += 1
            if batch:
                with ThreadPoolExecutor(max_workers=max(2, len(batch))) as pool:
                    futures = {pool.submit(_safe_run_stage, s, claim, context): s for s in batch}
                    # Preserve registry order in the trace, regardless of completion order.
                    results_by_id: Dict[str, StageResult] = {}
                    for fut in futures:
                        s = futures[fut]
                        results_by_id[s.id] = fut.result()
                    for s in batch:
                        result = results_by_id[s.id]
                        trace.append(result)
                        context[s.id] = result.outputs
            i = j

    # Pull canonical stage outputs from context (always-populated dict; the
    # trace itself may be missing a stage that errored or was skipped).
    s1_o  = context.get("S1",  {}) or {}
    s3_o  = context.get("S3",  {}) or {}
    s4a_o = context.get("S4A", {}) or {}
    s4b_o = context.get("S4B", {}) or {}
    s6_o  = context.get("S6",  {}) or {}

    # Roll up final status
    final_status = "OPEN"
    if any(s.status == "hold" for s in trace):
        final_status = "ON_HOLD"
    elif s6_o.get("settlement_status") == "AUTHORIZED_STP":
        final_status = "STP_AUTHORIZED"
    elif any(s.status == "hitl" for s in trace):
        final_status = "ADJUSTER_REVIEW"
    elif s1_o.get("fnol_status") == "DISPUTE":
        final_status = "COVERAGE_DISPUTE"

    record = sor.update_claim(claim.claim_id, {
        "status": final_status,
        "updated_at": _utcnow_iso(),
        "summary": {
            "track":     s3_o.get("recommended_track"),
            "adjuster":  s3_o.get("assigned_adjuster"),
            "fraud_band": s4a_o.get("fraud_risk_band"),
            "damage_point": s4b_o.get("ai_damage_estimate_point_usd"),
            "settlement_status": s6_o.get("settlement_status"),
            "settlement_amount_usd": s6_o.get("amount_authorized_usd"),
            # A11 is authoritative when it ran; fall back to S4B's hint only
            # when no A11 evaluation was produced (claim didn't trigger A11).
            "total_loss": (context["A11"]["is_total_loss"]
                           if "A11" in context and "is_total_loss" in context["A11"]
                           else bool(s4b_o.get("total_loss"))),
            "tl_evaluation_id": context.get("A11", {}).get("evaluation_id"),
            "salvage_vendor":   context.get("A11", {}).get("salvage_vendor"),
            "salvage_lot_id":   context.get("A11", {}).get("salvage_lot_id"),
        },
    })

    return {
        "claim_id": claim.claim_id,
        "pipeline_version": PIPELINE_VERSION,
        "llm_provider": resolve_provider(),
        "started_at": started,
        "completed_at": _utcnow_iso(),
        "total_duration_ms": int((time.time() - pipeline_start) * 1000),
        "final_status": final_status,
        "stages": [_serialize_stage(s) for s in trace],
        "claim_record": record,
    }

def _serialize_stage(s: StageResult) -> Dict[str, Any]:
    # asdict recurses into nested dataclasses already; no need to re-serialise
    # decisions a second time.
    return asdict(s)


# ───────────────────────────────────────────────────────────────────────────
# CLI / smoke test
# ───────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample = {
        "policy_number": "POC-POL-00123",
        "loss_date_time": "2026-05-10T14:25:00Z",
        "loss_location": "Houston, TX",
        "loss_cause": "REAR_END_COLLISION",
        "loss_description": "Stopped at red light; struck from behind. Mild whiplash.",
        "reporter_name": "Aria Castillo",
        "reporter_phone": "+1-713-555-0142",
        "injury_reported": True,
        "injury_severity": "MINOR",
        "estimated_loss_usd": 4800,
        "vehicle_acv_usd": 22500,
        "drivable_indicator": True,
        "photo_count": 6,
        "photo_quality_score": 0.82,
        "liability_clear": True,
        "rear_ended_by_other": True,
        "third_party_carrier": "ACME Mutual",
        "third_party_policy_number": "ACM-7782-99",
        "telematics": {
            "crash_alert_received": True,
            "delta_v_mph": 9.5,
            "impact_severity_score": 3.2,
            "airbag_deployed": False,
            "consent_given": True,
        },
    }
    out = run_pipeline(Claim(**sample))
    print(json.dumps(out, indent=2, default=str)[:4000])
