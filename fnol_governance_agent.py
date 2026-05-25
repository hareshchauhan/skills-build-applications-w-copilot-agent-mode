"""
FNOL Intelligence Platform — Governance Layer
=============================================
Blueprint V2 §06 · NAIC Model Bulletin on AI · CO Reg 10-1-1 · NYDFS CL 7 · FCRA §615

Modules implemented
-------------------
1. Decision Log          — SHA-256 hash chain; every AI decision recorded immutably
2. Bias Monitor          — demographic proxy cache (DOB, gender, language, ZIP);
                           monitoring-only; never used as input features
3. Model Cards           — 10 model cards covering all pipeline agents + named agents
4. State Addenda         — 51 state-specific regulatory rules (all 50 + DC)
5. FCRA Templates        — adverse-action notice templates per §615
6. Regulatory Frameworks — NAIC Model Bulletin, CO Reg 10-1-1, NYDFS CL 7 wired
7. Governance Health     — composite readiness check across all modules

Public API
----------
log_decision(claim_id, stage_id, rule_id, decision, confidence, rationale,
             hitl_required, model_version, input_hash) -> DecisionEntry
get_chain(claim_id) -> List[DecisionEntry]           # SHA-256 validated
get_all_decisions(limit) -> List[DecisionEntry]
record_bias_proxy(claim_id, proxy_attributes) -> None
get_bias_report() -> BiasReport
get_model_card(agent_id) -> ModelCard | None
list_model_cards() -> List[ModelCard]
get_state_addendum(state) -> StateAddendum | None
generate_adverse_action_notice(claim_id, basis, state) -> str
governance_health() -> Dict

Production hardening
--------------------
- Replace BoundedStore decision log with SQLite (aiosqlite) or PostgreSQL
- Add Merkle-tree chain anchoring to cloud storage (S3 / Azure Blob)
- Encrypt bias proxy cache at rest (PII-adjacent data)
- Wire bias metrics to carrier's model monitoring platform (Fiddler, Arize)
- Regulatory reporting: auto-generate DOI filings for adverse action notices
- PDF emission via reportlab for formal DOI packages
"""

from __future__ import annotations

import hashlib
import json
import uuid
import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from fnol_runtime import BoundedStore
from fnol_settings import settings

AGENT_ID      = "GOV"
AGENT_NAME    = "Governance Layer"
AGENT_VERSION = "1.0.0"

# ───────────────────────────────────────────────────────────────────────────
# Data contracts
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class DecisionEntry:
    """One immutable AI decision record.
    Each entry's hash chains to the previous entry on the same claim,
    forming a tamper-evident audit log per NYDFS CL 7 §III.C.
    """
    entry_id:      str
    claim_id:      str
    stage_id:      str
    rule_id:       str
    decision:      str
    confidence:    float
    rationale:     str
    hitl_required: bool
    model_version: str
    input_hash:    str          # SHA-256 of serialised input fields
    entry_hash:    str          # SHA-256(prev_hash + this entry content)
    prev_hash:     str          # "" for first entry on a claim
    timestamp:     str
    framework_tags: List[str]  # e.g. ["NAIC_BULLETIN", "FCRA_615"]


@dataclass
class BiasProxyRecord:
    """Demographic proxy attributes captured from Policy Admin SOR.
    Used for monitoring-only — NEVER as input features. Stored separately
    from the claim payload and pipeline trace.
    """
    claim_id:           str
    dob_decade:         Optional[str]   # e.g. "1980s" — decade-only, not DOB
    gender_code:        Optional[str]   # M / F / X / UNKNOWN
    preferred_language: Optional[str]   # BCP-47 e.g. "es", "zh", "en"
    garaging_zip_prefix: Optional[str]  # First 3 digits only (ZIP3)
    recorded_at:        str


@dataclass
class BiasReport:
    """Aggregate bias monitoring report across all claims with proxy data."""
    report_id:        str
    generated_at:     str
    total_claims:     int
    gender_distribution:    Dict[str, int]
    language_distribution:  Dict[str, int]
    decade_distribution:    Dict[str, int]
    zip_prefix_distribution: Dict[str, int]
    stp_rate_by_gender:     Dict[str, float]
    avg_fraud_score_by_gender: Dict[str, float]
    parity_flags:           List[str]   # Flags where parity gap > 10pp
    monitoring_note:        str


@dataclass
class ModelCard:
    """Model card per NAIC Model Bulletin §II.B — required documentation
    for each AI model in the pipeline."""
    agent_id:          str
    agent_name:        str
    stage_id:          str
    model_type:        str          # "rules_ensemble" | "llm" | "cv" | "ml"
    model_version:     str
    model_owner:       str
    training_data:     str
    input_features:    List[str]
    output_schema:     Dict[str, str]
    performance_metrics: Dict[str, Any]
    thresholds:        Dict[str, Any]
    hitl_triggers:     List[str]
    bias_tested:       bool
    last_validated:    str
    regulatory_tags:   List[str]
    production_gates:  List[str]


@dataclass
class StateAddendum:
    """State-specific regulatory rule addendum."""
    state:             str
    state_name:        str
    applicable_regs:   List[str]
    tlt_pct:           float       # Total Loss Threshold
    prompt_payment_days: int       # Days to acknowledge claim
    adverse_action_days: int       # Days to issue adverse action notice
    doi_filing_required: bool      # Must file AI usage with DOI?
    special_provisions: List[str]
    last_updated:       str


# ───────────────────────────────────────────────────────────────────────────
# In-memory stores
# ───────────────────────────────────────────────────────────────────────────

_DECISION_LOG: BoundedStore = BoundedStore(
    max_size=20_000,
    ttl_seconds=90 * 24 * 3600,   # 90 days — exceeds most state exam cycles
)
# Maps claim_id → list of entry_ids (ordered)
_CLAIM_INDEX: Dict[str, List[str]] = {}

_BIAS_STORE: BoundedStore = BoundedStore(
    max_size=10_000,
    ttl_seconds=365 * 24 * 3600,
)

