"""
fnol_agents_ext.py — Extended Agents (A5, A6, A7, A8)
======================================================
Stage-4B Damage Estimation, Stage-5 BI Evaluation, Stage-6 Settlement, Stage-7 Subrogation.
V2 Blueprint Reference: Section 03 (Process Blueprint) · Section 06 (Decisioning Catalog)

These agents complete the production 8-agent pipeline. L3 hooks (Conversational
Orchestration + Co-Pilot) live in fnol_l3_agents.py.
"""

from __future__ import annotations

import math
import re
import time
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fnol_workflow_engine import (
    BaseAgent, DecisionRecord, FNOLPayload, MaturityLevel, Vehicle,
    ENGINE_VERSION, now_iso, log,
)


# ════════════════════════════════════════════════════════════════════════════════
# A5 — DAMAGE ESTIMATION AGENT (Stage 4B — parallel with Fraud)
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class DamageArea:
    panel: str
    severity: str             # MINOR | MODERATE | SEVERE | TOTAL
    confidence: float
    estimatedCostUSD: float
    operationFlag: str        # REPAIR | REPLACE | PAINT_ONLY | UNREPAIRABLE


@dataclass
class DamageResult:
    decision: str             # REPAIR | TOTAL_LOSS | PHOTO_REJECTED | DISPATCH_DRP
    aiEstimateUSD: float
    acvUSD: float
    repairToACVRatio: float
    totalLossThresholdState: float
    drpShopAssigned: Optional[str]
    salvageValueUSD: float
    damageAreas: List[DamageArea]
    photosAccepted: int
    photosRejected: int
    cccHandshakeId: Optional[str]
    decisionRecord: DecisionRecord
    hitlRequired: bool


