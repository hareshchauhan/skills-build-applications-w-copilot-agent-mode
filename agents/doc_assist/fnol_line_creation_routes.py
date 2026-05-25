"""
FNOL Intelligence Platform — S1-C Line Creation API Routes
==========================================================
Include in fnol_api_server.py:
    from agents.doc_assist import fnol_line_creation_routes as _lc_routes
    app.include_router(_lc_routes.router)

Endpoints (prefix /api/v1/fnol/line-creation):
  GET  /health
  POST /create/{claim_id}
  GET  /claim/{claim_id}
  GET  /line/{line_id}
  GET  /col-codes            — returns full COL mapping for all SOR targets
"""
from __future__ import annotations
import hmac, logging, os
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from . import fnol_line_creation_agent as lca
from fnol_rbac import require_roles, Role, CLAIMS_ROLES

log = logging.getLogger("fnol.line_creation.routes")
router = APIRouter(prefix="/api/v1/fnol/line-creation", tags=["S1-C Line Creation"])

class ClaimantIn(BaseModel):
    claimant_id: str
    name: str
    role: str = "FIRST_PARTY"
    injury_reported: bool = False
    vehicle_id: Optional[str] = None
    known_carrier: Optional[str] = None

class VehicleIn(BaseModel):
    vehicle_id: str
    vin: Optional[str] = None
    role: str = "INSURED"
    damage_assessed: bool = False
    total_loss_flag: bool = False
    damage_areas: List[str] = []
    drivable: bool = True

class CreateLinesRequest(BaseModel):
    coverage_types: List[str]
    claimants: List[ClaimantIn] = []
    vehicles: List[VehicleIn] = []
    loss_cause: str = "COLLISION"
    jurisdiction_state: Optional[str] = None
    no_fault_indicator: Optional[bool] = None
    rental_eligible: bool = False
    tow_required: bool = False
    injury_reported: bool = False
    loss_severity: str = "MEDIUM"
    deductibles: Dict[str, float] = {}
    limits: Dict[str, float] = {}
    sor_target: str = "DUCK_CREEK"
    source_channel: str = "API"

@router.get("/health")
def lc_health(): return lca.health()

@router.post("/create/{claim_id}")
def create_lines(claim_id: str, body: CreateLinesRequest, _: str = Depends(require_roles(*CLAIMS_ROLES))):
    from dataclasses import asdict
    req = lca.LineCreationRequest(
        claim_id=claim_id,
        coverage_types=body.coverage_types,
        claimants=[lca.Claimant(**c.dict()) for c in body.claimants],
        vehicles=[lca.Vehicle(**v.dict()) for v in body.vehicles],
        loss_cause=body.loss_cause,
        jurisdiction_state=body.jurisdiction_state,
        no_fault_indicator=body.no_fault_indicator,
        rental_eligible=body.rental_eligible,
        tow_required=body.tow_required,
        injury_reported=body.injury_reported,
        loss_severity=body.loss_severity,
        deductibles=body.deductibles,
        limits=body.limits,
        sor_target=body.sor_target,
        source_channel=body.source_channel,
    )
    return asdict(lca.create_claim_lines(claim_id, req))

@router.get("/claim/{claim_id}")
def get_claim_lines(claim_id: str, _: str = Depends(require_roles(*CLAIMS_ROLES))):
    r = lca.get_lines_for_claim(claim_id)
    if not r: raise HTTPException(404, f"No lines for claim {claim_id}")
    return r

@router.get("/line/{line_id}")
def get_line(line_id: str, _: str = Depends(require_roles(*CLAIMS_ROLES))):
    r = lca.get_line(line_id)
    if not r: raise HTTPException(404, f"Line {line_id} not found")
    return r

@router.get("/col-codes")
def get_col_codes(_: str = Depends(require_roles(*CLAIMS_ROLES))):
    return {"sor_targets": {k: v for k, v in lca.SOR_COL_MAP.items()}}
