"""
FNOL Intelligence Platform — Governance API Routes
===================================================
HTTP surface for the governance layer (decision log, bias monitor, model
cards, state addenda, FCRA adverse-action notices).

Endpoints under /api/v1/fnol/governance/:
  GET    /health                              — governance composite health
  GET    /decisions                           — list recent decision entries
  GET    /decisions/{claim_id}                — SHA-256-validated chain for a claim
  POST   /decisions                           — append a new decision entry
  GET    /bias                                — bias-monitor proxy report
  POST   /bias                                — record a bias proxy entry
  GET    /bias/evaluation                     — statistical bias evaluation
  GET    /model-cards                         — list all model cards
  GET    /model-cards/{agent_id}              — single model card
  GET    /state-addenda                       — list all state addenda
  GET    /state-addenda/{state}               — single state addendum
  POST   /adverse-action                      — generate an FCRA §615 notice

Wire into fnol_api_server.py:
    import fnol_governance_routes
    app.include_router(fnol_governance_routes.router)
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, confloat

import fnol_governance_agent as gov
from fnol_api_deps import require_api_key, client_error, server_error


log = logging.getLogger("fnol.governance.routes")
router = APIRouter(prefix="/api/v1/fnol/governance", tags=["Governance"])


# ── Schemas ─────────────────────────────────────────────────────────────

class DecisionRequest(BaseModel):
    claim_id: str = Field(..., min_length=1, max_length=128)
    stage_id: str = Field(..., min_length=1, max_length=16)
    rule_id: str = Field(..., min_length=1, max_length=64)
    decision: str = Field(..., min_length=1, max_length=128)
    confidence: confloat(ge=0.0, le=1.0) = 0.0
    rationale: str = Field(..., min_length=1, max_length=2000)
    hitl_required: bool = False
    model_version: Optional[str] = Field(default=None, max_length=64)
    input_hash: Optional[str] = Field(default=None, max_length=128)


class BiasProxyRequest(BaseModel):
    claim_id: str = Field(..., min_length=1, max_length=128)
    gender_code: Optional[str] = Field(default=None, max_length=4)
    preferred_language: Optional[str] = Field(default=None, max_length=8)
    age_decade: Optional[str] = Field(default=None, max_length=8)
    garaging_zip_prefix: Optional[str] = Field(default=None, max_length=4)
    stp_authorized: Optional[bool] = None
    fraud_score: Optional[confloat(ge=0.0, le=1.0)] = None


class AdverseActionRequest(BaseModel):
    claim_id: str = Field(..., min_length=1, max_length=128)
    template_key: str = Field(..., min_length=1, max_length=64)   # COVERAGE_DENIAL | STP_DENIAL | FRAUD_HOLD
    basis: str = Field(..., min_length=1, max_length=1000)
    state: str = Field(..., min_length=2, max_length=2)


# ── Routes ──────────────────────────────────────────────────────────────

@router.get("/health")
def governance_health(_: str = Depends(require_api_key)):
    return gov.governance_health()


@router.get("/decisions")
def list_decisions(limit: int = 100, _: str = Depends(require_api_key)):
    limit = max(1, min(limit, 1000))
    return {"decisions": gov.get_all_decisions(limit=limit)}


@router.get("/decisions/{claim_id}")
def get_chain(claim_id: str, _: str = Depends(require_api_key)):
    result = gov.get_chain(claim_id, validate=True)
    if not result or result.get("count", 0) == 0:
        # Some agents return an empty result instead of None on miss — surface
        # 200 with an empty chain rather than 404 so callers can detect "no
        # decisions yet" vs "claim not in system".
        return result or {"claim_id": claim_id, "count": 0, "entries": [], "chain_valid": True}
    return result


@router.post("/decisions")
def log_decision(req: DecisionRequest, _: str = Depends(require_api_key)):
    try:
        entry = gov.log_decision(
            claim_id=req.claim_id,
            stage_id=req.stage_id,
            rule_id=req.rule_id,
            decision=req.decision,
            confidence=float(req.confidence),
            rationale=req.rationale,
            hitl_required=req.hitl_required,
            model_version=req.model_version or "unknown",
            input_hash=req.input_hash or "",
        )
    except Exception as e:
        raise server_error("governance.log_decision failed", e)
    from dataclasses import asdict, is_dataclass
    return asdict(entry) if is_dataclass(entry) else entry


@router.get("/bias")
def get_bias(_: str = Depends(require_api_key)):
    return gov.get_bias_report()


@router.post("/bias")
def record_bias(req: BiasProxyRequest, _: str = Depends(require_api_key)):
    try:
        # record_bias_proxy takes keyword args (not a dict). The smoke test
        # field `age_decade` maps to the agent's `dob_decade` parameter.
        gov.record_bias_proxy(
            claim_id=req.claim_id,
            dob_decade=req.age_decade,
            gender_code=req.gender_code,
            preferred_language=req.preferred_language,
            garaging_zip_prefix=req.garaging_zip_prefix,
            stp_authorized=req.stp_authorized,
            fraud_score=req.fraud_score,
        )
    except Exception as e:
        raise server_error("governance.record_bias_proxy failed", e)
    return {"claim_id": req.claim_id, "recorded": True}


@router.get("/bias/evaluation")
def bias_evaluation(_: str = Depends(require_api_key)):
    # The CO Reg 10-1-1 §VII evaluation is exposed as `complete_bias_evaluation`.
    fn = (getattr(gov, "complete_bias_evaluation", None)
          or getattr(gov, "run_bias_evaluation", None)
          or getattr(gov, "evaluate_bias", None))
    if fn is None:
        raise client_error("Bias evaluation function not available in governance agent", 501)
    try:
        return fn()
    except Exception as e:
        raise server_error("governance.complete_bias_evaluation failed", e)


@router.get("/model-cards")
def list_model_cards(_: str = Depends(require_api_key)):
    cards = gov.list_model_cards()
    return {"model_cards": cards, "total": len(cards)}


@router.get("/model-cards/{agent_id}")
def get_model_card(agent_id: str, _: str = Depends(require_api_key)):
    card = gov.get_model_card(agent_id)
    if not card:
        raise client_error(f"Model card '{agent_id}' not found", 404)
    return card


@router.get("/state-addenda")
def list_state_addenda(_: str = Depends(require_api_key)):
    items = gov.list_state_addenda()
    return {"state_addenda": items, "total": len(items)}


@router.get("/state-addenda/{state}")
def get_state_addendum(state: str, _: str = Depends(require_api_key)):
    addendum = gov.get_state_addendum(state.upper())
    if not addendum:
        raise client_error(f"No state addendum for '{state}'", 404)
    return addendum


@router.post("/adverse-action")
def generate_adverse_action(req: AdverseActionRequest, _: str = Depends(require_api_key)):
    # Allow-list template_key to prevent format-string injection through
    # gov.generate_adverse_action_notice's downstream .format() call.
    allowed = {"COVERAGE_DENIAL", "STP_DENIAL", "FRAUD_HOLD"}
    if req.template_key.upper() not in allowed:
        raise client_error(
            f"Unknown template_key '{req.template_key}'. Allowed: {sorted(allowed)}", 400)
    try:
        notice = gov.generate_adverse_action_notice(
            claim_id=req.claim_id,
            basis=req.basis,
            state=req.state.upper(),
            template_key=req.template_key.upper(),
        )
    except TypeError:
        # Some implementations don't accept template_key kwarg yet.
        try:
            notice = gov.generate_adverse_action_notice(
                claim_id=req.claim_id, basis=req.basis, state=req.state.upper())
        except Exception as e:
            raise server_error("governance.generate_adverse_action_notice failed", e)
    except Exception as e:
        raise server_error("governance.generate_adverse_action_notice failed", e)
    if isinstance(notice, str):
        return {"claim_id": req.claim_id, "template_key": req.template_key.upper(), "notice": notice}
    return notice