class DamageEstimationAgent(BaseAgent):
    """
    A5 — Damage Estimation (Stage 4B).
    Inputs:  photos, vehicle metadata, telematics severity, narrative
    Outputs: AI repair estimate, ACV, total-loss decision, DRP routing
    Models:  Tractable / CCC One / Mitchell (mocked here; pluggable in prod)
    """
    name = "DamageEstimationAgent"

    # State-specific total-loss thresholds (MV-907 / state DOI guidance)
    TOTAL_LOSS_THRESHOLD: Dict[str, float] = {
        "TX": 1.00,    "GA": 0.75,     "CA": 0.80,     "FL": 0.80,     "NY": 0.75,
        "IL": 0.75,    "PA": 1.00,     "OH": 0.75,     "AZ": 0.70,     "DEFAULT": 0.80,
    }

    # ACV approximation table (placeholder — production uses Black Book / NADA / KBB API)
    ACV_TABLE: Dict[str, Dict[int, float]] = {
        "Honda Accord": {2022: 25_000, 2021: 22_000, 2020: 20_000},
        "Honda Civic":  {2022: 22_000, 2021: 19_500, 2020: 17_500},
        "Toyota Camry": {2024: 30_000, 2023: 27_000, 2022: 25_000},
        "Tesla Model 3":{2024: 42_000, 2023: 38_000, 2022: 35_000},
        "BMW 5 Series": {2018: 22_000, 2017: 20_000, 2016: 18_000},
    }

    def process(self, payload: FNOLPayload, photos: Optional[List[str]] = None,
                presented_estimate: Optional[float] = None) -> DamageResult:
        t0 = time.time()
        photos = photos or payload.photos or []
        vehicle = payload.vehicles[0] if payload.vehicles else Vehicle()
        text = (payload.lossDescription or "").lower()
        tel = payload.telematics

        # --- photo intake validation ---
        accepted, rejected = self._validate_photos(photos)
        if photos and accepted == 0:
            dr = self.emit(
                claim=payload.claimNumber, dtype="DAMAGE_PHOTO_REVIEW",
                value={"decision":"PHOTO_REJECTED","accepted":0,"rejected":rejected},
                conf=0.92, inputs={"photoCount": len(photos)},
                hitl=True, explanation="All photos rejected — request re-upload",
            )
            return DamageResult(
                decision="PHOTO_REJECTED", aiEstimateUSD=0.0, acvUSD=0.0,
                repairToACVRatio=0.0, totalLossThresholdState=0.0,
                drpShopAssigned=None, salvageValueUSD=0.0, damageAreas=[],
                photosAccepted=0, photosRejected=rejected, cccHandshakeId=None,
                decisionRecord=dr, hitlRequired=True,
            )

        # --- damage area detection (CV proxy from narrative + telematics) ---
        damage_areas = self._detect_damage_areas(text, tel, presented_estimate)

        # --- AI estimate ---
        ai_estimate = sum(d.estimatedCostUSD for d in damage_areas)
        if presented_estimate and presented_estimate > 0:
            ai_estimate = (ai_estimate * 0.4 + presented_estimate * 0.6)
        ai_estimate = round(ai_estimate, 0)

        # --- ACV ---
        acv = self._lookup_acv(vehicle)

        # --- total-loss decision ---
        threshold = self.TOTAL_LOSS_THRESHOLD.get(payload.state, self.TOTAL_LOSS_THRESHOLD["DEFAULT"])
        ratio = round(ai_estimate / acv, 3) if acv > 0 else 0.0
        is_total_loss = (ratio >= threshold) or any(d.operationFlag == "UNREPAIRABLE" for d in damage_areas)

        if is_total_loss:
            decision = "TOTAL_LOSS"
            salvage = round(acv * 0.18, -1)
            drp = None
            ccc_id = None
        else:
            decision = "DISPATCH_DRP"
            salvage = 0.0
            drp = self._select_drp_shop(payload.state)
            ccc_id = f"CCC-{payload.claimNumber[-6:]}-{int(time.time()) % 100000}"

        hitl = is_total_loss or (presented_estimate and abs(presented_estimate - ai_estimate) / max(1, ai_estimate) > 0.25)

        dr = self.emit(
            claim=payload.claimNumber, dtype="DAMAGE_ESTIMATE",
            value={"decision":decision, "aiEstimate":ai_estimate, "acv":acv,
                   "ratio":ratio, "threshold":threshold, "areas":len(damage_areas)},
            conf=0.88,
            inputs={"vin":vehicle.vin, "year":vehicle.year, "make":vehicle.make, "model":vehicle.model,
                    "photos":len(photos), "tel_dV":tel.deltaV_mph if tel else 0.0},
            hitl=bool(hitl), explanation=f"Damage assessed in {(time.time()-t0)*1000:.0f}ms",
            model_version="tractable-v3.2-mock",
        )

        return DamageResult(
            decision=decision, aiEstimateUSD=ai_estimate, acvUSD=acv,
            repairToACVRatio=ratio, totalLossThresholdState=threshold,
            drpShopAssigned=drp, salvageValueUSD=salvage,
            damageAreas=damage_areas, photosAccepted=accepted, photosRejected=rejected,
            cccHandshakeId=ccc_id, decisionRecord=dr, hitlRequired=bool(hitl),
        )

    @staticmethod
    def _validate_photos(photos: List[str]) -> tuple:
        if not photos:
            return 0, 0
        accepted = sum(1 for p in photos if (p or "").strip())
        rejected = len(photos) - accepted
        return accepted, rejected

    @staticmethod
    def _detect_damage_areas(text: str, tel, presented_estimate: Optional[float]) -> List[DamageArea]:
        areas: List[DamageArea] = []
        keyword_map = {
            "front":      ("Front Bumper", "MODERATE", 2_800, "REPAIR"),
            "rear":       ("Rear Bumper",  "MODERATE", 2_500, "REPAIR"),
            "side":       ("Side Panel",   "MODERATE", 3_200, "REPAIR"),
            "driver":     ("Driver Door",  "MODERATE", 2_200, "REPAIR"),
            "passenger":  ("Passenger Door","MODERATE", 2_200, "REPAIR"),
            "hood":       ("Hood",         "MODERATE", 1_900, "REPAIR"),
            "roof":       ("Roof",         "SEVERE",   4_500, "REPLACE"),
            "windshield": ("Windshield",   "SEVERE",   1_400, "REPLACE"),
            "rollover":   ("Frame",        "TOTAL",   18_000, "UNREPAIRABLE"),
            "totaled":    ("Frame",        "TOTAL",   25_000, "UNREPAIRABLE"),
            "t-bone":     ("Side Panel",   "SEVERE",   8_500, "REPLACE"),
            "t boned":    ("Side Panel",   "SEVERE",   8_500, "REPLACE"),
            "rear-end":   ("Rear Bumper",  "SEVERE",   5_500, "REPLACE"),
            "rear ended": ("Rear Bumper",  "SEVERE",   5_500, "REPLACE"),
            "hail":       ("Roof",         "MODERATE", 3_800, "PAINT_ONLY"),
            "fire":       ("Frame",        "TOTAL",   30_000, "UNREPAIRABLE"),
            "flood":      ("Frame",        "TOTAL",   22_000, "UNREPAIRABLE"),
            "scratch":    ("Side Panel",   "MINOR",      450, "PAINT_ONLY"),
        }
        for kw, (panel, sev, cost, op) in keyword_map.items():
            if kw in text:
                areas.append(DamageArea(panel=panel, severity=sev, confidence=0.78,
                                        estimatedCostUSD=float(cost), operationFlag=op))
        if not areas and tel and tel.deltaV_mph >= 15:
            areas.append(DamageArea(panel="Front Bumper", severity="MODERATE",
                                    confidence=0.65, estimatedCostUSD=3_500.0, operationFlag="REPAIR"))
        if not areas:
            areas.append(DamageArea(panel="Unspecified", severity="MINOR",
                                    confidence=0.55, estimatedCostUSD=1_200.0, operationFlag="REPAIR"))
        # de-duplicate by panel
        seen = set(); unique = []
        for a in areas:
            if a.panel in seen: continue
            seen.add(a.panel); unique.append(a)
        return unique

    @classmethod
    def _lookup_acv(cls, v: Vehicle) -> float:
        if not v or not v.make or not v.model:
            return 18_000.0
        key = f"{v.make} {v.model}"
        table = cls.ACV_TABLE.get(key)
        if table:
            for yr in [v.year, v.year-1, v.year+1]:
                if yr in table: return float(table[yr])
        # heuristic fallback
        base = 22_000.0
        if v.year >= 2023: base = 30_000
        if v.year <= 2018: base = 14_000
        if v.year <= 2015: base = 9_000
        return float(base)

    @staticmethod
    def _select_drp_shop(state: str) -> str:
        shops = {
            "TX": "Caliber Collision · Houston Galleria",
            "GA": "Service King · Atlanta Buckhead",
            "CA": "Gerber Collision · LA Hollywood",
            "FL": "Caliber Collision · Miami Doral",
            "NY": "Maaco · Brooklyn Industrial",
        }
        return shops.get(state, f"Caliber Collision · {state} Regional Hub")


