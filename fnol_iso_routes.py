"""
FNOL Intelligence Platform — Verisk ISO ClaimSearch API Routes
==============================================================
HTTP surface for the ISO ClaimSearch adapter (live | shell | mock modes).

Endpoints under /api/v1/fnol/iso/:
  GET    /health                — adapter mode + cache stats
  POST   /query                 — submit an ISO inquiry (rate-limited)
  GET    /cache                 — cache statistics
  DELETE /cache/{claim_id}      — invalidate cache entries for a claim

Wire into fnol_api_server.py:
    import fnol_iso_routes
    app.include_router(fnol_iso_routes.router)
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import fnol_iso_adapter as iso
from fnol_api_deps import require_api_key, rate_limited, client_error, server_error


log = logging.getLogger("fnol.iso.routes")
router = APIRouter(prefix="/api/v1/fnol/iso", tags=["Verisk ISO ClaimSearch"])


# ── Schemas ─────────────────────────────────────────────────────────────

class ISOQueryRequest(BaseModel):
    """Subset of ISOClaimSearchRequest fields that callers should supply.
    The adapter validates the (name+DOB) / VIN / policy_number requirement."""
    claim_id: str = Field(..., min_length=1, max_length=128)
    claimant_first_name: Optional[str] = Field(default=None, max_length=128)
    claimant_last_name: Optional[str] = Field(default=None, max_length=128)
    claimant_dob: Optional[str] = Field(default=None, max_length=16)
    claimant_ssn_last4: Optional[str] = Field(default=None, max_length=4)
    claimant_zip: Optional[str] = Field(default=None, max_length=10)
    vin: Optional[str] = Field(default=None, max_length=17)
    vehicle_year: Optional[int] = Field(default=None, ge=1900, le=2100)
    vehicle_make: Optional[str] = Field(default=None, max_length=64)
    vehicle_model: Optional[str] = Field(default=None, max_length=64)
    license_plate: Optional[str] = Field(default=None, max_length=16)
    license_state: Optional[str] = Field(default=None, max_length=2)
    policy_number: Optional[str] = Field(default=None, max_length=64)
    loss_date: Optional[str] = Field(default=None, max_length=16)


# ── Routes ──────────────────────────────────────────────────────────────

@router.get("/health")
def iso_health(_: str = Depends(require_api_key)):
    return iso.health()


@router.post("/query")
def iso_query(req: ISOQueryRequest, _: str = Depends(rate_limited)):
    """ISO query is billed per inquiry — rate-limited per API key."""
    try:
        request_obj = iso.ISOClaimSearchRequest(**req.model_dump(exclude_none=True))
        response = iso.query(request_obj)
    except ValueError as e:
        # Validation failure inside the adapter (e.g. missing identity fields)
        raise client_error(str(e), 400)
    except Exception as e:
        raise server_error("iso.query failed", e)
    return asdict(response) if is_dataclass(response) else response


@router.get("/cache")
def iso_cache(_: str = Depends(require_api_key)):
    return iso.cache_stats()


@router.delete("/cache/{claim_id}")
def iso_cache_delete(claim_id: str, _: str = Depends(require_api_key)):
    removed = iso.invalidate_cache(claim_id)
    return {"claim_id": claim_id, "removed": bool(removed)}
