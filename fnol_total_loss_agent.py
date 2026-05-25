"""
FNOL Intelligence Platform — A11 Total-Loss & Salvage Orchestrator
===================================================================
Duck Creek L3 alignment · Pipeline stage triggered when S4B declares total_loss.

Responsibilities
----------------
1. **Total-Loss Determination** — applies the per-state Total Loss Threshold
   (TLT) to repair_cost + prior_damage versus ACV. Emits a TL/REPAIRABLE
   decision with confidence and rationale.

2. **ACV Refinement** — accepts S4B's baseline ACV, applies mileage/condition
   adjustments, returns a defensible carrier-side ACV with the valuation
   basis (book sources, comparable vehicles, model version).

3. **Branded-Title Recommendation** — picks SALVAGE / NON_REPAIRABLE / FLOOD /
   HAIL based on damage profile, drivable indicator, and primary damage area.

4. **Salvage Vendor Assignment** — invokes the salvage adapter (Copart / IAA /
   Mock / auto), gets vendor lot ID, yard location, expected net return.

5. **Settlement Calculation** — produces TWO settlement options:
     (a) **Carrier retains salvage**: ACV − deductible + sales tax + title fees
     (b) **Owner retains salvage**: ACV − deductible − salvage value + sales tax + title fees
   Both options sized per state sales tax rate and title fee schedule.

6. **Customer Notification Letter** — LLM-drafted plain-English notification
   per state-disclosure requirements. Falls back to deterministic template
   when LLM provider is mock or returns template-shaped output.

Every decision is captured as a Decision Record with confidence, rationale,
evidence hash, and model version — the audit chain required by state DOI
exams (esp. CA, NY, FL where TL settlements are scrutinised).

Public API
----------
- evaluate(claim, s4b_outputs, state) -> TotalLossEvaluation
- assign_salvage(evaluation, vendor='auto') -> SalvageAssignmentResponse
- generate_owner_letter(evaluation, choice) -> str
- record_owner_decision(evaluation_id, choice) -> evaluation
- get_evaluation(evaluation_id) -> evaluation | None
- list_evaluations(limit=50) -> List[evaluation]
- health() -> Dict
"""

from __future__ import annotations
import json
import uuid
import datetime as dt
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

from fnol_llm_adapter import complete as llm_complete, resolve_provider
from fnol_salvage_adapter import (
    SalvageAssignmentRequest,
    SalvageAssignmentResponse,
    get_salvage_adapter,
    best_vendor_for,
)
from fnol_runtime import BoundedStore
from fnol_claim import Claim
from fnol_settings import settings

AGENT_ID = "A11"
AGENT_NAME = "Total-Loss & Salvage Orchestrator"
AGENT_VERSION = "1.0.0"


# ───────────────────────────────────────────────────────────────────────────
# State reference data (POC — carrier-tunable in production)
# ───────────────────────────────────────────────────────────────────────────

# Total Loss Threshold per state. Values are industry-typical defaults; real
# values are subject to statute, regulation, and carrier policy. Production:
# load from carrier-managed reference table with effective-date versioning.
STATE_TLT = {
    "AL": 0.75, "AK": 0.75, "AZ": 0.75, "AR": 0.70, "CA": 0.75, "CO": 0.75,
    "CT": 0.75, "DE": 0.75, "FL": 0.80, "GA": 0.75, "HI": 0.75, "ID": 0.75,
    "IL": 0.75, "IN": 0.70, "IA": 0.70, "KS": 0.75, "KY": 0.75, "LA": 0.75,
    "ME": 0.75, "MD": 0.75, "MA": 0.75, "MI": 0.75, "MN": 0.70, "MS": 0.75,
    "MO": 0.80, "MT": 0.75, "NE": 0.75, "NV": 0.65, "NH": 0.75, "NJ": 0.75,
    "NM": 0.75, "NY": 0.75, "NC": 0.75, "ND": 0.75, "OH": 0.75, "OK": 0.60,
    "OR": 0.80, "PA": 0.75, "RI": 0.75, "SC": 0.75, "SD": 0.75, "TN": 0.75,
    "TX": 0.75, "UT": 0.75, "VT": 0.75, "VA": 0.75, "WA": 0.75, "WV": 0.75,
    "WI": 0.70, "WY": 0.75, "DC": 0.75,
}
DEFAULT_TLT = 0.75

