"""
FNOL Intelligence Platform — S1-B Vendor Report Trigger API Routes
==================================================================
FastAPI router. Include in fnol_api_server.py:
    from agents.doc_assist import fnol_vendor_report_routes as _vrt_routes
    app.include_router(_vrt_routes.router)

Endpoints (prefix /api/v1/fnol/vendor-report):
  GET  /health
  POST /trigger/{claim_id}          — trigger all vendor reports for a claim
  GET  /status/{claim_id}           — latest result for a claim
  GET  /report/{report_id}          — fetch by result ID
  GET  /triggers/{claim_id}         — list downstream triggers
  PUT  /triggers/{trigger_id}/ack   — acknowledge a trigger
"""
from __future__ import annotations

import hmac
import logging
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from . import fnol_vendor_report_agent as vra

log = logging.getLogger("fnol.vendor_report.routes")

router = APIRouter(
    prefix="/api/v1/fnol/vendor-report",
    tags=["S1-B Vendor Report Trigger"],
)

# ── Auth ─────────────────────────────────────────────────────────────────
from fnol_api_deps import require_api_key as _require_api_key

# ── Request / Response models ─────────────────────────────────────────────

class TriggerRequest(BaseModel):
    vin: Optional[str] = None
    police_report_number: Optional[str] = None
    jurisdiction_state: Optional[str] = None
    litigation_indicator: bool = False
    claimant_names: List[str] = []
    claimant_dobs: List[str] = []
    accident_date: Optional[str] = None
    accident_location: Optional[str] = None
    loss_cause: Optional[str] = None
    injury_reported: bool = False
    source_channel: str = "API"

# ── Routes ────────────────────────────────────────────────────────────────

@router.get("/health")
def vendor_report_health():
    return vra.health()

@router.post("/trigger/{claim_id}")
def trigger_vendor_reports(
    claim_id: str,
    body: TriggerRequest,
    _api_key: str = Header(default=None),
):
    _require_api_key(_api_key)
    req = vra.VendorReportRequest(
        claim_id=claim_id,
        vin=body.vin,
        police_report_number=body.police_report_number,
        jurisdiction_state=body.jurisdiction_state,
        litigation_indicator=body.litigation_indicator,
        claimant_names=body.claimant_names,
        claimant_dobs=body.claimant_dobs,
        accident_date=body.accident_date,
        accident_location=body.accident_location,
        loss_cause=body.loss_cause,
        injury_reported=body.injury_reported,
        source_channel=body.source_channel,
    )
    result = vra.trigger_vendor_reports(claim_id, req)
    from dataclasses import asdict
    return asdict(result)

@router.get("/status/{claim_id}")
def get_report_status(
    claim_id: str,
    _api_key: str = Header(default=None),
):
    _require_api_key(_api_key)
    result = vra.get_report_status(claim_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"No vendor report result for claim {claim_id}")
    return result

@router.get("/report/{report_id}")
def get_report(
    report_id: str,
    _api_key: str = Header(default=None),
):
    _require_api_key(_api_key)
    r = vra.get_report(report_id)
    if not r:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found")
    return r

@router.get("/triggers/{claim_id}")
def list_triggers(
    claim_id: str,
    _api_key: str = Header(default=None),
):
    _require_api_key(_api_key)
    return {"claim_id": claim_id, "triggers": vra.list_downstream_triggers(claim_id)}

@router.put("/triggers/{trigger_id}/ack")
def ack_trigger(
    trigger_id: str,
    _api_key: str = Header(default=None),
):
    _require_api_key(_api_key)
    t = vra.acknowledge_trigger(trigger_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"Trigger {trigger_id} not found")
    return t
