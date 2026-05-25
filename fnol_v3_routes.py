"""
FNOL Intelligence Platform — L3 LangGraph API Routes
=====================================================
Optional L3 surface that runs claims through the LangGraph StateGraph engine
with durable checkpoints. Only active when the `langgraph` and
`langgraph-checkpoint-sqlite` packages are installed; otherwise endpoints
return 503 with a clear install hint.

Endpoints under /api/v1/fnol/v3/:
  GET    /health                          — L3 readiness + graph topology
  GET    /claims                          — list active checkpointed threads
  POST   /claims                          — run a claim through LangGraph
  GET    /claims/{thread_id}              — fetch thread state by id

Wire into fnol_api_server.py:
    import fnol_v3_routes
    app.include_router(fnol_v3_routes.router)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

import fnol_langgraph_engine as lg
from fnol_claim import Claim
from fnol_api_deps import client_error, server_error
from fnol_rbac import require_roles, require_roles_rate_limited, Role, CLAIMS_ROLES, READ_ROLES, SUPERVISOR_UP


log = logging.getLogger("fnol.v3.routes")
router = APIRouter(prefix="/api/v1/fnol/v3", tags=["L3 LangGraph"])


def _require_langgraph() -> None:
    """Translate the engine's `LANGGRAPH_AVAILABLE=False` state into a 503
    so clients get a clear deploy-time signal instead of a 500."""
    if not getattr(lg, "LANGGRAPH_AVAILABLE", False):
        raise client_error(
            ("LangGraph not installed on this server. "
             "Install with: pip install langgraph langgraph-checkpoint-sqlite"),
            503,
        )


# ── Routes ──────────────────────────────────────────────────────────────

@router.get("/health")
def v3_health(_ = Depends(require_roles(*READ_ROLES))):
    # /health intentionally does NOT raise on LANGGRAPH_AVAILABLE=False —
    # the body itself returns `l3_enabled` so smoke tests can detect the
    # missing-dependency case without a 503.
    return lg.get_graph_health()


@router.get("/claims")
def list_threads(limit: int = 50, _ = Depends(require_roles(*SUPERVISOR_UP, Role.READONLY))):
    if not getattr(lg, "LANGGRAPH_AVAILABLE", False):
        return {"threads": [], "l3_enabled": False}
    limit = max(1, min(limit, 200))
    try:
        threads = lg.list_threads(limit=limit)
    except Exception as e:
        raise server_error("lg.list_threads failed", e)
    return {"threads": threads, "l3_enabled": True}


@router.post("/claims", status_code=status.HTTP_201_CREATED)
def submit_claim(claim: Claim, _ = Depends(require_roles_rate_limited(*CLAIMS_ROLES))):
    """Run a Claim through the L3 LangGraph engine. Rate-limited because the
    graph invokes LLM-backed nodes (SIU memo, ROR draft, etc.)."""
    _require_langgraph()
    try:
        result = lg.run_claim_langgraph(claim)
    except Exception as e:
        raise server_error("lg.run_claim_langgraph failed", e)
    return result


@router.get("/claims/{thread_id}")
def get_thread(thread_id: str, _ = Depends(require_roles(*READ_ROLES))):
    _require_langgraph()
    try:
        state = lg.get_thread_state(thread_id)
    except KeyError:
        raise client_error(f"Thread {thread_id} not found", 404)
    except Exception as e:
        raise server_error("lg.get_thread_state failed", e)
    if not state:
        raise client_error(f"Thread {thread_id} not found", 404)
    return state


class ResumeRequest(BaseModel):
    """Adjuster or SIU decision to resume a suspended HITL thread."""
    decision:           Optional[str]   = "APPROVED"
    adjuster_id:        Optional[str]   = None
    adjuster_notes:     Optional[str]   = None
    approved_bi_usd:    Optional[float] = None
    investigator_notes: Optional[str]   = None
    disposition:        Optional[str]   = None


@router.post("/claims/{thread_id}/resume")
def resume_thread(
    thread_id: str,
    body: ResumeRequest,
    _ = Depends(require_roles(*SUPERVISOR_UP)),
):
    """
    Resume a graph thread suspended at a HITL interrupt gate.

    Injects the adjuster/SIU decision into the graph state and continues
    execution from the checkpoint.  Returns the pipeline trace if the graph
    reaches END, or a new interrupt payload if another gate is encountered.

    Typical payloads
    ----------------
    Triage gate  — {"decision": "APPROVED", "adjuster_id": "ADJ-001"}
    SIU hold     — {"disposition": "CLEARED", "investigator_notes": "All good"}
    BI gate      — {"decision": "ADJUST_OFFER", "approved_bi_usd": 45000}
    """
    _require_langgraph()
    decision_dict = body.model_dump(exclude_none=True)
    try:
        result = lg.resume_thread(thread_id, decision_dict)
    except KeyError:
        raise client_error(f"Thread {thread_id} not found", 404)
    except Exception as e:
        raise server_error("lg.resume_thread failed", e)
    return result