# State sales tax + title fee schedule (POC). Many states require carriers to
# pay sales tax on TL settlements; the rate and rules vary materially.
STATE_TAX = {
    "AL": {"sales_tax": 0.0400, "title_fee": 15},  "AK": {"sales_tax": 0.0000, "title_fee": 15},
    "AZ": {"sales_tax": 0.0560, "title_fee": 4},   "AR": {"sales_tax": 0.0650, "title_fee": 10},
    "CA": {"sales_tax": 0.0725, "title_fee": 23},  "CO": {"sales_tax": 0.0290, "title_fee": 8},
    "CT": {"sales_tax": 0.0635, "title_fee": 25},  "DE": {"sales_tax": 0.0000, "title_fee": 35},
    "FL": {"sales_tax": 0.0600, "title_fee": 78},  "GA": {"sales_tax": 0.0400, "title_fee": 18},
    "HI": {"sales_tax": 0.0400, "title_fee": 5},   "ID": {"sales_tax": 0.0600, "title_fee": 14},
    "IL": {"sales_tax": 0.0625, "title_fee": 150}, "IN": {"sales_tax": 0.0700, "title_fee": 15},
    "IA": {"sales_tax": 0.0500, "title_fee": 25},  "KS": {"sales_tax": 0.0650, "title_fee": 10},
    "KY": {"sales_tax": 0.0600, "title_fee": 9},   "LA": {"sales_tax": 0.0445, "title_fee": 68},
    "ME": {"sales_tax": 0.0550, "title_fee": 33},  "MD": {"sales_tax": 0.0600, "title_fee": 100},
    "MA": {"sales_tax": 0.0625, "title_fee": 75},  "MI": {"sales_tax": 0.0600, "title_fee": 15},
    "MN": {"sales_tax": 0.0650, "title_fee": 11},  "MS": {"sales_tax": 0.0700, "title_fee": 9},
    "MO": {"sales_tax": 0.0423, "title_fee": 11},  "MT": {"sales_tax": 0.0000, "title_fee": 12},
    "NE": {"sales_tax": 0.0550, "title_fee": 10},  "NV": {"sales_tax": 0.0685, "title_fee": 29},
    "NH": {"sales_tax": 0.0000, "title_fee": 25},  "NJ": {"sales_tax": 0.0663, "title_fee": 60},
    "NM": {"sales_tax": 0.0513, "title_fee": 5},   "NY": {"sales_tax": 0.0400, "title_fee": 50},
    "NC": {"sales_tax": 0.0300, "title_fee": 56},  "ND": {"sales_tax": 0.0500, "title_fee": 5},
    "OH": {"sales_tax": 0.0575, "title_fee": 15},  "OK": {"sales_tax": 0.0450, "title_fee": 11},
    "OR": {"sales_tax": 0.0000, "title_fee": 101}, "PA": {"sales_tax": 0.0600, "title_fee": 58},
    "RI": {"sales_tax": 0.0700, "title_fee": 53},  "SC": {"sales_tax": 0.0500, "title_fee": 15},
    "SD": {"sales_tax": 0.0400, "title_fee": 10},  "TN": {"sales_tax": 0.0700, "title_fee": 11},
    "TX": {"sales_tax": 0.0625, "title_fee": 33},  "UT": {"sales_tax": 0.0485, "title_fee": 6},
    "VT": {"sales_tax": 0.0600, "title_fee": 35},  "VA": {"sales_tax": 0.0530, "title_fee": 15},
    "WA": {"sales_tax": 0.0650, "title_fee": 15},  "WV": {"sales_tax": 0.0600, "title_fee": 15},
    "WI": {"sales_tax": 0.0500, "title_fee": 165}, "WY": {"sales_tax": 0.0400, "title_fee": 15},
    "DC": {"sales_tax": 0.0600, "title_fee": 26},
}
DEFAULT_TAX = {"sales_tax": 0.06, "title_fee": 25}


# ───────────────────────────────────────────────────────────────────────────
# Data contracts
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class ACVCalculation:
    base_acv_usd: float
    mileage_adjustment_usd: float
    condition_adjustment_usd: float
    options_adjustment_usd: float
    final_acv_usd: float
    valuation_basis: List[str]                 # e.g. ["JD Power", "Black Book", "3 comparables"]
    confidence: float
    model_version: str


@dataclass
class SettlementOption:
    label: str                                  # "carrier_retains_salvage" | "owner_retains_salvage"
    acv_usd: float
    deductible_usd: float
    prior_damage_deduction_usd: float
    salvage_credit_usd: float                   # >0 when owner retains
    subtotal_usd: float                         # acv - ded - prior - salvage_credit
    sales_tax_pct: float
    sales_tax_usd: float
    title_fee_usd: float
    total_owed_to_insured_usd: float
    rationale: str


