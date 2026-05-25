"""
A2 — Coverage Verification Agent (Stage 2)
==========================================
Validates coverage in-force against the SOR; emits ROR letter if applicable;
calculates available limits and reserve guidance.

V2 Blueprint Reference: Section 03 (Process Blueprint) · Section 06 (Decisioning Catalog)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fnol_workflow_engine import (
    BaseAgent, DecisionRecord, FNOLPayload, LossType,
    MaturityLevel, ENGINE_VERSION, now_iso, log
)


@dataclass
class CoverageDecision:
    inForce: bool
    decision: str                       # COVERED | DENIED | ROR_PENDING
    rationale: str
    coverageMap: Dict[str, Any]
    availableLimits: Dict[str, float]
    deductibles: Dict[str, float]
    rorRequired: bool
    rorReason: Optional[str]
    coverageComplexity: float           # 0.0-1.0 input to triage
    decisionRecord: DecisionRecord
    exposures: List[Dict[str, Any]] = field(default_factory=list)


class CoverageVerificationAgent(BaseAgent):
    """
    Coverage decisioning logic:
      1. Pull policy from SOR.
      2. Verify policy in-force at lossDateTime.
      3. Map lossType to coverage clauses.
      4. Issue ROR letter if facts ambiguous (state-specific window).
      5. Compute coverage complexity for triage.
    """
    name = "CoverageVerificationAgent"

    LOSS_TO_COVERAGE = {
        LossType.COLLISION.value:    ["COLL", "BI", "PD"],
        LossType.COMPREHENSIVE.value:["COMP"],
        LossType.LIABILITY.value:    ["BI", "PD"],
        LossType.UNINSURED.value:    ["UM"],
        LossType.PIP.value:          ["MED", "PIP"],
        LossType.UNKNOWN.value:      ["BI", "PD", "COLL"],
    }

    def process(self, payload: FNOLPayload) -> CoverageDecision:
        t0 = time.time()
        policy = self.sor.fetch_policy(payload.policyNumber)

        # --- policy not found ---
        if not policy:
            dr = self.emit(
                claim=payload.claimNumber, dtype="COVERAGE_VERIFY",
                value={"decision":"DENIED","reason":"policy_not_found"},
                conf=0.99, inputs={"policy": payload.policyNumber},
                hitl=True, explanation="Policy not found in SOR — escalate",
            )
            return CoverageDecision(
                inForce=False, decision="DENIED",
                rationale=f"Policy {payload.policyNumber} not found in SOR",
                coverageMap={}, availableLimits={}, deductibles={},
                rorRequired=False, rorReason=None, coverageComplexity=1.0,
                decisionRecord=dr,
            )

        # --- in-force check ---
        in_force = self._in_force(policy, payload.lossDateTime)
        if not in_force:
            dr = self.emit(
                claim=payload.claimNumber, dtype="COVERAGE_VERIFY",
                value={"decision":"DENIED","reason":"policy_not_in_force",
                       "expiry":policy.get("expiry"), "status":policy.get("status")},
                conf=0.97, inputs={"policy": payload.policyNumber, "loss": payload.lossDateTime},
                hitl=True, explanation="Policy expired or cancelled at loss datetime",
            )
            return CoverageDecision(
                inForce=False, decision="DENIED",
                rationale=f"Policy not in-force (status={policy.get('status')}, expiry={policy.get('expiry')})",
                coverageMap=policy.get("coverages", {}), availableLimits={}, deductibles={},
                rorRequired=False, rorReason=None, coverageComplexity=1.0,
                decisionRecord=dr,
            )

        # --- coverage clause mapping ---
        coverages = policy.get("coverages", {})
        applicable = self.LOSS_TO_COVERAGE.get(payload.lossType, ["BI","PD","COLL"])
        coverage_map = {k: coverages.get(k) for k in applicable if k in coverages}
        missing = [k for k in applicable if k not in coverages]

        # available limits & deductibles
        available_limits: Dict[str, float] = {}
        deductibles: Dict[str, float] = {}
        for k, v in coverage_map.items():
            if not v:
                continue
            if "limit" in v: available_limits[k] = float(v["limit"])
            if "per_person" in v: available_limits[f"{k}_per_person"] = float(v["per_person"])
            if "per_accident" in v: available_limits[f"{k}_per_accident"] = float(v["per_accident"])
            if "deductible" in v: deductibles[k] = float(v["deductible"])

        # --- ROR rationale (Reservation of Rights letter) ---
        ror_required = False
        ror_reason: Optional[str] = None
        if payload.lossType == LossType.UNKNOWN.value:
            ror_required, ror_reason = True, "loss type undetermined at FNOL"
        elif missing:
            ror_required, ror_reason = True, f"requested coverages not on policy: {','.join(missing)}"
        elif any(p.attorneyRetained for p in payload.parties):
            ror_required, ror_reason = True, "attorney retained — preserve rights pending investigation"

        decision = "ROR_PENDING" if ror_required else "COVERED"
        coverage_complexity = self._complexity(coverage_map, missing, payload)

        # --- exposures (V2 Blueprint S2 — feeds Settlement) ---
        exposures: List[Dict[str, Any]] = []
        for party in payload.parties:
            for cov_code in applicable:
                if cov_code not in coverages:
                    continue
                cov = coverages[cov_code]
                exposure = {
                    "partyName": f"{party.firstName} {party.lastName}",
                    "partyRole": party.role,
                    "coverageCode": cov_code,
                    "applicableLimit": cov.get("limit", cov.get("per_person", 0)),
                    "deductible": cov.get("deductible", 0),
                }
                exposures.append(exposure)

        dr = self.emit(
            claim=payload.claimNumber, dtype="COVERAGE_VERIFY",
            value={"decision":decision, "applicable":applicable, "missing":missing,
                   "rorRequired":ror_required, "complexity":coverage_complexity},
            conf=0.93,
            inputs={"policy": payload.policyNumber, "lossType": payload.lossType,
                    "claimantCount": len(payload.parties)},
            hitl=ror_required, explanation=f"Coverage verified in {(time.time()-t0)*1000:.0f}ms",
            model_version="coverage-rules-v1.3",
        )

        return CoverageDecision(
            inForce=True, decision=decision,
            rationale="Policy in-force; coverage clauses mapped" if not ror_required else f"ROR: {ror_reason}",
            coverageMap=coverage_map, availableLimits=available_limits,
            deductibles=deductibles, rorRequired=ror_required, rorReason=ror_reason,
            coverageComplexity=coverage_complexity, decisionRecord=dr, exposures=exposures,
        )

    @staticmethod
    def _in_force(policy: Dict[str, Any], loss_dt: Optional[str]) -> bool:
        status = policy.get("status", "ACTIVE")
        if status != "ACTIVE":
            return False
        # expiry parse
        expiry_str = policy.get("expiry")
        if expiry_str:
            try:
                expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
            except Exception:
                return True   # fail-safe: trust SOR status field
            loss_dt_parsed = None
            if loss_dt:
                try:
                    loss_dt_parsed = datetime.fromisoformat(loss_dt.replace("Z", "+00:00"))
                    if loss_dt_parsed.tzinfo is None:
                        loss_dt_parsed = loss_dt_parsed.replace(tzinfo=timezone.utc)
                except Exception:
                    loss_dt_parsed = None
            comparator = loss_dt_parsed or datetime.now(timezone.utc)
            return comparator <= expiry
        return True

    @staticmethod
    def _complexity(coverage_map: Dict[str, Any], missing: List[str], payload: FNOLPayload) -> float:
        score = 0.2
        if missing: score += 0.3
        if len(coverage_map) >= 4: score += 0.1
        if len(payload.parties) >= 3: score += 0.2
        if any(p.role == "thirdParty" for p in payload.parties): score += 0.1
        if any(p.attorneyRetained for p in payload.parties): score += 0.2
        return round(min(1.0, score), 2)


__all__ = ["CoverageVerificationAgent","CoverageDecision"]