# ════════════════════════════════════════════════════════════════════════════════
# A6 — BI EVALUATION AGENT (Stage 5)
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class InjuryAssessment:
    party: str
    severity: str
    treatmentSummary: str
    medicalSpendUSD: float
    permanentImpairmentPct: float
    estimatedFinalDemandUSD: float


@dataclass
class BIEvaluationResult:
    totalInjuredCount: int
    medicalSpendTotalUSD: float
    settlementP10: float
    settlementP50: float           # mid-point offer recommendation
    settlementP90: float
    faultPct: float                 # claimant fault percentage
    reserveAdequacyUSD: float
    attorneyRepresented: bool
    injuryAssessments: List[InjuryAssessment]
    decisionRecord: DecisionRecord
    hitlRequired: bool


class BIEvaluationAgent(BaseAgent):
    """
    A6 — Bodily Injury Evaluation (Stage 5).
    V2 Blueprint: 200K context Claude Opus reads medical records + claim narrative.
    Stub here uses heuristics; production wires Claude Opus + RAG over medical bills.
    """
    name = "BIEvaluationAgent"

    SEVERITY_BASE_DEMAND = {
        "MINOR":    8_500.0,
        "MODERATE": 35_000.0,
        "SEVERE":  185_000.0,
        "FATAL":   850_000.0,
    }

    def process(self, payload: FNOLPayload, fault_pct: float = 0.0,
                attorney_retained: Optional[bool] = None) -> BIEvaluationResult:
        t0 = time.time()
        if attorney_retained is None:
            attorney_retained = any(p.attorneyRetained for p in payload.parties)

        assessments: List[InjuryAssessment] = []
        med_total = 0.0
        for inj in payload.injuriesReported:
            base = self.SEVERITY_BASE_DEMAND.get(inj.severity, 5_000.0)
            med_spend = base * 0.35
            permanent_pct = self._permanent_impairment(inj.severity)
            attorney_multiplier = 1.7 if attorney_retained else 1.0
            demand = base * attorney_multiplier
            assessments.append(InjuryAssessment(
                party=inj.party or "claimant", severity=inj.severity,
                treatmentSummary=f"Auto-assessed from FNOL narrative — severity={inj.severity}",
                medicalSpendUSD=round(med_spend, 0),
                permanentImpairmentPct=permanent_pct,
                estimatedFinalDemandUSD=round(demand, 0),
            ))
            med_total += med_spend

        if not assessments:
            dr = self.emit(
                claim=payload.claimNumber, dtype="BI_EVALUATE",
                value={"injuries":0,"reserve":0}, conf=0.95,
                inputs={"claim":payload.claimNumber}, hitl=False,
                explanation="No BI exposure — skipped",
            )
            return BIEvaluationResult(
                totalInjuredCount=0, medicalSpendTotalUSD=0.0,
                settlementP10=0.0, settlementP50=0.0, settlementP90=0.0,
                faultPct=fault_pct, reserveAdequacyUSD=0.0,
                attorneyRepresented=attorney_retained, injuryAssessments=[],
                decisionRecord=dr, hitlRequired=False,
            )

        total_demand = sum(a.estimatedFinalDemandUSD for a in assessments)
        # comparative-fault adjustment (CA 50%, GA 50%, TX 51% modified comp negl, etc.)
        net_after_fault = total_demand * (1.0 - fault_pct)

        # P10/P50/P90 settlement spread
        p50 = round(net_after_fault * 0.65, -2)         # mid (negotiated)
        p10 = round(net_after_fault * 0.45, -2)
        p90 = round(net_after_fault * 0.95, -2)
        reserve = round(net_after_fault * 1.05, -2)     # reserve set at conservative upper

        hitl = (any(a.severity in ("SEVERE","FATAL") for a in assessments) or
                attorney_retained or total_demand > 75_000)

        dr = self.emit(
            claim=payload.claimNumber, dtype="BI_EVALUATE",
            value={"injured":len(assessments),"p50":p50,"reserve":reserve,
                   "attorney":attorney_retained,"medSpend":round(med_total,0)},
            conf=0.83,
            inputs={"injuryCount":len(assessments),"fault":fault_pct},
            hitl=hitl, explanation=f"BI evaluated in {(time.time()-t0)*1000:.0f}ms · "
                                   f"{'Claude Opus simulated' if hitl else 'heuristic only'}",
            model_version="claude-opus-bi-v1-mock",
        )

        return BIEvaluationResult(
            totalInjuredCount=len(assessments), medicalSpendTotalUSD=round(med_total, 0),
            settlementP10=p10, settlementP50=p50, settlementP90=p90,
            faultPct=fault_pct, reserveAdequacyUSD=reserve,
            attorneyRepresented=attorney_retained, injuryAssessments=assessments,
            decisionRecord=dr, hitlRequired=hitl,
        )

    @staticmethod
    def _permanent_impairment(severity: str) -> float:
        mapping = {"MINOR":0.0, "MODERATE":2.5, "SEVERE":18.0, "FATAL":100.0}
        return mapping.get(severity, 0.0)