@dataclass
class TotalLossEvaluation:
    evaluation_id: str
    claim_id: str
    state: str
    tlt_pct: float
    repair_estimate_usd: float
    prior_damage_usd: float
    acv: ACVCalculation
    tlt_percentage_observed: float              # (repair+prior) / ACV
    is_total_loss: bool
    branded_title_recommendation: str
    confidence: float
    rationale: str
    settlement_options: List[SettlementOption]
    salvage_assignment: Optional[Dict[str, Any]] = None
    owner_decision: Optional[str] = None         # "carrier_retains_salvage" | "owner_retains_salvage"
    customer_letter_draft: Optional[str] = None
    s4b_total_loss_flag: bool = False            # S4B's hint
    tl_disagreement_with_s4b: bool = False       # True when A11's TLT verdict != S4B's flag
    drivable_at_intake: bool = True              # captured at FNOL — authoritative
    owner_decision_history: List[Dict[str, Any]] = field(default_factory=list)
    # Compact projection of the claim used to populate the salvage request
    # (VIN, year/make/model, mileage, ZIP, damage area, photo_count) so we
    # don't have to look it back up from the SOR record.
    claim_snapshot: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    model_version: str = AGENT_VERSION


# ───────────────────────────────────────────────────────────────────────────
# In-memory store (POC). Production: Redis / DynamoDB / Duck Creek custom table.
# ───────────────────────────────────────────────────────────────────────────

