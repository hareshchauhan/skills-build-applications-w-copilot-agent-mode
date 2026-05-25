"""
FNOL Intelligence Platform — A12 SIU API Routes
================================================
HTTP surface for the A12 SIU Case Builder agent.

Endpoints under /api/v1/fnol/siu/:
  GET    /health                          — agent + store health
  GET    /                                — list cases (paginated)
  POST   /open                            — open SIU case from pipeline trace
  GET    /{case_id}                       — fetch a single case
  GET    /by-claim/{claim_id}             — fetch the case for a given claim
  POST   /evidence                        — append evidence item
  POST   /notes                           — save adjuster notes
  POST   /referral                        — generate SIU referral memo (LLM)
  POST   /close                           — close case with disposition

Wire into fnol_api_server.py:
    import fnol_siu_routes
    app.include_router(fnol_siu_routes.router)
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

import fnol_siu_agent as siu
from fnol_api_deps import client_error, server_error
from fnol_rbac import require_roles, require_roles_rate_limited, Role, SIU_ROLES


log = logging.getLogger("fnol.siu.routes")
router = APIRouter(prefix="/api/v1/fnol/siu", tags=["A12 SIU Case Builder"])


# ── Schemas ─────────────────────────────────────────────────────────────

class OpenCaseRequest(BaseModel):
    claim_id: str = Field(..., min_length=1, max_length=128)


class EvidenceRequest(BaseModel):
    case_id: str = Field(..., min_length=1, max_length=128)
    evidence_type: str = Field(default="DOCUMENT", max_length=32)
    description: str = Field(..., min_length=1, max_length=500)
    source: str = Field(default="ADJUSTER", max_length=100)


class NotesRequest(BaseModel):
    case_id: str = Field(..., min_length=1, max_length=128)
    notes: str = Field(..., min_length=1, max_length=2000)


class ReferralRequest(BaseModel):
    case_id: str = Field(..., min_length=1, max_length=128)


class CloseCaseRequest(BaseModel):
    case_id: str = Field(..., min_length=1, max_length=128)
    disposition: str = Field(..., min_length=1, max_length=64)   # CLEARED | CONFIRMED_FRAUD | CLOSED_INCONCLUSIVE
    investigator_notes: Optional[str] = Field(default=None, max_length=2000)


# ── Helpers ─────────────────────────────────────────────────────────────

def _case_to_dict(case) -> Dict[str, Any]:
    """Convert an SIUCase dataclass to a JSON-safe dict."""
    return asdict(case)


def _fetch_pipeline_trace(claim_id: str) -> Optional[Dict[str, Any]]:
    """Pull a pipeline trace from the api_server's bounded store.
    Imported lazily to avoid a circular dependency with fnol_api_server."""
    from fnol_api_server import _PIPELINE_TRACES   # type: ignore[attr-defined]
    return _PIPELINE_TRACES.get(claim_id)


# ── Routes ──────────────────────────────────────────────────────────────

@router.get("/health")
def siu_health(_ = Depends(require_roles(*SIU_ROLES))):
    return siu.health()


@router.get("")
@router.get("/")
def list_cases(limit: int = 50, _ = Depends(require_roles(*SIU_ROLES))):
    limit = max(1, min(limit, 200))
    return {"cases": siu.list_cases(limit=limit)}


@router.post("/open", status_code=status.HTTP_201_CREATED)
def open_case(req: OpenCaseRequest, _ = Depends(require_roles(*SIU_ROLES))):
    pipeline = _fetch_pipeline_trace(req.claim_id)
    if not pipeline:
        raise client_error(f"No pipeline trace found for claim {req.claim_id}", 404)
    try:
        case = siu.open_case(req.claim_id, pipeline)
    except KeyError as e:
        raise client_error(str(e), 404)
    except ValueError as e:
        # Fraud band not eligible (LOW/MEDIUM) — surface as 422 so adjusters
        # can distinguish "not found" from "not SIU-eligible".
        raise client_error(str(e), 422)
    except Exception as e:
        raise server_error("siu.open_case failed", e)
    return _case_to_dict(case)


@router.get("/by-claim/{claim_id}")
def get_case_by_claim(claim_id: str, _ = Depends(require_roles(*SIU_ROLES))):
    case = siu.get_case_by_claim(claim_id)
    if case is None:
        raise client_error(f"No SIU case found for claim {claim_id}", 404)
    return _case_to_dict(case)


@router.get("/{case_id}")
def get_case(case_id: str, _ = Depends(require_roles(*SIU_ROLES))):
    case = siu.get_case(case_id)
    if case is None:
        raise client_error(f"SIU case {case_id} not found", 404)
    return _case_to_dict(case)


@router.post("/evidence")
def add_evidence(req: EvidenceRequest, _ = Depends(require_roles(*SIU_ROLES))):
    try:
        case = siu.add_evidence(req.case_id, req.evidence_type, req.description, req.source)
    except KeyError as e:
        raise client_error(str(e), 404)
    except Exception as e:
        raise server_error("siu.add_evidence failed", e)
    return _case_to_dict(case)


@router.post("/notes")
def save_notes(req: NotesRequest, _ = Depends(require_roles(*SIU_ROLES))):
    try:
        case = siu.save_notes(req.case_id, req.notes)
    except KeyError as e:
        raise client_error(str(e), 404)
    except Exception as e:
        raise server_error("siu.save_notes failed", e)
    return _case_to_dict(case)


@router.post("/referral")
def generate_referral(req: ReferralRequest, _ = Depends(require_roles_rate_limited(*SIU_ROLES))):
    """Generate SIU referral memo via LLM. Rate-limited to bound provider cost."""
    try:
        case = siu.generate_referral(req.case_id)
    except KeyError as e:
        raise client_error(str(e), 404)
    except Exception as e:
        raise server_error("siu.generate_referral failed", e)
    return _case_to_dict(case)


@router.post("/close")
def close_case(req: CloseCaseRequest, _ = Depends(require_roles(*SIU_ROLES))):
    try:
        case = siu.close_case(req.case_id, req.disposition, req.investigator_notes)
    except KeyError as e:
        raise client_error(str(e), 404)
    except ValueError as e:
        raise client_error(str(e), 400)
    except Exception as e:
        raise server_error("siu.close_case failed", e)
    return _case_to_dict(case)