# ════════════════════════════════════════════════════════════════════════════════
# A7 — SETTLEMENT AGENT (Stage 6)
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class SettlementResult:
    status: str               # AUTO_APPROVED | PENDING_ADJUSTER | BLOCKED_FRAUD | BLOCKED_COVERAGE
    grossPayableUSD: float
    deductibleUSD: float
    netPayableUSD: float
    paymentMethod: str        # ACH | ZELLE | CHECK
    payee: str
    eftStatus: str
    decisionRecord: DecisionRecord
    hitlRequired: bool
    blockedReason: Optional[str] = None


class SettlementAgent(BaseAgent):
    """
    A7 — Settlement (Stage 6).
    Inputs:  damage estimate, BI evaluation, fraud band, coverage decision
    Outputs: net payable, payment method, EFT release status
    """
    name = "SettlementAgent"

    STP_MAX_USD = 15_000.0     # auto-approval ceiling

    def process(self, payload: FNOLPayload, *,
                damage_estimate: float = 0.0,
                bi_p50: float = 0.0,
                deductible: float = 0.0,
                fraud_band: str = "LOW",
                coverage_decision: str = "COVERED",
                payment_hold: bool = False) -> SettlementResult:
        t0 = time.time()
        gross = float(damage_estimate) + float(bi_p50)
        net = max(0.0, gross - float(deductible))

        blocked_reason: Optional[str] = None
        if coverage_decision == "DENIED":
            status = "BLOCKED_COVERAGE"
            blocked_reason = "coverage denied"
            net = 0.0
        elif payment_hold or fraud_band in ("HIGH","CRITICAL"):
            status = "BLOCKED_FRAUD"
            blocked_reason = f"fraud {fraud_band} payment hold"
            net = 0.0
        elif net <= self.STP_MAX_USD and fraud_band == "LOW" and coverage_decision == "COVERED" and bi_p50 == 0.0:
            status = "AUTO_APPROVED"
        else:
            status = "PENDING_ADJUSTER"

        payee = (payload.parties[0].firstName + " " + payload.parties[0].lastName) if payload.parties else "Insured"
        method = "ACH" if status == "AUTO_APPROVED" else "CHECK"
        eft = "RELEASED" if status == "AUTO_APPROVED" else "PENDING"

        hitl = status in ("PENDING_ADJUSTER","BLOCKED_FRAUD")
        dr = self.emit(
            claim=payload.claimNumber, dtype="SETTLE",
            value={"status":status,"net":net,"method":method,"eft":eft},
            conf=0.91 if status == "AUTO_APPROVED" else 0.85,
            inputs={"gross":gross,"deductible":deductible,"fraud":fraud_band,"coverage":coverage_decision},
            hitl=hitl, explanation=f"Settlement decisioned in {(time.time()-t0)*1000:.0f}ms — {status}",
            model_version="settle-rules-v1.4",
        )

        return SettlementResult(
            status=status, grossPayableUSD=round(gross,2), deductibleUSD=round(deductible,2),
            netPayableUSD=round(net,2), paymentMethod=method,
            payee=payee.strip() or "Insured", eftStatus=eft,
            decisionRecord=dr, hitlRequired=hitl, blockedReason=blocked_reason,
        )