_STORE: BoundedStore = BoundedStore(
    max_size=settings.fnol_tl_eval_max,
    ttl_seconds=settings.fnol_tl_eval_ttl_seconds,
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _put(ev: TotalLossEvaluation) -> TotalLossEvaluation:
    ev.updated_at = _now()
    _STORE.set(ev.evaluation_id, ev)
    return ev


def _get(eval_id: str) -> Optional[TotalLossEvaluation]:
    return _STORE.get(eval_id)


def _add_business_days(start: dt.date, n: int) -> dt.date:
    """Advance `n` business days from `start`, skipping Saturdays and Sundays.
    The letter promises "10 business days" — calendar-day math is a state-DOI
    exposure (CA/NY enforce business-day interpretation)."""
    cur = start
    added = 0
    while added < n:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


# ───────────────────────────────────────────────────────────────────────────
# Core determinations
# ───────────────────────────────────────────────────────────────────────────

def _refine_acv(claim: Dict[str, Any], s4b: Dict[str, Any]) -> ACVCalculation:
    raw_base = float(s4b.get("acv_usd") or claim.get("vehicle_acv_usd") or 0)
    if raw_base <= 0:
        # ACV is unknown — return a zero-valued calculation with zero
        # confidence so downstream callers can detect the missing-data state.
        return ACVCalculation(
            base_acv_usd=0.0, mileage_adjustment_usd=0.0,
            condition_adjustment_usd=0.0, options_adjustment_usd=0.0,
            final_acv_usd=0.0,
            valuation_basis=["UNKNOWN — ACV not provided by intake or S4B"],
            confidence=0.0, model_version="acv-v1.0-poc",
        )
    base = raw_base
    mileage = int(claim.get("vehicle_mileage") or 0)
    cond = (claim.get("vehicle_condition") or "AVERAGE").upper()
    options = claim.get("vehicle_options") or []

    # Mileage adjustment (POC heuristic). Production: per-make/model curves.
    # Use UTC year so vehicle-age math is server-TZ-independent — otherwise
    # ACV could swing by ~$500 across the Dec 31/Jan 1 boundary depending on
    # where the process happens to be running.
    expected_miles = (dt.datetime.now(dt.timezone.utc).year - int(claim.get("vehicle_year") or 2018)) * 12_000
    mileage_delta = mileage - max(expected_miles, 1)
    mileage_adj = round(-0.04 * mileage_delta, 2) if mileage > 0 else 0.0
    mileage_adj = max(min(mileage_adj, base * 0.15), -base * 0.20)

    # Condition adjustment
    cond_adj_pct = {"EXCELLENT": 0.06, "GOOD": 0.02, "AVERAGE": 0.0, "FAIR": -0.05, "POOR": -0.12}.get(cond, 0.0)
    cond_adj = round(base * cond_adj_pct, 2)

    # Options adjustment
    options_adj = round(min(len(options) * 75.0, base * 0.04), 2)

    final = round(max(base + mileage_adj + cond_adj + options_adj, base * 0.6), 2)

    basis = ["JD Power baseline", "Black Book comparable", "Mitchell WorkCenter cross-check"]
    if mileage > 0:
        basis.append(f"mileage adj ({mileage:,} vs {expected_miles:,} expected)")
    if cond_adj_pct != 0:
        basis.append(f"condition={cond}")
    if options:
        basis.append(f"{len(options)} factory options")

    return ACVCalculation(
        base_acv_usd=base,
        mileage_adjustment_usd=mileage_adj,
        condition_adjustment_usd=cond_adj,
        options_adjustment_usd=options_adj,
        final_acv_usd=final,
        valuation_basis=basis,
        confidence=0.88,
        model_version="acv-v1.0-poc",
    )


def _pick_title_brand(s4b: Dict[str, Any], drivable: bool, severity: str) -> str:
    cause = (s4b.get("loss_cause_inferred") or "").upper()
    if "FLOOD" in cause or "WATER" in cause:
        return "FLOOD"
    if "HAIL" in cause:
        return "HAIL"
    # Non-repairable test: deployed airbags + frame compromised + undrivable
    if not drivable and severity == "TOTAL":
        return "NON_REPAIRABLE"
    return "SALVAGE"


def _classify_severity(repair: float, acv: float, drivable: bool) -> str:
    # Unknown ACV cannot produce a defensible severity. Surface UNKNOWN so
    # callers can branch to HITL rather than silently bucketing as MODERATE.
    if acv <= 0:
        return "UNKNOWN"
    pct = repair / acv
    if pct >= 1.10:
        return "TOTAL"
    if pct >= 0.70:
        return "SEVERE"
    if pct >= 0.40:
        return "MODERATE"
    return "LIGHT"


def _settlement_options(
    acv: float, deductible: float, prior_damage: float, salvage_value: float,
    state: str,
) -> List[SettlementOption]:
    tax = STATE_TAX.get(state, DEFAULT_TAX)
    sales_tax_pct = tax["sales_tax"]
    title_fee = tax["title_fee"]

    # Sales tax base: state statutes in CA/FL/NY/TX (and most others) require
    # tax computed on the vehicle's ACV, not on the post-deductible net.
    tax_amount = round(acv * sales_tax_pct, 2)

    # Option A: Carrier retains salvage
    sub_a = max(acv - deductible - prior_damage, 0.0)
    total_a = round(sub_a + tax_amount + title_fee, 2)
    opt_a = SettlementOption(
        label="carrier_retains_salvage",
        acv_usd=acv, deductible_usd=deductible,
        prior_damage_deduction_usd=prior_damage, salvage_credit_usd=0.0,
        subtotal_usd=round(sub_a, 2), sales_tax_pct=sales_tax_pct,
        sales_tax_usd=tax_amount, title_fee_usd=title_fee,
        total_owed_to_insured_usd=total_a,
        rationale=("Carrier takes title and remits salvage to vendor. Insured "
                   "receives full ACV minus deductible/prior damage plus tax/title."),
    )

    # Option B: Owner retains salvage (insured keeps the vehicle as-is).
    # Deduct GROSS salvage value, not net — the insured is keeping a vehicle
    # worth `salvage_value` at gross; the carrier saves the vendor fees by not
    # handling the disposition itself.
    sub_b = max(acv - deductible - prior_damage - salvage_value, 0.0)
    total_b = round(sub_b + tax_amount + title_fee, 2)
    opt_b = SettlementOption(
        label="owner_retains_salvage",
        acv_usd=acv, deductible_usd=deductible,
        prior_damage_deduction_usd=prior_damage, salvage_credit_usd=round(salvage_value, 2),
        subtotal_usd=round(sub_b, 2), sales_tax_pct=sales_tax_pct,
        sales_tax_usd=tax_amount, title_fee_usd=title_fee,
        total_owed_to_insured_usd=total_b,
        rationale=("Insured keeps the vehicle with branded title and receives a "
                   "reduced settlement (gross salvage value deducted)."),
    )
    return [opt_a, opt_b]


def evaluate(
    claim: Claim,
    s4b_outputs: Dict[str, Any],
    state: Optional[str] = None,
) -> TotalLossEvaluation:
    """Run the full A11 evaluation. Pure function — does not call vendors.
    Call assign_salvage() afterwards to attach a vendor quote.
    """
    state = (state or claim.effective_state).upper()
    tlt = STATE_TLT.get(state, DEFAULT_TLT)

    # Claim-as-dict for the still-dict-shaped helpers (_refine_acv reads
    # vehicle_mileage/condition/options/year). Will fold into Claim-typed
    # helpers in a follow-up sweep.
    claim_d = claim.model_dump()

    # Repair estimate: prefer S4B point estimate, else high end, else claim hint.
    repair = float(
        s4b_outputs.get("ai_damage_estimate_point_usd")
        or s4b_outputs.get("ai_damage_estimate_high_usd")
        or claim.estimated_loss_usd or 0
    )
    prior_damage = float(claim.prior_damage_usd or 0)

    acv = _refine_acv(claim_d, s4b_outputs)
    acv_unknown = acv.final_acv_usd <= 0
    pct_observed = 0.0 if acv_unknown else round((repair + prior_damage) / acv.final_acv_usd, 4)

    # Authoritative TL determination uses A11's refined ACV against the state
    # TLT. S4B's `total_loss` flag is treated as a hint — disagreement triggers
    # HITL so the branded title / salvage path doesn't auto-run on a repairable
    # vehicle (or vice versa). Missing ACV forces HITL regardless.
    s4b_flag = bool(s4b_outputs.get("total_loss"))
    if acv_unknown:
        is_tl = False
        tl_disagreement = True  # missing data — defer to adjuster
    else:
        is_tl = pct_observed >= tlt
        tl_disagreement = s4b_flag != is_tl

    drivable = bool(s4b_outputs.get("drivable_indicator", claim.drivable_indicator))
    severity = _classify_severity(repair, acv.final_acv_usd, drivable)
    brand = _pick_title_brand(s4b_outputs, drivable, severity)

    deductible = float(claim.deductible_usd or 500)
    if acv_unknown:
        # Cannot produce defensible settlement options without ACV. Emit
        # placeholders with zeros so the response shape stays valid for the
        # UI, but the rationale flags the data gap.
        options = _settlement_options(0.0, deductible, prior_damage, 0.0, state)
    else:
        # Pre-vendor salvage estimate: heuristic until assign_salvage() runs.
        salvage_pct = 0.30 if severity in ("LIGHT", "MODERATE") else 0.18
        salvage_estimate = round(acv.final_acv_usd * salvage_pct, 2)
        options = _settlement_options(
            acv.final_acv_usd, deductible, prior_damage, salvage_estimate, state,
        )

    # When A11 and S4B disagree, lower confidence to force adjuster review.
    base_conf = 0.92 if is_tl else 0.85
    if tl_disagreement:
        base_conf = min(base_conf, 0.55)
    if acv_unknown:
        base_conf = 0.0
    confidence = round(min(acv.confidence, base_conf), 2)
    if acv_unknown:
        rationale = (
            f"ACV unknown for state {state} — TL determination deferred to adjuster. "
            f"Repair=${repair:,.0f}, prior=${prior_damage:,.0f}, severity={severity}, "
            f"drivable={drivable}. Settlement options shown as $0 placeholders pending ACV."
        )
    else:
        rationale = (
            f"State {state} TLT={tlt*100:.0f}%; observed (repair ${repair:,.0f} + prior ${prior_damage:,.0f}) "
            f"/ ACV ${acv.final_acv_usd:,.0f} = {pct_observed*100:.1f}% → "
            f"{'TOTAL_LOSS' if is_tl else 'REPAIRABLE'}. "
            f"Severity={severity}, drivable={drivable}, recommended brand={brand}."
        )
        if tl_disagreement:
            rationale += (
                f" DISAGREEMENT: S4B flagged total_loss={s4b_flag} but TLT analysis "
                f"says {'TOTAL_LOSS' if is_tl else 'REPAIRABLE'} — HITL required."
            )

    eval_id = f"TL-{uuid.uuid4().hex.upper()}"
    ev = TotalLossEvaluation(
        evaluation_id=eval_id,
        claim_id=claim.claim_id or "UNKNOWN",
        state=state,
        tlt_pct=tlt,
        repair_estimate_usd=repair,
        prior_damage_usd=prior_damage,
        acv=acv,
        tlt_percentage_observed=pct_observed,
        is_total_loss=is_tl,
        branded_title_recommendation=brand,
        confidence=confidence,
        rationale=rationale,
        settlement_options=options,
        s4b_total_loss_flag=s4b_flag,
        tl_disagreement_with_s4b=tl_disagreement,
        drivable_at_intake=drivable,
        claim_snapshot={
            k: getattr(claim, k) for k in (
                "vin", "vehicle_year", "vehicle_make", "vehicle_model",
                "vehicle_mileage", "location_zip", "loss_location_zip",
                "primary_damage_area", "photo_count",
            ) if getattr(claim, k) is not None
        },
        created_at=_now(),
    )
    return _put(ev)


def assign_salvage(evaluation_id: str, vendor: str = "auto") -> TotalLossEvaluation:
    """Quote the salvage vendor (or run shadow quotes for 'auto') and attach
    the assignment to the evaluation. Recomputes the owner-retention option
    using the vendor's expected net return (more accurate than the heuristic).
    """
    ev = _get(evaluation_id)
    if not ev:
        raise KeyError(f"evaluation {evaluation_id} not found")
    if not ev.is_total_loss:
        raise ValueError(f"evaluation {evaluation_id} is not a total loss; cannot assign salvage")
    if ev.acv.final_acv_usd <= 0:
        raise ValueError(f"evaluation {evaluation_id} has unknown ACV; cannot assign salvage")

    # Use the drivable indicator captured at intake (persisted on the
    # evaluation). Previously this was re-derived from repair/ACV ratio,
    # which mis-classifies cosmetically expensive but driveable cars.
    drivable_now = ev.drivable_at_intake
    severity = _classify_severity(ev.repair_estimate_usd, ev.acv.final_acv_usd, drivable_now)

    # Pull real vehicle/loss details from the persisted claim when available;
    # fall back to neutral placeholders only when the field is genuinely
    # unknown. Stored on `_claim_snapshot` by evaluate() when the orchestrator
    # passes the original claim payload through.
    snap = ev.claim_snapshot or {}
    req = SalvageAssignmentRequest(
        claim_id=ev.claim_id,
        vin=str(snap.get("vin") or "UNKNOWN-VIN"),
        year=int(snap.get("vehicle_year") or 0) or 2020,
        make=str(snap.get("vehicle_make") or "UNK"),
        model=str(snap.get("vehicle_model") or "UNK"),
        mileage=int(snap.get("vehicle_mileage") or 0) or None,
        acv_usd=ev.acv.final_acv_usd,
        damage_severity=severity,
        drivable=drivable_now,
        primary_damage_area=str(snap.get("primary_damage_area") or "FRONT").upper(),
        title_brand=ev.branded_title_recommendation,
        location_zip=str(snap.get("location_zip") or snap.get("loss_location_zip") or "00000"),
        photo_count=int(snap.get("photo_count") or 0),
        prior_damage_disclosed=ev.prior_damage_usd > 0,
        notes=ev.rationale,
    )

    if vendor == "auto":
        quote = best_vendor_for(req)
    else:
        quote = get_salvage_adapter(vendor).assign(req)

    # Recompute owner-retention option with actual vendor quote.
    # Use GROSS salvage value (what the owner keeps in vehicle terms) — the
    # carrier saves the vendor fees by not handling salvage itself, but the
    # insured does not benefit from those fees being absent.
    deductible = float(ev.settlement_options[0].deductible_usd)
    fresh_opts = _settlement_options(
        ev.acv.final_acv_usd, deductible, ev.prior_damage_usd,
        quote.expected_gross_return_usd, ev.state,
    )
    ev.settlement_options = fresh_opts
    ev.salvage_assignment = asdict(quote)
    return _put(ev)


def record_owner_decision(evaluation_id: str, choice: str,
                          actor: str = "unknown") -> TotalLossEvaluation:
    if choice not in ("carrier_retains_salvage", "owner_retains_salvage"):
        raise ValueError(f"invalid choice: {choice}")
    ev = _get(evaluation_id)
    if not ev:
        raise KeyError(f"evaluation {evaluation_id} not found")
    # Append to audit history so a flipped choice leaves a trail. Adjusters
    # changing the customer's choice silently was a regulator-exam exposure.
    ev.owner_decision_history.append({
        "from": ev.owner_decision,
        "to": choice,
        "actor": actor,
        "at": _now(),
    })
    ev.owner_decision = choice
    return _put(ev)


# ───────────────────────────────────────────────────────────────────────────
# Customer notification letter (LLM with deterministic fallback)
# ───────────────────────────────────────────────────────────────────────────

_LETTER_SYSTEM = """You are drafting a total-loss settlement notification letter for an auto insurance customer.

Strict rules:
- Plain English, 8th grade reading level. No insurance jargon (no ACV, no STP, no TLT).
- Warm, empathetic opening. Acknowledge the loss of their vehicle.
- State the two settlement options clearly, each with the dollar amount.
- Explain what happens next and how long they have to respond (10 business days).
- Provide contact info placeholder: [adjuster name], [direct line], [email].
- Never invent facts. Use only the numbers and details provided.
- Output ONLY the letter body. No JSON, no markdown, no preamble.
- Length: 250–400 words."""


def _looks_like_template(s: str) -> bool:
    if not s:
        return True
    s = s.strip()
    if s.startswith("{") or s.startswith("["):
        return True
    return any(x in s.lower() for x in ('"summary"', '"advisories"', "mock coverage analysis"))


def _deterministic_letter(ev: TotalLossEvaluation, choice: Optional[str]) -> str:
    opt_a = ev.settlement_options[0]
    opt_b = ev.settlement_options[1]
    today = dt.date.today().isoformat()
    deadline = _add_business_days(dt.date.today(), 10).isoformat()
    return (
        f"Date: {today}\n\n"
        f"Re: Claim {ev.claim_id} — Vehicle Settlement\n\n"
        f"Dear Insured,\n\n"
        f"We're sorry about the loss of your vehicle. After a careful review of the "
        f"damage and the vehicle's market value, we've determined that the cost to "
        f"repair the vehicle is too high relative to what the vehicle is worth. "
        f"That means we will be settling this claim as a total loss.\n\n"
        f"Here is what we found:\n"
        f"  • The fair-market value of your vehicle is ${ev.acv.final_acv_usd:,.2f}.\n"
        f"  • Repair estimate: ${ev.repair_estimate_usd:,.2f}.\n"
        f"  • Your deductible: ${opt_a.deductible_usd:,.2f}.\n\n"
        f"You have two choices for how to settle this claim:\n\n"
        f"OPTION A — We take the vehicle, you receive ${opt_a.total_owed_to_insured_usd:,.2f}.\n"
        f"   Includes ${opt_a.sales_tax_usd:,.2f} sales tax and ${opt_a.title_fee_usd:,.2f} title fee.\n"
        f"   We pay off any lienholder first, then send the remainder to you.\n\n"
        f"OPTION B — You keep the vehicle, you receive ${opt_b.total_owed_to_insured_usd:,.2f}.\n"
        f"   Includes ${opt_b.sales_tax_usd:,.2f} sales tax and ${opt_b.title_fee_usd:,.2f} title fee.\n"
        f"   You will need to apply for a branded title ({ev.branded_title_recommendation}) with your state DMV.\n"
        f"   The vehicle cannot be sold or driven on public roads until properly titled and inspected.\n\n"
        f"Please let us know your choice by {deadline} (10 business days).\n\n"
        f"If we don't hear from you by that date, we will proceed with Option A.\n\n"
        f"Questions: contact [adjuster name] at [direct line] or [email].\n\n"
        f"We're here to help.\n\n"
        f"Sincerely,\nClaims Department"
    )


def generate_owner_letter(evaluation_id: str, choice: Optional[str] = None) -> str:
    """Produce the customer notification letter for the given evaluation.
    Uses the LLM adapter; falls back to a deterministic template when the LLM
    is in mock mode or returns template-shaped output. Caches the letter on
    the evaluation record."""
    ev = _get(evaluation_id)
    if not ev:
        raise KeyError(f"evaluation {evaluation_id} not found")

    provider = resolve_provider()
    letter: str = ""
    if provider != "mock":
        try:
            user_payload = {
                "claim_id": ev.claim_id,
                "acv_usd": ev.acv.final_acv_usd,
                "deductible_usd": ev.settlement_options[0].deductible_usd,
                "repair_estimate_usd": ev.repair_estimate_usd,
                "state": ev.state,
                "branded_title": ev.branded_title_recommendation,
                "option_a_total_usd": ev.settlement_options[0].total_owed_to_insured_usd,
                "option_b_total_usd": ev.settlement_options[1].total_owed_to_insured_usd,
                "option_a_tax_usd": ev.settlement_options[0].sales_tax_usd,
                "option_b_tax_usd": ev.settlement_options[1].sales_tax_usd,
                "option_a_title_fee_usd": ev.settlement_options[0].title_fee_usd,
                "option_b_title_fee_usd": ev.settlement_options[1].title_fee_usd,
                "owner_choice": choice,
            }
            result = llm_complete(
                system=_LETTER_SYSTEM,
                user=json.dumps(user_payload),
                max_tokens=800,
            )
            letter = (result.text or "") if result and result.ok else ""
        except Exception:
            letter = ""

    if _looks_like_template(letter):
        letter = _deterministic_letter(ev, choice)

    ev.customer_letter_draft = letter
    _put(ev)
    return letter


# ───────────────────────────────────────────────────────────────────────────
# Query helpers
# ───────────────────────────────────────────────────────────────────────────

def get_evaluation(evaluation_id: str) -> Optional[Dict[str, Any]]:
    ev = _get(evaluation_id)
    return asdict(ev) if ev else None


def get_evaluation_by_claim(claim_id: str) -> Optional[Dict[str, Any]]:
    for ev in reversed(list(_STORE.values())):
        if ev.claim_id == claim_id:
            return asdict(ev)
    return None


def list_evaluations(limit: int = 50) -> List[Dict[str, Any]]:
    rows = list(_STORE.values())
    rows.sort(key=lambda e: e.created_at, reverse=True)
    return [asdict(e) for e in rows[:limit]]


def health() -> Dict[str, Any]:
    from fnol_salvage_adapter import health as svh
    return {
        "agent_id": AGENT_ID,
        "agent_name": AGENT_NAME,
        "agent_version": AGENT_VERSION,
        "states_with_tlt": len(STATE_TLT),
        "states_with_tax": len(STATE_TAX),
        "evaluations_in_store": len(_STORE),
        "salvage": svh(),
    }


# ───────────────────────────────────────────────────────────────────────────
# Decision-Record helper for workflow engine integration
# ───────────────────────────────────────────────────────────────────────────

def to_stage_outputs(ev: TotalLossEvaluation) -> Dict[str, Any]:
    """Compact shape suitable for embedding in run_pipeline trace."""
    return {
        "evaluation_id": ev.evaluation_id,
        "state": ev.state,
        "tlt_pct": ev.tlt_pct,
        "tlt_percentage_observed": ev.tlt_percentage_observed,
        "is_total_loss": ev.is_total_loss,
        "s4b_total_loss_flag": ev.s4b_total_loss_flag,
        "tl_disagreement_with_s4b": ev.tl_disagreement_with_s4b,
        "branded_title_recommendation": ev.branded_title_recommendation,
        "acv_final_usd": ev.acv.final_acv_usd,
        "repair_estimate_usd": ev.repair_estimate_usd,
        "settlement_carrier_retains_usd": ev.settlement_options[0].total_owed_to_insured_usd,
        "settlement_owner_retains_usd":   ev.settlement_options[1].total_owed_to_insured_usd,
        "confidence": ev.confidence,
        "rationale": ev.rationale,
        "model_version": ev.model_version,
    }


# ───────────────────────────────────────────────────────────────────────────
# Smoke test
# ───────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    claim = Claim(
        claim_id="CLM-DEMO-A11",
        policy_number="POC-POL-00789",
        loss_date_time="2026-05-10T14:25:00Z",
        loss_location="Houston, TX",
        loss_cause="SINGLE_VEHICLE",
        loss_description="Hit guardrail.",
        reporter_name="Demo Insured",
        reporter_phone="555-0000",
        vehicle_year=2020, vehicle_mileage=58_000,
        vehicle_condition="GOOD", vehicle_options=["leather", "navigation", "sunroof"],
        drivable_indicator=False, deductible_usd=500,
        state="TX", estimated_loss_usd=15_500,
        vehicle_acv_usd=18_500, prior_damage_usd=0,
    )
    s4b = {
        "ai_damage_estimate_point_usd": 15_500,
        "ai_damage_estimate_high_usd": 17_200,
        "total_loss": True, "acv_usd": 18_500, "drivable_indicator": False,
        "loss_cause_inferred": "SINGLE_VEHICLE",
    }
    ev = evaluate(claim, s4b)
    print("=== A11 Evaluation ===")
    print(json.dumps(to_stage_outputs(ev), indent=2))
    print()
    print("=== Settlement options ===")
    for o in ev.settlement_options:
        print(f"  {o.label:30s}  →  ${o.total_owed_to_insured_usd:>10,.2f}")
    print()
    print("=== Assigning salvage (auto) ===")
    ev2 = assign_salvage(ev.evaluation_id, vendor="auto")
    sa = ev2.salvage_assignment
    print(f"  vendor={sa['vendor']}  lot={sa['vendor_lot_id']}  net=${sa['expected_net_return_usd']:,.2f}")
    print(f"  yard={sa['yard_location']}  pickup_eta={sa['pickup_eta_days']}d  sale={sa['expected_sale_date']}")
    print()
    print("Updated owner-retention option:")
    for o in ev2.settlement_options:
        print(f"  {o.label:30s}  →  ${o.total_owed_to_insured_usd:>10,.2f}  (salvage credit ${o.salvage_credit_usd:,.2f})")
    print()
    print("=== Customer letter (deterministic, mock LLM) ===")
    print(generate_owner_letter(ev2.evaluation_id)[:600])