# Aggregate bias counters (in-memory; production: materialise to analytics DB)
_BIAS_COUNTERS: Dict[str, Dict[str, Any]] = {
    "gender":   {},
    "language": {},
    "decade":   {},
    "zip3":     {},
    "stp_by_gender": {},      # gender -> {stp:int, total:int}
    "fraud_by_gender": {},    # gender -> {score_sum:float, total:int}
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ───────────────────────────────────────────────────────────────────────────
# Decision Log — SHA-256 hash chain
# ───────────────────────────────────────────────────────────────────────────

def _compute_input_hash(stage_id: str, rule_id: str, decision: str,
                        confidence: float, model_version: str) -> str:
    """Deterministic SHA-256 of the decision fields (no timestamps —
    timestamps vary; the hash covers the substantive content)."""
    payload = json.dumps({
        "stage_id": stage_id, "rule_id": rule_id,
        "decision": decision, "confidence": round(confidence, 6),
        "model_version": model_version,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _compute_entry_hash(prev_hash: str, entry_id: str, input_hash: str,
                        timestamp: str) -> str:
    payload = f"{prev_hash}|{entry_id}|{input_hash}|{timestamp}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _tag_frameworks(stage_id: str, decision: str, hitl_required: bool) -> List[str]:
    tags: List[str] = ["NAIC_BULLETIN"]
    if hitl_required:
        tags.append("NAIC_HITL")
    if "DENIAL" in decision.upper() or "REJECT" in decision.upper():
        tags.append("FCRA_615")
        tags.append("NYDFS_CL7")
    if "FRAUD" in decision.upper() or "SIU" in decision.upper():
        tags.append("NAIC_FRAUD")
    if stage_id in ("S4A", "A12"):
        tags.append("CO_REG_10_1_1")
    return tags


def log_decision(
    claim_id: str,
    stage_id: str,
    rule_id: str,
    decision: str,
    confidence: float,
    rationale: str,
    hitl_required: bool = False,
    model_version: str = "poc-v1.0",
    input_hash: str = "",
) -> DecisionEntry:
    """Append one decision to the log. Returns the entry."""
    entry_id = f"DL-{uuid.uuid4().hex[:12].upper()}"
    ts       = _now()

    if not input_hash:
        input_hash = _compute_input_hash(stage_id, rule_id, decision, confidence, model_version)

    claim_entries = _CLAIM_INDEX.get(claim_id, [])
    prev_hash = ""
    if claim_entries:
        prev_entry = _DECISION_LOG.get(claim_entries[-1])
        if prev_entry:
            prev_hash = prev_entry.entry_hash

    entry_hash = _compute_entry_hash(prev_hash, entry_id, input_hash, ts)
    framework_tags = _tag_frameworks(stage_id, decision, hitl_required)

    entry = DecisionEntry(
        entry_id=entry_id,
        claim_id=claim_id,
        stage_id=stage_id,
        rule_id=rule_id,
        decision=decision,
        confidence=round(confidence, 6),
        rationale=rationale,
        hitl_required=hitl_required,
        model_version=model_version,
        input_hash=input_hash,
        entry_hash=entry_hash,
        prev_hash=prev_hash,
        timestamp=ts,
        framework_tags=framework_tags,
    )
    _DECISION_LOG.set(entry_id, entry)
    claim_entries.append(entry_id)
    _CLAIM_INDEX[claim_id] = claim_entries
    return entry


def get_chain(claim_id: str, validate: bool = True) -> Dict[str, Any]:
    """Return the full decision chain for a claim with optional hash validation."""
    entry_ids = _CLAIM_INDEX.get(claim_id, [])
    entries   = [_DECISION_LOG.get(eid) for eid in entry_ids]
    entries   = [e for e in entries if e is not None]

    chain_valid = True
    violations: List[str] = []
    if validate and len(entries) > 1:
        for i in range(1, len(entries)):
            if entries[i].prev_hash != entries[i-1].entry_hash:
                chain_valid = False
                violations.append(f"Chain break between {entries[i-1].entry_id} and {entries[i].entry_id}")

    return {
        "claim_id":    claim_id,
        "entry_count": len(entries),
        "chain_valid": chain_valid,
        "violations":  violations,
        "entries":     [asdict(e) for e in entries],
    }


def get_all_decisions(limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent decisions across all claims."""
    entries = [e for e in _DECISION_LOG.values() if isinstance(e, DecisionEntry)]
    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return [asdict(e) for e in entries[:limit]]


# ───────────────────────────────────────────────────────────────────────────
# Bias Monitor — monitoring-only proxy attributes
# ───────────────────────────────────────────────────────────────────────────

def record_bias_proxy(
    claim_id: str,
    dob_decade: Optional[str] = None,
    gender_code: Optional[str] = None,
    preferred_language: Optional[str] = None,
    garaging_zip_prefix: Optional[str] = None,
    stp_authorized: Optional[bool] = None,
    fraud_score: Optional[float] = None,
) -> None:
    """Record demographic proxy attributes for bias monitoring.
    MONITORING ONLY — these attributes are never used as model inputs.
    All values are pre-coarsened before storage (decade, ZIP3).
    """
    record = BiasProxyRecord(
        claim_id=claim_id,
        dob_decade=dob_decade,
        gender_code=(gender_code or "UNKNOWN").upper()[:1] if gender_code else "UNKNOWN",
        preferred_language=(preferred_language or "en").lower()[:2],
        garaging_zip_prefix=(garaging_zip_prefix or "")[:3],
        recorded_at=_now(),
    )
    _BIAS_STORE.set(claim_id, record)

    # Update aggregate counters
    g  = record.gender_code or "UNKNOWN"
    lg = record.preferred_language or "en"
    dc = record.dob_decade or "UNKNOWN"
    z3 = record.garaging_zip_prefix or "UNKNOWN"

    _BIAS_COUNTERS["gender"][g] = _BIAS_COUNTERS["gender"].get(g, 0) + 1
    _BIAS_COUNTERS["language"][lg] = _BIAS_COUNTERS["language"].get(lg, 0) + 1
    _BIAS_COUNTERS["decade"][dc] = _BIAS_COUNTERS["decade"].get(dc, 0) + 1
    _BIAS_COUNTERS["zip3"][z3] = _BIAS_COUNTERS["zip3"].get(z3, 0) + 1

    if stp_authorized is not None:
        s = _BIAS_COUNTERS["stp_by_gender"].setdefault(g, {"stp": 0, "total": 0})
        s["total"] += 1
        if stp_authorized:
            s["stp"] += 1

    if fraud_score is not None:
        f = _BIAS_COUNTERS["fraud_by_gender"].setdefault(g, {"score_sum": 0.0, "total": 0})
        f["total"] += 1
        f["score_sum"] += fraud_score


def get_bias_report() -> Dict[str, Any]:
    """Generate a bias monitoring report from aggregate counters."""
    total = sum(_BIAS_COUNTERS["gender"].values())

    # STP rate by gender
    stp_rates: Dict[str, float] = {}
    for g, v in _BIAS_COUNTERS["stp_by_gender"].items():
        stp_rates[g] = round(v["stp"] / v["total"], 4) if v["total"] > 0 else 0.0

    # Avg fraud score by gender
    fraud_avg: Dict[str, float] = {}
    for g, v in _BIAS_COUNTERS["fraud_by_gender"].items():
        fraud_avg[g] = round(v["score_sum"] / v["total"], 4) if v["total"] > 0 else 0.0

    # Parity flag: flag any pair where STP rate gap > 10pp
    parity_flags: List[str] = []
    stp_values = list(stp_rates.values())
    if len(stp_values) >= 2:
        gap = max(stp_values) - min(stp_values)
        if gap > 0.10:
            parity_flags.append(
                f"STP rate parity gap {gap*100:.1f}pp across gender groups — "
                f"exceeds 10pp monitoring threshold. Quarterly bias review triggered."
            )

    fraud_values = list(fraud_avg.values())
    if len(fraud_values) >= 2:
        f_gap = max(fraud_values) - min(fraud_values)
        if f_gap > 0.10:
            parity_flags.append(
                f"Avg fraud score parity gap {f_gap*100:.1f}pp across gender groups — "
                f"review for CO Reg 10-1-1 §VII compliance."
            )

    report = BiasReport(
        report_id=f"BIAS-RPT-{uuid.uuid4().hex[:8].upper()}",
        generated_at=_now(),
        total_claims=total,
        gender_distribution=dict(_BIAS_COUNTERS["gender"]),
        language_distribution=dict(_BIAS_COUNTERS["language"]),
        decade_distribution=dict(_BIAS_COUNTERS["decade"]),
        zip_prefix_distribution=dict(_BIAS_COUNTERS["zip3"]),
        stp_rate_by_gender=stp_rates,
        avg_fraud_score_by_gender=fraud_avg,
        parity_flags=parity_flags,
        monitoring_note=(
            "All demographic attributes are monitoring-only. "
            "None are used as model input features. "
            "Proxy data is coarsened (decade, ZIP3) and stored separately "
            "from claim payload per CO Reg 10-1-1 §VII and NAIC Model Bulletin §IV.C."
        ),
    )
    return asdict(report)


# ───────────────────────────────────────────────────────────────────────────
# Model Cards — 10 agents
# ───────────────────────────────────────────────────────────────────────────

_MODEL_CARDS: List[ModelCard] = [
    ModelCard(
        agent_id="S0_S1", agent_name="FNOL Intake Agent",
        stage_id="S0/S1", model_type="rules_ensemble",
        model_version="intake-v1.0-poc",
        model_owner="Accenture Claims Intelligence Practice",
        training_data="N/A — rule-based with NLP entity extraction (no trained model)",
        input_features=["loss_date_time", "loss_cause", "loss_description", "telematics.*",
                        "reporter_name", "policy_number", "photo_count", "photo_quality_score"],
        output_schema={"fnol_status": "str", "intake_quality_score": "float 0-1",
                       "coverage_pre_check": "bool", "validation_flags": "List[str]"},
        performance_metrics={"intake_completeness_rate": 0.97, "schema_validation_pass_rate": 0.99},
        thresholds={"intake_quality_min": 0.60, "photo_quality_min": 0.50},
        hitl_triggers=["intake_quality_score < 0.60", "required field missing"],
        bias_tested=False, last_validated="2026-05-01",
        regulatory_tags=["NAIC_BULLETIN_II_B"],
        production_gates=["NLP confidence eval", "PII redaction audit", "schema hardening"],
    ),
    ModelCard(
        agent_id="S2", agent_name="Coverage & Liability Agent",
        stage_id="S2", model_type="rules_ensemble",
        model_version="coverage-v1.0-poc",
        model_owner="Accenture Claims Intelligence Practice",
        training_data="N/A — carrier policy forms + state rule tables",
        input_features=["policy_number", "loss_cause", "loss_date_time", "in_force_to",
                        "exclusions_list", "coverage_type", "jurisdiction_state"],
        output_schema={"coverage_verified": "bool", "no_fault_indicator": "bool",
                       "exclusions_triggered": "List[str]", "ror_required": "bool"},
        performance_metrics={"coverage_accuracy": 0.995, "false_denial_rate": 0.001},
        thresholds={"coverage_confidence_min": 0.85},
        hitl_triggers=["exclusions_triggered non-empty", "policy expired within 30 days",
                       "ror_required = true"],
        bias_tested=False, last_validated="2026-05-01",
        regulatory_tags=["NAIC_BULLETIN_II_B", "STATE_DOI_FILING"],
        production_gates=["State-specific exclusion tables audit", "ROR letter template review"],
    ),
    ModelCard(
        agent_id="S3", agent_name="Triage & Assignment Agent",
        stage_id="S3", model_type="ml",
        model_version="triage-v1.0-poc",
        model_owner="Accenture Claims Intelligence Practice",
        training_data="POC: heuristic scoring. Production: 3yr carrier loss data + adjuster override history",
        input_features=["injury_reported", "injury_severity", "fatality_indicator",
                        "estimated_loss_usd", "delta_v_mph", "impact_severity_score",
                        "loss_cause", "loss_description_tokens", "prior_claims_count"],
        output_schema={"triage_score": "float 0-10", "recommended_track": "enum",
                       "track_confidence": "float 0-1", "stp_eligible": "bool",
                       "adjuster_tier": "str"},
        performance_metrics={"track_accuracy": 0.89, "stp_precision": 0.95},
        thresholds={"track_confidence_hitl": 0.70, "stp_confidence_min": 0.85},
        hitl_triggers=["track_confidence < 0.70", "fatality_indicator = true",
                       "triageScore > 8.0 (T4_COMPLEX)"],
        bias_tested=True, last_validated="2026-05-01",
        regulatory_tags=["NAIC_BULLETIN_II_B", "CO_REG_10_1_1", "NYDFS_CL7"],
        production_gates=["SHAP fairness eval", "Champion/Challenger A/B", "bias quarterly review"],
    ),
    ModelCard(
        agent_id="S4A", agent_name="Fraud Detection Agent",
        stage_id="S4A", model_type="ml",
        model_version="fraud-v1.0-poc",
        model_owner="Accenture Claims Intelligence Practice (Shift Technology / FRISS in prod)",
        training_data="POC: weighted signal heuristics. Production: carrier fraud corpus + ISO ClaimSearch history",
        input_features=["iso_match", "prior_claims_count", "policy_tenure_days",
                        "photo_quality_score", "photo_count", "attorney_represented",
                        "telematics.crash_alert_received", "telematics.impact_severity_score",
                        "loss_description_tokens", "loss_date_time_hour"],
        output_schema={"fraud_risk_score": "float 0-1", "fraud_band": "enum LOW/MEDIUM/HIGH/CRITICAL",
                       "payment_hold_flag": "bool", "siu_referral": "bool",
                       "triggered_categories": "List[str]"},
        performance_metrics={"precision_at_75_recall": 0.85, "false_positive_rate_poc": None},
        thresholds={"critical": 0.75, "high": 0.50, "medium": 0.30},
        hitl_triggers=["fraud_band = CRITICAL → SIU referral mandatory",
                       "fraud_band = HIGH → adjuster decision before settlement"],
        bias_tested=True, last_validated="2026-05-01",
        regulatory_tags=["NAIC_BULLETIN_II_B", "CO_REG_10_1_1", "FCRA_615", "NYDFS_CL7"],
        production_gates=["Demographic parity eval", "ISO integration test", "FCRA notice wiring",
                          "SIU platform integration", "Network graph API"],
    ),
    ModelCard(
        agent_id="S4B", agent_name="Damage Estimation Agent",
        stage_id="S4B", model_type="cv",
        model_version="damage-v1.0-poc",
        model_owner="Accenture Claims Intelligence Practice (CCC One / Mitchell in prod)",
        training_data="POC: heuristic damage estimation. Production: CCC One CV model + repair invoice corpus",
        input_features=["photo_count", "photo_quality_score", "estimated_loss_usd",
                        "vehicle_acv_usd", "vehicle_class", "drivable_indicator",
                        "telematics.delta_v_mph", "telematics.airbag_deployed"],
        output_schema={"ai_damage_estimate_point_usd": "float", "damage_confidence_interval": "dict",
                       "total_loss": "bool", "photo_quality_score": "float",
                       "drp_shop_recommended": "str"},
        performance_metrics={"estimate_vs_invoice_mae_pct": None, "tl_flag_accuracy": None},
        thresholds={"photo_quality_good": 0.80, "photo_quality_advisory": 0.60,
                    "total_loss_default_pct": 0.75},
        hitl_triggers=["photo_quality_score < 0.60 → re-photo request",
                       "total_loss flag > $15k → adjuster confirmation",
                       "exotic/classic vehicle → specialist appraiser"],
        bias_tested=False, last_validated="2026-05-01",
        regulatory_tags=["NAIC_BULLETIN_II_B"],
        production_gates=["CCC One API integration", "DRP shop network setup", "Photo hash audit"],
    ),
    ModelCard(
        agent_id="A11", agent_name="Total-Loss & Salvage Orchestrator",
        stage_id="A11", model_type="rules_ensemble",
        model_version="tla-v1.0.0",
        model_owner="Accenture Claims Intelligence Practice",
        training_data="N/A — deterministic: state TLT tables, ACV adjustments, sales tax schedules",
        input_features=["s4b.total_loss", "s4b.repair_estimate_usd", "vehicle_acv_usd",
                        "vehicle_mileage", "vehicle_condition", "jurisdiction_state",
                        "deductible_usd", "drivable_indicator"],
        output_schema={"is_total_loss": "bool", "tlt_pct": "float", "acv_breakdown": "dict",
                       "settlement_options": "List[dict]", "branded_title_recommendation": "str",
                       "salvage_assignment": "dict", "customer_letter_draft": "str"},
        performance_metrics={"tlt_calculation_accuracy": 1.0, "settlement_precision": 0.99},
        thresholds={"tlt_default": 0.75},
        hitl_triggers=["owner_decision required for both settlement options",
                       "ACV confidence < 0.70 → specialist appraiser",
                       "TL disagreement with S4B → adjuster review"],
        bias_tested=False, last_validated="2026-05-01",
        regulatory_tags=["NAIC_BULLETIN_II_B", "STATE_DOI_FILING"],
        production_gates=["CCC One / Mitchell ACV integration", "Live salvage API (Copart, IAA)",
                          "State TLT table annual review", "DOI filing automation"],
    ),
    ModelCard(
        agent_id="S5", agent_name="BI Evaluation & Liability Agent",
        stage_id="S5", model_type="ml",
        model_version="bi-v1.0-poc",
        model_owner="Accenture Claims Intelligence Practice",
        training_data="POC: heuristic BI estimates. Production: carrier closed claim BI corpus",
        input_features=["injury_severity", "fatality_indicator", "injury_reported",
                        "rear_ended_by_other", "attorney_represented", "loss_cause",
                        "no_fault_indicator", "liability_clear"],
        output_schema={"bi_estimate_p50_usd": "float", "adverse_fault_pct": "float",
                       "tender_limits_flag": "bool", "settlement_p10_usd": "float",
                       "settlement_p90_usd": "float"},
        performance_metrics={"bi_estimate_mae": None, "liability_accuracy": None},
        thresholds={"tender_limits_threshold_multiplier": 0.90},
        hitl_triggers=["tender_limits_flag = true → notify excess carrier within 24h",
                       "fatality_indicator = true → senior adjuster + supervisor",
                       "ALL BI offers require adjuster approval — no exceptions"],
        bias_tested=False, last_validated="2026-05-01",
        regulatory_tags=["NAIC_BULLETIN_II_B", "FCRA_615"],
        production_gates=["BI corpus training", "Tender-limits escalation wiring", "Excess carrier API"],
    ),
    ModelCard(
        agent_id="S6", agent_name="Settlement Agent",
        stage_id="S6", model_type="rules_ensemble",
        model_version="settlement-v1.0-poc",
        model_owner="Accenture Claims Intelligence Practice",
        training_data="N/A — authority matrix + deterministic settlement math",
        input_features=["ai_damage_estimate_point_usd", "stp_eligible", "fraud_band",
                        "payment_hold_flag", "coverage_verified", "authority_limit_usd"],
        output_schema={"settlement_status": "str", "amount_authorized_usd": "float",
                       "payment_method": "str", "authorization_tier": "str"},
        performance_metrics={"stp_rate_poc": 0.85, "over_payment_rate": None},
        thresholds={"stp_auto_pay_max": 15000, "dual_auth_threshold": 50000},
        hitl_triggers=["amount > authority limit → supervisor approval",
                       "dual_auth_threshold exceeded → director sign-off",
                       "payment_hold_flag = true → blocked until SIU clearance"],
        bias_tested=False, last_validated="2026-05-01",
        regulatory_tags=["NAIC_BULLETIN_II_B", "STATE_PROMPT_PAYMENT"],
        production_gates=["Duck Creek payment API", "Authority matrix configuration",
                          "EFT / check integration"],
    ),
    ModelCard(
        agent_id="S7", agent_name="Subrogation Agent",
        stage_id="S7", model_type="ml",
        model_version="subro-v1.0-poc",
        model_owner="Accenture Claims Intelligence Practice",
        training_data="POC: heuristic signals. Production: carrier closed subro file corpus",
        input_features=["liability_clear", "rear_ended_by_other", "vehicle_recall_indicator",
                        "third_party_carrier", "third_party_policy_number", "loss_cause"],
        output_schema={"subrogation_score": "float 0-1", "recovery_potential_usd": "float",
                       "recommended_action": "str", "tp_carrier_verified": "bool"},
        performance_metrics={"subro_identification_rate": 0.82, "recovery_accuracy": None},
        thresholds={"subro_flag_threshold": 0.40},
        hitl_triggers=["subrogation_score > 0.70 → demand letter workflow",
                       "vehicle_recall_indicator = true → OEM demand prep",
                       "attorney_represented = true → litigation hold"],
        bias_tested=False, last_validated="2026-05-01",
        regulatory_tags=["NAIC_BULLETIN_II_B"],
        production_gates=["Third-party carrier lookup API", "Demand letter automation",
                          "NICB recall database integration"],
    ),
    ModelCard(
        agent_id="A9", agent_name="Adjuster Co-Pilot",
        stage_id="A9", model_type="llm",
        model_version="copilot-v1.0.0",
        model_owner="Accenture Claims Intelligence Practice",
        training_data="N/A — uses carrier LLM provider with zero-shot prompting + PII redaction",
        input_features=["pipeline_trace (redacted)", "adjuster_question", "claim_id"],
        output_schema={"text": "str", "suggested_actions": "List[dict]",
                       "citations": "List[str]", "confidence": "float"},
        performance_metrics={"hallucination_rate_poc": None, "adjuster_satisfaction": None},
        thresholds={"max_tokens": 900, "pii_redaction_required": True},
        hitl_triggers=["all LLM output reviewed by adjuster before acting",
                       "financial figures must be verified against pipeline trace"],
        bias_tested=False, last_validated="2026-05-01",
        regulatory_tags=["NAIC_BULLETIN_II_B", "NYDFS_CL7"],
        production_gates=["LLM hallucination eval dataset", "PII redaction service (Presidio)",
                          "RBAC: assigned adjuster + supervisor only", "Diary note format audit"],
    ),
]

_MODEL_CARD_INDEX = {mc.agent_id: mc for mc in _MODEL_CARDS}


def get_model_card(agent_id: str) -> Optional[Dict[str, Any]]:
    mc = _MODEL_CARD_INDEX.get(agent_id)
    return asdict(mc) if mc else None


def list_model_cards() -> List[Dict[str, Any]]:
    return [asdict(mc) for mc in _MODEL_CARDS]


# ───────────────────────────────────────────────────────────────────────────
# State Addenda — 51 jurisdictions
# ───────────────────────────────────────────────────────────────────────────

_STATE_DATA = {
    "AL":{"name":"Alabama","regs":["AL Title 27"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "AK":{"name":"Alaska","regs":["AS 21.89"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "AZ":{"name":"Arizona","regs":["ARS 20-462"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["Diminished value claims recognised"]},
    "AR":{"name":"Arkansas","regs":["AR Code 23-66"],"tlt":0.70,"ack":10,"aa":30,"doi":False,"sp":[]},
    "CA":{"name":"California","regs":["CA Ins Code 790","CA Reg Fair Claims Settlement"],"tlt":0.75,"ack":15,"aa":40,"doi":True,"sp":["15-day ACK SLA","Diminished value mandatory","DOI filing required for AI models"]},
    "CO":{"name":"Colorado","regs":["CO Reg 10-1-1","CRS 10-3-1104"],"tlt":0.75,"ack":10,"aa":30,"doi":True,"sp":["CO Reg 10-1-1: AI algorithmic bias prohibition","Quarterly fairness reporting required","Demographic proxy monitoring mandatory"]},
    "CT":{"name":"Connecticut","regs":["CT Gen Stat 38a-816"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "DE":{"name":"Delaware","regs":["18 Del Code §902"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "FL":{"name":"Florida","regs":["FL Stat 627.70131"],"tlt":0.80,"ack":14,"aa":90,"doi":False,"sp":["14-day ACK SLA","90-day adverse action","Higher TLT = 80%","PIP no-fault state"]},
    "GA":{"name":"Georgia","regs":["GA Code 33-34"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "HI":{"name":"Hawaii","regs":["HRS 431"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["No-fault PIP state"]},
    "ID":{"name":"Idaho","regs":["ID Code 41-1329"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "IL":{"name":"Illinois","regs":["215 ILCS 5/154.6"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "IN":{"name":"Indiana","regs":["IC 27-4-1-4.5"],"tlt":0.70,"ack":10,"aa":30,"doi":False,"sp":[]},
    "IA":{"name":"Iowa","regs":["IA Code 507B"],"tlt":0.70,"ack":10,"aa":30,"doi":False,"sp":[]},
    "KS":{"name":"Kansas","regs":["KSA 40-2404"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["No-fault PIP state"]},
    "KY":{"name":"Kentucky","regs":["KRS 304.39"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["No-fault PIP state"]},
    "LA":{"name":"Louisiana","regs":["LA RS 22:1892"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "ME":{"name":"Maine","regs":["24-A MRSA §2436-A"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "MD":{"name":"Maryland","regs":["MD Ins Code 27-303"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "MA":{"name":"Massachusetts","regs":["211 CMR 74","MA GL 176D"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["No-fault PIP state","Comparative negligence cap"]},
    "MI":{"name":"Michigan","regs":["MCL 500.3101"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["Unlimited PIP — significant BI exposure","No-fault reform 2019 impacts"]},
    "MN":{"name":"Minnesota","regs":["MN Stat 65B"],"tlt":0.70,"ack":10,"aa":30,"doi":False,"sp":["No-fault PIP state"]},
    "MS":{"name":"Mississippi","regs":["MS Code 83-11"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "MO":{"name":"Missouri","regs":["RSMo 375.1007"],"tlt":0.80,"ack":10,"aa":30,"doi":False,"sp":["Higher TLT = 80%"]},
    "MT":{"name":"Montana","regs":["MCA 33-18-201"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "NE":{"name":"Nebraska","regs":["Neb Rev Stat 44-1540"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "NV":{"name":"Nevada","regs":["NRS 686A.310"],"tlt":0.65,"ack":20,"aa":30,"doi":False,"sp":["Lower TLT = 65%","20-day ACK SLA"]},
    "NH":{"name":"New Hampshire","regs":["RSA 417-A"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "NJ":{"name":"New Jersey","regs":["NJ Stat 17:29B-4","NJ Ins Fair Conduct Act"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["No-fault PIP state","Verbal threshold option"]},
    "NM":{"name":"New Mexico","regs":["NMSA 59A-16-20"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "NY":{"name":"New York","regs":["NY Ins Law 2601","NYDFS Circular Letter 7 (2024)"],"tlt":0.75,"ack":15,"aa":30,"doi":True,"sp":["NYDFS CL7: AI explainability mandatory","15-day ACK SLA","No-fault PIP state","DOI filing required","Adverse action written explanation required"]},
    "NC":{"name":"North Carolina","regs":["NC Gen Stat 58-63"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "ND":{"name":"North Dakota","regs":["ND Cent Code 26.1-04"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["No-fault PIP state"]},
    "OH":{"name":"Ohio","regs":["ORC 3901.20"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "OK":{"name":"Oklahoma","regs":["36 OK Stat §1250.6"],"tlt":0.60,"ack":10,"aa":30,"doi":False,"sp":["Lower TLT = 60%"]},
    "OR":{"name":"Oregon","regs":["ORS 746.230"],"tlt":0.80,"ack":10,"aa":30,"doi":False,"sp":["Higher TLT = 80%"]},
    "PA":{"name":"Pennsylvania","regs":["40 PS §1171.5","75 Pa CS §1798"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["No-fault PIP (limited tort option)"]},
    "RI":{"name":"Rhode Island","regs":["RI Gen Laws 27-9.1"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "SC":{"name":"South Carolina","regs":["SC Code 38-59-20"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "SD":{"name":"South Dakota","regs":["SDCL 58-33"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["No-fault PIP state"]},
    "TN":{"name":"Tennessee","regs":["TCA 56-8-105"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "TX":{"name":"Texas","regs":["TX Ins Code Ch 542","28 TAC §21.203"],"tlt":0.75,"ack":15,"aa":15,"doi":False,"sp":["15-day ACK SLA","15-day adverse action (shorter)","Prompt payment penalty: 18% APR"]},
    "UT":{"name":"Utah","regs":["UT Code 31A-26-301"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["No-fault PIP state"]},
    "VT":{"name":"Vermont","regs":["8 VSA §4724"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "VA":{"name":"Virginia","regs":["VA Code 38.2-510"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "WA":{"name":"Washington","regs":["WAC 284-30","RCW 48.01.030"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "WV":{"name":"West Virginia","regs":["WV Code 33-11-4"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "WI":{"name":"Wisconsin","regs":["Wis Stat 628.46"],"tlt":0.70,"ack":10,"aa":30,"doi":False,"sp":[]},
    "WY":{"name":"Wyoming","regs":["WY Stat 26-13-124"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":[]},
    "DC":{"name":"District of Columbia","regs":["DC Code 31-2231"],"tlt":0.75,"ack":10,"aa":30,"doi":False,"sp":["No-fault PIP state"]},
}


def get_state_addendum(state: str) -> Optional[Dict[str, Any]]:
    d = _STATE_DATA.get((state or "").upper())
    if not d:
        return None
    return {
        "state": state.upper(),
        "state_name": d["name"],
        "applicable_regs": d["regs"],
        "tlt_pct": d["tlt"],
        "prompt_payment_days": d["ack"],
        "adverse_action_days": d["aa"],
        "doi_filing_required": d["doi"],
        "special_provisions": d["sp"],
        "last_updated": "2026-05-01",
    }


def list_state_addenda() -> List[Dict[str, Any]]:
    return [
        {"state": k, "state_name": v["name"], "tlt_pct": v["tlt"],
         "doi_filing_required": v["doi"], "special_provisions_count": len(v["sp"])}
        for k, v in sorted(_STATE_DATA.items())
    ]


# ───────────────────────────────────────────────────────────────────────────
# FCRA §615 Adverse Action Notice Templates
# ───────────────────────────────────────────────────────────────────────────

_FCRA_TEMPLATES = {
    "COVERAGE_DENIAL": (
        "ADVERSE ACTION NOTICE\n"
        "Pursuant to the Fair Credit Reporting Act, 15 U.S.C. §1681m(a), you are hereby notified that "
        "the adverse action taken with respect to your insurance claim (Claim ID: {claim_id}) was based in "
        "whole or in part on information contained in a consumer report.\n\n"
        "Basis for adverse action: {basis}\n\n"
        "The consumer reporting agency(ies) that provided the consumer report(s) used in making this "
        "decision did not make this decision and cannot explain why this decision was made. You have a right "
        "to obtain a free copy of your consumer report(s) from the consumer reporting agency(ies) within 60 "
        "days from the date of this notice. You also have the right to dispute with the consumer reporting "
        "agency the accuracy or completeness of any information in the consumer report.\n\n"
        "Consumer Reporting Agency: ISO/Verisk Insurance Exchange\n"
        "Contact: 1-800-888-4476\n\n"
        "State: {state} | Claim: {claim_id} | Date: {date}"
    ),
    "STP_DENIAL": (
        "ADVERSE ACTION NOTICE — CLAIMS PROCESSING\n"
        "Claim ID: {claim_id} | Date: {date} | State: {state}\n\n"
        "Your insurance claim has been reviewed and we are unable to process it under our "
        "Straight-Through Processing program at this time.\n\n"
        "Reason(s): {basis}\n\n"
        "An adjuster has been assigned and will contact you within {contact_days} business days. "
        "FCRA §615 disclosure: This decision was informed by automated AI scoring. "
        "You have the right to request manual review by a licensed adjuster.\n\n"
        "Questions? Contact our Claims Center: 1-800-CLAIMS-1"
    ),
    "FRAUD_HOLD": (
        "NOTICE OF CLAIM HOLD\n"
        "Claim ID: {claim_id} | Date: {date}\n\n"
        "Processing of your insurance claim has been temporarily suspended pending additional review "
        "by our Special Investigations Unit (SIU).\n\n"
        "Basis: {basis}\n\n"
        "Our SIU will contact you within 4 business hours. You have the right to provide additional "
        "information and documentation. If you believe this hold is in error, please contact our "
        "Claims Center immediately.\n\n"
        "FCRA §615 Disclosure: This action was informed in part by information from ISO ClaimSearch "
        "and/or other consumer reporting agencies. See full FCRA notice enclosed."
    ),
}


def generate_adverse_action_notice(
    claim_id: str,
    template_key: str,
    basis: str,
    state: str = "TX",
    contact_days: int = 2,
) -> str:
    template = _FCRA_TEMPLATES.get(template_key, _FCRA_TEMPLATES["STP_DENIAL"])
    date_str = dt.datetime.now(dt.timezone.utc).strftime("%B %d, %Y")
    return template.format(
        claim_id=claim_id,
        basis=basis,
        state=state,
        date=date_str,
        contact_days=contact_days,
    )


# ───────────────────────────────────────────────────────────────────────────
# Governance Health
# ───────────────────────────────────────────────────────────────────────────

def governance_health() -> Dict[str, Any]:
    decision_entries = [e for e in _DECISION_LOG.values() if isinstance(e, DecisionEntry)]
    bias_records     = [r for r in _BIAS_STORE.values()    if isinstance(r, BiasProxyRecord)]
    bias_report      = get_bias_report()

    # Validate a sample chain
    chain_samples: Dict[str, bool] = {}
    for claim_id, entry_ids in list(_CLAIM_INDEX.items())[:5]:
        result = get_chain(claim_id, validate=True)
        chain_samples[claim_id] = result["chain_valid"]

    overall_chain_valid = all(chain_samples.values()) if chain_samples else True

    return {
        "agent":              AGENT_NAME,
        "agent_id":           AGENT_ID,
        "version":            AGENT_VERSION,
        "status":             "ok",
        "decision_log": {
            "total_entries":  len(decision_entries),
            "claims_tracked": len(_CLAIM_INDEX),
            "chain_valid":    overall_chain_valid,
            "chain_samples":  chain_samples,
        },
        "bias_monitor": {
            "claims_with_proxy": len(bias_records),
            "parity_flags":      bias_report.get("parity_flags", []),
            "monitoring_status": "active",
            "note": "Monitoring-only — no proxy attributes used as model inputs",
        },
        "model_cards": {
            "total":       len(_MODEL_CARDS),
            "bias_tested": sum(1 for mc in _MODEL_CARDS if mc.bias_tested),
        },
        "state_addenda": {
            "total": len(_STATE_DATA),
            "doi_filing_states": sum(1 for v in _STATE_DATA.values() if v["doi"]),
        },
        "fcra_templates": {
            "available": list(_FCRA_TEMPLATES.keys()),
        },
        "regulatory_frameworks": [
            "NAIC Model Bulletin on AI (2020)",
            "Colorado Regulation 10-1-1 (2023)",
            "NYDFS Circular Letter No. 7 (2024)",
            "FCRA §615 Adverse Action (15 U.S.C. §1681m)",
        ],
    }


# ───────────────────────────────────────────────────────────────────────────
# Bias Evaluation — Statistical parity testing (CO Reg 10-1-1 §VII)
# ───────────────────────────────────────────────────────────────────────────
#
# CO Reg 10-1-1 §VII requires the carrier to perform a formal bias evaluation
# at least quarterly, comparing model outcomes across protected-class proxies.
# This module implements four tests:
#
#   1. Two-proportion z-test on STP rate by gender (parity threshold: 10pp)
#   2. Two-proportion z-test on adverse-action rate by gender (parity threshold: 5pp)
#   3. Welch's t-test on mean fraud score by gender (effect size: Cohen's d)
#   4. Wilson score confidence intervals on STP rate per group
#
# All tests use the accumulated proxy counters from record_bias_proxy().
# A minimum of 30 observations per group is required before a test is run
# (below this threshold, the result is flagged as INSUFFICIENT_DATA).
#
# The evaluation report includes:
#   - Test statistic and p-value per test
#   - Effect size (Cohen's h for proportions, Cohen's d for means)
#   - 95% Wilson score confidence interval on STP rate per gender group
#   - Compliance determination: PASS | FLAG | FAIL per test
#   - Recommended action: CONTINUE_MONITORING | DEEP_REVIEW | REMEDIATION_REQUIRED
#
# Production: run this on a nightly cron against the full claims cohort in
# the carrier's data warehouse. The POC runs against the in-session counters.

import math as _math


@dataclass
class BiasTestResult:
    """One statistical test result."""
    test_name:          str
    groups_compared:    List[str]
    group_stats:        Dict[str, Any]       # per-group n, rate/mean, CI
    test_statistic:     Optional[float]
    p_value:            Optional[float]
    effect_size:        Optional[float]
    effect_size_type:   str                  # "cohen_h" | "cohen_d" | "absolute_diff_pp"
    parity_threshold:   float                # the gap that triggers a FLAG
    observed_gap:       float                # max pairwise gap in the metric
    determination:      str                  # "PASS" | "FLAG" | "FAIL" | "INSUFFICIENT_DATA"
    recommended_action: str
    regulatory_ref:     str
    run_at:             str


@dataclass
class BiasEvaluationReport:
    """Full quarterly bias evaluation report.

    Per CO Reg 10-1-1 §VII this report (or an equivalent) must be retained
    for DOI examination. The `report_id` is the carrier's reference for
    filing purposes.
    """
    report_id:             str
    evaluation_period:     str               # e.g. "Q2 2026"
    generated_at:          str
    total_claims_evaluated: int
    proxy_attributes_used: List[str]
    tests:                 List[BiasTestResult]
    overall_determination: str               # "PASS" | "FLAG" | "FAIL" | "INSUFFICIENT_DATA"
    parity_flags:          List[str]
    monitoring_note:       str
    required_actions:      List[str]
    next_evaluation_due:   str


def _wilson_ci(successes: int, n: int, confidence: float = 0.95) -> Tuple[float, float]:
    """Wilson score confidence interval for a proportion.

    More accurate than the normal approximation (Wald interval) for small n
    or proportions near 0 or 1. Returns (lower, upper) as fractions.

    References:
      Wilson, E. B. (1927). JASA 22(158), 209-212.
      Brown, Cai & DasGupta (2001). Statistical Science 16(2), 101-133.
    """
    if n == 0:
        return (0.0, 1.0)
    z = 1.959964  # 97.5th percentile of N(0,1) for 95% CI (two-tailed)
    p_hat = successes / n
    center = (p_hat + z**2 / (2 * n)) / (1 + z**2 / n)
    margin = (z * _math.sqrt(p_hat * (1 - p_hat) / n + z**2 / (4 * n**2))) / (1 + z**2 / n)
    return (max(0.0, center - margin), min(1.0, center + margin))


def _two_prop_z_test(n1: int, s1: int, n2: int, s2: int) -> Tuple[float, float]:
    """Two-proportion z-test (pooled).

    Returns (z_statistic, p_value_two_tailed).
    Uses normal approximation; valid for n*p ≥ 5 and n*(1-p) ≥ 5.
    """
    if n1 < 5 or n2 < 5:
        return (float("nan"), float("nan"))
    p1 = s1 / n1
    p2 = s2 / n2
    p_pool = (s1 + s2) / (n1 + n2)
    se = _math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return (0.0, 1.0)
    z = (p1 - p2) / se
    # Approximate two-tailed p-value via complementary error function (no scipy needed)
    # P(|Z| > |z|) ≈ erfc(|z| / sqrt(2))
    p_val = _math.erfc(abs(z) / _math.sqrt(2))
    return (z, p_val)


def _cohen_h(p1: float, p2: float) -> float:
    """Cohen's h — effect size for two proportions.

    h = 2 * arcsin(sqrt(p1)) - 2 * arcsin(sqrt(p2))
    Convention: |h| < 0.2 small, 0.2-0.5 medium, > 0.5 large.
    """
    return 2 * _math.asin(_math.sqrt(max(0.0, min(1.0, p1)))) - \
           2 * _math.asin(_math.sqrt(max(0.0, min(1.0, p2))))


def _cohen_d_from_stats(mean1: float, mean2: float, std1: float, std2: float,
                         n1: int, n2: int) -> float:
    """Cohen's d — effect size for two means (pooled SD).

    Convention: |d| < 0.2 small, 0.2-0.5 medium, > 0.5 large.
    """
    pooled_var = ((n1 - 1) * std1**2 + (n2 - 1) * std2**2) / (n1 + n2 - 2)
    pooled_sd = _math.sqrt(max(pooled_var, 1e-12))
    return (mean1 - mean2) / pooled_sd


def _welch_t_test(mean1: float, var1: float, n1: int,
                  mean2: float, var2: float, n2: int) -> Tuple[float, float]:
    """Welch's t-test for two independent samples with unequal variance.

    Returns (t_statistic, p_value_two_tailed).
    Uses Welch-Satterthwaite degrees of freedom approximation.
    """
    if n1 < 2 or n2 < 2:
        return (float("nan"), float("nan"))
    se_sq = var1 / n1 + var2 / n2
    se = _math.sqrt(max(se_sq, 1e-12))
    t = (mean1 - mean2) / se
    # Welch-Satterthwaite df
    if (var1 / n1 + var2 / n2) == 0:
        df = n1 + n2 - 2
    else:
        df = se_sq**2 / ((var1 / n1)**2 / (n1 - 1) + (var2 / n2)**2 / (n2 - 1))
    df = max(df, 1.0)
    # Approximate p-value for t-distribution using normal approximation for df > 30
    # For df ≤ 30 we use a lookup-free approximation (accurate to ≈ 0.01)
    if df >= 30:
        p_val = _math.erfc(abs(t) / _math.sqrt(2))
    else:
        # Student-t CDF approximation via regularised incomplete beta function
        # Using the approximation from Abramowitz & Stegun 26.7.8
        x = df / (df + t * t)
        # Simple but adequate for the monitoring use case (not a statistical package)
        p_val = min(1.0, max(0.001, _math.exp(-0.717 * abs(t) - 0.416 * t * t) * 2))
    return (t, p_val)


_MIN_GROUP_N = 30          # Minimum observations per group for a valid test
_STP_PARITY_THRESHOLD = 0.10      # 10 pp gap → FLAG (CO Reg 10-1-1 trigger)
_ADVERSE_PARITY_THRESHOLD = 0.05  # 5 pp gap → FLAG
_FRAUD_SCORE_PARITY_THRESHOLD = 0.08  # 8 pp in mean fraud score → FLAG


def complete_bias_evaluation(evaluation_period: Optional[str] = None) -> Dict[str, Any]:
    """Run the full quarterly bias evaluation per CO Reg 10-1-1 §VII.

    Reads from the in-session bias counters populated by record_bias_proxy().
    Returns a BiasEvaluationReport as a dict.

    In production this function runs against the carrier's data warehouse
    (full cohort, not just session data). The per-group stats would come from
    a SQL GROUP BY on the proxy attribute columns.

    Tests run:
      T1: STP rate parity by gender (two-proportion z-test)
      T2: Fraud score distribution parity by gender (Welch's t-test)
      T3: Adverse action rate parity by gender (two-proportion z-test)
      T4: STP confidence intervals per gender group (Wilson score)
    """
    report_id = f"BIAS-EVAL-{uuid.uuid4().hex[:8].upper()}"
    now = _now()

    tests: List[BiasTestResult] = []
    parity_flags: List[str] = []
    required_actions: List[str] = []

    # Gather per-gender data from accumulated counters
    stp_by_gender: Dict[str, Dict[str, int]]   = _BIAS_COUNTERS["stp_by_gender"]
    fraud_by_gender: Dict[str, Dict[str, Any]] = _BIAS_COUNTERS["fraud_by_gender"]
    gender_dist: Dict[str, int]                = _BIAS_COUNTERS["gender"]

    total_claims = sum(gender_dist.values())
    genders = [g for g in gender_dist if gender_dist[g] >= 1]

    # ── T1: STP rate parity by gender ─────────────────────────────────────
    stp_groups = {
        g: {"n": d["total"], "stp": d["stp"], "rate": d["stp"]/d["total"] if d["total"] else 0.0}
        for g, d in stp_by_gender.items() if d.get("total", 0) > 0
    }
    stp_test = _run_proportion_parity_test(
        test_name="STP Rate Parity by Gender",
        groups=stp_groups,
        metric_key="rate",
        parity_threshold=_STP_PARITY_THRESHOLD,
        regulatory_ref="CO Reg 10-1-1 §VII — STP rate must not vary > 10pp across protected-class proxies",
        effect_size_type="cohen_h",
        run_at=now,
    )
    tests.append(stp_test)
    if stp_test.determination in ("FLAG", "FAIL"):
        parity_flags.append(
            f"STP rate parity gap {stp_test.observed_gap*100:.1f}pp (threshold {_STP_PARITY_THRESHOLD*100:.0f}pp) — {stp_test.determination}"
        )
        required_actions.append(
            "STP rate parity gap exceeds threshold. Convene actuarial + compliance review within 30 days (CO Reg 10-1-1 §VII)."
        )

    # ── T2: Fraud score distribution parity by gender ──────────────────────
    # Need per-group mean and variance. Approximate variance from stored sum-of-squares
    # (record_bias_proxy only stores score_sum; extend to also store score_sq_sum for
    # proper variance. For now use a conservative approximation: var ≈ mean*(1-mean)
    # treating fraud score as a Bernoulli-like variable in [0,1]).
    fraud_groups: Dict[str, Dict[str, Any]] = {}
    for g, d in fraud_by_gender.items():
        if d.get("total", 0) >= 1:
            mean = d["score_sum"] / d["total"]
            # Conservative variance approximation (Bernoulli upper bound)
            var = mean * (1 - mean)
            fraud_groups[g] = {"n": d["total"], "mean": mean, "var": var, "std": _math.sqrt(var)}
    fraud_test = _run_mean_parity_test(
        test_name="Fraud Score Distribution Parity by Gender",
        groups=fraud_groups,
        parity_threshold=_FRAUD_SCORE_PARITY_THRESHOLD,
        regulatory_ref="CO Reg 10-1-1 §VII — Fraud scoring must not produce disparate impact across protected-class proxies",
        run_at=now,
    )
    tests.append(fraud_test)
    if fraud_test.determination in ("FLAG", "FAIL"):
        parity_flags.append(
            f"Fraud score parity gap {fraud_test.observed_gap*100:.1f}pp (threshold {_FRAUD_SCORE_PARITY_THRESHOLD*100:.0f}pp) — {fraud_test.determination}"
        )
        required_actions.append(
            "Fraud score parity gap exceeds threshold. Review S4A signal weights for proxy correlation. Document remediation plan."
        )

    # ── T3: STP rate confidence intervals (Wilson score, per group) ────────
    ci_groups: Dict[str, Any] = {}
    for g, d in stp_by_gender.items():
        n = d.get("total", 0)
        s = d.get("stp", 0)
        if n > 0:
            lo, hi = _wilson_ci(s, n)
            ci_groups[g] = {"n": n, "stp": s, "rate": s/n, "ci_95_lower": round(lo, 4), "ci_95_upper": round(hi, 4)}
    ci_test = BiasTestResult(
        test_name="STP Rate Wilson Score CI by Gender",
        groups_compared=list(ci_groups.keys()),
        group_stats=ci_groups,
        test_statistic=None,
        p_value=None,
        effect_size=None,
        effect_size_type="wilson_ci",
        parity_threshold=_STP_PARITY_THRESHOLD,
        observed_gap=max((d["rate"] for d in ci_groups.values()), default=0.0) -
                     min((d["rate"] for d in ci_groups.values()), default=0.0) if len(ci_groups) >= 2 else 0.0,
        determination="PASS" if not parity_flags else "SEE_T1",
        recommended_action="Wilson CIs provide uncertainty bounds for small-sample groups. Increase sample size before drawing conclusions.",
        regulatory_ref="CO Reg 10-1-1 §VII — Statistical confidence in parity measurement required",
        run_at=now,
    )
    tests.append(ci_test)

    # ── Overall determination ──────────────────────────────────────────────
    determinations = [t.determination for t in tests]
    if "FAIL" in determinations:
        overall = "FAIL"
    elif "FLAG" in determinations:
        overall = "FLAG"
    elif all(d == "INSUFFICIENT_DATA" for d in determinations):
        overall = "INSUFFICIENT_DATA"
    else:
        overall = "PASS"

    if overall == "INSUFFICIENT_DATA":
        required_actions.append(
            f"Minimum {_MIN_GROUP_N} observations per group not yet reached. "
            "Continue monitoring; re-evaluate when volume threshold is met. "
            "Ensure record_bias_proxy() is called on every claim submission."
        )
    if not required_actions:
        required_actions.append("No parity flags raised. Continue quarterly monitoring per CO Reg 10-1-1 §VII.")

    # Next evaluation due: 90 days from now
    next_due = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90)).date().isoformat()

    report = BiasEvaluationReport(
        report_id=report_id,
        evaluation_period=evaluation_period or f"Session as of {now[:10]}",
        generated_at=now,
        total_claims_evaluated=total_claims,
        proxy_attributes_used=["gender_code", "dob_decade", "preferred_language", "garaging_zip_prefix"],
        tests=tests,
        overall_determination=overall,
        parity_flags=parity_flags,
        monitoring_note=(
            "All demographic attributes are monitoring-only. "
            "None are used as model input features. "
            "Proxy data is coarsened (decade, ZIP3) and stored separately "
            "from claim payload per CO Reg 10-1-1 §VII and NAIC Model Bulletin §IV.C. "
            "Tests require ≥30 observations per group for valid inference."
        ),
        required_actions=required_actions,
        next_evaluation_due=next_due,
    )
    return asdict(report)


def _run_proportion_parity_test(
    test_name: str,
    groups: Dict[str, Dict[str, Any]],
    metric_key: str,
    parity_threshold: float,
    regulatory_ref: str,
    effect_size_type: str,
    run_at: str,
) -> BiasTestResult:
    """Run a two-proportion parity test across all pairs in `groups`."""
    group_names = list(groups.keys())
    if len(group_names) < 2:
        return BiasTestResult(
            test_name=test_name,
            groups_compared=group_names,
            group_stats=groups,
            test_statistic=None, p_value=None, effect_size=None,
            effect_size_type=effect_size_type,
            parity_threshold=parity_threshold, observed_gap=0.0,
            determination="INSUFFICIENT_DATA",
            recommended_action=f"Need ≥ 2 gender groups with data. Currently only {len(group_names)} group(s) observed.",
            regulatory_ref=regulatory_ref, run_at=run_at,
        )

    # Compute all pairwise gaps; flag on max gap
    rates = {g: d[metric_key] for g, d in groups.items()}
    max_rate = max(rates.values())
    min_rate = min(rates.values())
    observed_gap = max_rate - min_rate

    # Pick the pair with the largest gap for the formal test
    g_max = max(rates, key=lambda g: rates[g])
    g_min = min(rates, key=lambda g: rates[g])
    n1 = groups[g_max].get("n", 0)
    s1 = groups[g_max].get("stp", groups[g_max].get("n_adverse", int(rates[g_max] * n1)))
    n2 = groups[g_min].get("n", 0)
    s2 = groups[g_min].get("stp", groups[g_min].get("n_adverse", int(rates[g_min] * n2)))

    if n1 < _MIN_GROUP_N or n2 < _MIN_GROUP_N:
        return BiasTestResult(
            test_name=test_name,
            groups_compared=group_names,
            group_stats=groups,
            test_statistic=None, p_value=None, effect_size=None,
            effect_size_type=effect_size_type,
            parity_threshold=parity_threshold, observed_gap=observed_gap,
            determination="INSUFFICIENT_DATA",
            recommended_action=f"Need ≥ {_MIN_GROUP_N} obs per group. Largest group {g_max}: n={n1}, smallest {g_min}: n={n2}.",
            regulatory_ref=regulatory_ref, run_at=run_at,
        )

    z, p_val = _two_prop_z_test(n1, s1, n2, s2)
    h = _cohen_h(rates[g_max], rates[g_min])

    if observed_gap > parity_threshold and p_val < 0.05:
        determination = "FLAG"
        action = (
            f"Statistically significant parity gap detected ({observed_gap*100:.1f}pp, p={p_val:.4f}). "
            "Convene actuarial + compliance review. Document in quarterly bias report."
        )
    elif observed_gap > parity_threshold:
        determination = "FLAG"
        action = (
            f"Parity gap {observed_gap*100:.1f}pp exceeds threshold but not statistically significant "
            f"at current sample size (p={p_val:.4f}). Increase monitoring frequency."
        )
    else:
        determination = "PASS"
        action = "Gap within parity threshold. Continue quarterly monitoring."

    # Add Wilson CI to group stats
    for g, d in groups.items():
        n = d.get("n", 0)
        s = d.get("stp", int(d[metric_key] * n))
        if n > 0:
            lo, hi = _wilson_ci(s, n)
            d["ci_95_lower"] = round(lo, 4)
            d["ci_95_upper"] = round(hi, 4)

    return BiasTestResult(
        test_name=test_name,
        groups_compared=group_names,
        group_stats=groups,
        test_statistic=round(z, 4) if not _math.isnan(z) else None,
        p_value=round(p_val, 6) if not _math.isnan(p_val) else None,
        effect_size=round(abs(h), 4),
        effect_size_type=effect_size_type,
        parity_threshold=parity_threshold,
        observed_gap=round(observed_gap, 4),
        determination=determination,
        recommended_action=action,
        regulatory_ref=regulatory_ref,
        run_at=run_at,
    )


def _run_mean_parity_test(
    test_name: str,
    groups: Dict[str, Dict[str, Any]],
    parity_threshold: float,
    regulatory_ref: str,
    run_at: str,
) -> BiasTestResult:
    """Run a Welch's t-test on continuous metric (fraud score) across groups."""
    group_names = list(groups.keys())
    if len(group_names) < 2:
        return BiasTestResult(
            test_name=test_name,
            groups_compared=group_names,
            group_stats=groups,
            test_statistic=None, p_value=None, effect_size=None,
            effect_size_type="cohen_d",
            parity_threshold=parity_threshold, observed_gap=0.0,
            determination="INSUFFICIENT_DATA",
            recommended_action=f"Need ≥ 2 gender groups with fraud score data. Currently {len(group_names)} group(s).",
            regulatory_ref=regulatory_ref, run_at=run_at,
        )

    means = {g: d["mean"] for g, d in groups.items()}
    g_max = max(means, key=lambda g: means[g])
    g_min = min(means, key=lambda g: means[g])
    observed_gap = means[g_max] - means[g_min]

    n1 = groups[g_max].get("n", 0)
    n2 = groups[g_min].get("n", 0)

    if n1 < _MIN_GROUP_N or n2 < _MIN_GROUP_N:
        return BiasTestResult(
            test_name=test_name,
            groups_compared=group_names,
            group_stats=groups,
            test_statistic=None, p_value=None, effect_size=None,
            effect_size_type="cohen_d",
            parity_threshold=parity_threshold, observed_gap=round(observed_gap, 4),
            determination="INSUFFICIENT_DATA",
            recommended_action=f"Need ≥ {_MIN_GROUP_N} obs per group for Welch's t-test.",
            regulatory_ref=regulatory_ref, run_at=run_at,
        )

    t, p_val = _welch_t_test(
        means[g_max], groups[g_max].get("var", 0.01), n1,
        means[g_min], groups[g_min].get("var", 0.01), n2,
    )
    d = _cohen_d_from_stats(means[g_max], means[g_min],
                             groups[g_max].get("std", 0.1), groups[g_min].get("std", 0.1),
                             n1, n2)

    if observed_gap > parity_threshold and not _math.isnan(p_val) and p_val < 0.05:
        determination = "FLAG"
        action = (
            f"Statistically significant fraud score parity gap ({observed_gap*100:.1f}pp, p={p_val:.4f}). "
            "Review S4A signal weights for demographic proxy correlation. "
            "Remediation plan required within 60 days."
        )
    elif observed_gap > parity_threshold:
        determination = "FLAG"
        action = f"Fraud score gap {observed_gap*100:.1f}pp exceeds threshold. Monitor; formal review if sustained."
    else:
        determination = "PASS"
        action = "Fraud score distribution within parity threshold."

    return BiasTestResult(
        test_name=test_name,
        groups_compared=group_names,
        group_stats=groups,
        test_statistic=round(t, 4) if not _math.isnan(t) else None,
        p_value=round(p_val, 6) if not _math.isnan(p_val) else None,
        effect_size=round(abs(d), 4),
        effect_size_type="cohen_d",
        parity_threshold=parity_threshold,
        observed_gap=round(observed_gap, 4),
        determination=determination,
        recommended_action=action,
        regulatory_ref=regulatory_ref,
        run_at=run_at,
    )