# ════════════════════════════════════════════════════════════════════════════════
# A8 — SUBROGATION AGENT (Stage 7)
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class SubrogationResult:
    subrogationScore: float       # 0.0–1.0 — likelihood of recovery from third party
    pursueRecovery: bool
    targetCarrier: Optional[str]
    estimatedRecoveryUSD: float
    statuteOfLimitationsDays: int
    rationale: str
    decisionRecord: DecisionRecord


class SubrogationAgent(BaseAgent):
    """
    A8 — Subrogation Identification at FNOL (Stage 7).
    V2 Blueprint target: ≥80% subrogation identification at FNOL (vs. <50% today).
    """
    name = "SubrogationAgent"

    def process(self, payload: FNOLPayload, fault_pct: float = 1.0,
                paid_amount: float = 0.0) -> SubrogationResult:
        t0 = time.time()
        # if claimant >50% at fault, subrogation unlikely
        third_party_fault = max(0.0, 1.0 - fault_pct)
        text = (payload.lossDescription or "").lower()

        score = third_party_fault * 0.6
        rationale_parts = [f"third-party fault est. {third_party_fault*100:.0f}%"]

        # narrative signals
        if any(k in text for k in ["rear-ended me","rear ended me","ran red light","ran a red","drunk","ran the stop","fled the scene","hit and run","hit me"]):
            score = min(1.0, score + 0.30)
            rationale_parts.append("clear third-party fault narrative")
        if any(p.role == "thirdParty" and p.insuranceCarrier for p in payload.parties):
            score = min(1.0, score + 0.10)
            rationale_parts.append("third-party carrier identified")
        if payload.policeReported:
            score = min(1.0, score + 0.05)
            rationale_parts.append("police report supports recovery")

        target_carrier = None
        for p in payload.parties:
            if p.role == "thirdParty" and p.insuranceCarrier:
                target_carrier = p.insuranceCarrier
                break

        pursue = score >= 0.50 and paid_amount >= 1_000.0
        # Most US states: 2–3 years statute for tort
        sol = 730 if payload.state in ("TX","CA","FL","GA","NY") else 1095

        recovery = round(paid_amount * score * 0.85, 0) if pursue else 0.0

        dr = self.emit(
            claim=payload.claimNumber, dtype="SUBROGATION_ASSESS",
            value={"score":round(score,3),"pursue":pursue,"target":target_carrier,
                   "estRecovery":recovery},
            conf=0.86, inputs={"fault":fault_pct,"paid":paid_amount,"state":payload.state},
            hitl=False, explanation=f"Subrogation assessed in {(time.time()-t0)*1000:.0f}ms",
            model_version="subro-v1.2",
        )

        return SubrogationResult(
            subrogationScore=round(score, 3), pursueRecovery=pursue,
            targetCarrier=target_carrier, estimatedRecoveryUSD=recovery,
            statuteOfLimitationsDays=sol, rationale=" · ".join(rationale_parts),
            decisionRecord=dr,
        )


__all__ = [
    "DamageEstimationAgent","DamageResult","DamageArea",
    "BIEvaluationAgent","BIEvaluationResult","InjuryAssessment",
    "SettlementAgent","SettlementResult",
    "SubrogationAgent","SubrogationResult",
]
