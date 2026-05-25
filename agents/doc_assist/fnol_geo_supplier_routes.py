"""
FNOL Intelligence Platform — S1-D Geo Supplier Routes
Include: app.include_router(_geo_routes.router)

Endpoints (prefix /api/v1/fnol/geo-supplier):
  GET  /health
  POST /assign/{claim_id}
  GET  /assignment/{claim_id}
  GET  /drp-network           — full DRP shop list
  GET  /field-adjusters       — field adjuster roster
"""
from __future__ import annotations
import hmac, logging, os
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from . import fnol_geo_supplier_agent as gsa
from fnol_api_deps import require_api_key

log = logging.getLogger("fnol.geo_supplier.routes")
router = APIRouter(prefix="/api/v1/fnol/geo-supplier", tags=["S1-D Geo Supplier"])

class GeoIn(BaseModel):
    lat: float; lon: float
    address: Optional[str]=None; city: Optional[str]=None
    state: Optional[str]=None; zip_code: Optional[str]=None

class AssignRequest(BaseModel):
    loss_location: GeoIn
    vehicle_location: Optional[GeoIn]=None
    vehicle_type: str="STANDARD"
    drivable: bool=True
    tow_required: bool=False
    damage_areas: List[str]=[]
    photo_quality_score: Optional[float]=None
    photo_count: Optional[int]=None
    jurisdiction_state: Optional[str]=None
    claimant_name: Optional[str]=None
    claimant_phone: Optional[str]=None
    claimant_email: Optional[str]=None
    preferred_channel: str="SMS"
    vin: Optional[str]=None
    coverage_summary: Optional[str]=None

@router.get("/health")
def geo_health(): return gsa.health()

@router.post("/assign/{claim_id}")
def assign(claim_id: str, body: AssignRequest, _: str = Depends(require_api_key)):
    from dataclasses import asdict
    req = gsa.GeoAssignmentRequest(
        claim_id=claim_id,
        loss_location=gsa.GeoLocation(**body.loss_location.dict()),
        vehicle_location=gsa.GeoLocation(**body.vehicle_location.dict()) if body.vehicle_location else None,
        vehicle_type=body.vehicle_type, drivable=body.drivable, tow_required=body.tow_required,
        damage_areas=body.damage_areas, photo_quality_score=body.photo_quality_score,
        photo_count=body.photo_count, jurisdiction_state=body.jurisdiction_state,
        claimant_name=body.claimant_name, claimant_phone=body.claimant_phone,
        claimant_email=body.claimant_email, preferred_channel=body.preferred_channel,
        vin=body.vin, coverage_summary=body.coverage_summary,
    )
    return asdict(gsa.assign_supplier(claim_id, req))

@router.get("/assignment/{claim_id}")
def get_assignment(claim_id: str, _: str = Depends(require_api_key)):
    r = gsa.get_assignment(claim_id)
    if not r: raise HTTPException(404, f"No assignment for {claim_id}")
    return r

@router.get("/drp-network")
def drp_network(_: str = Depends(require_api_key)):
    return {"shops": gsa._DRP_NETWORK, "count": len(gsa._DRP_NETWORK)}

@router.get("/field-adjusters")
def field_adjusters(_: str = Depends(require_api_key)):
    return {"adjusters": gsa._FIELD_ADJUSTER_ROSTER, "count": len(gsa._FIELD_ADJUSTER_ROSTER)}
