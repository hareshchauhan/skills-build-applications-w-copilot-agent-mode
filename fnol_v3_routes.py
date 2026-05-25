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
from fastapi import APIRouter, Depends, status

import fnol_langgraph_engine as lg
from fnol_claim import Claim
from fnol_api_deps import require_api_key, rate_limited, client_error, server_error


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
def v3_health(_: str = Depends(require_api_key)):
    # /health intentionally does NOT raise on LANGGRAPH_AVAILABLE=False —
    # the body itself returns `l3_enabled` so smoke tests can detect the
    # missing-dependency case without a 503.
    return lg.get_graph_health()


@router.get("/claims")
def list_threads(limit: int = 50, _: str = Depends(require_api_key)):
    if not getattr(lg, "LANGGRAPH_AVAILABLE", False):
        return {"threads": [], "l3_enabled": False}
    limit = max(1, min(limit, 200))
    try:
        threads = lg.list_threads(limit=limit)
    except Exception as e:
        raise server_error("lg.list_threads failed", e)
    return {"threads": threads, "l3_enabled": True}


@router.post("/claims", status_code=status.HTTP_201_CREATED)
def submit_claim(claim: Claim, _: str = Depends(rate_limited)):
    """Run a Claim through the L3 LangGraph engine. Rate-limited because the
    graph invokes LLM-backed nodes (SIU memo, ROR draft, etc.)."""
    _require_langgraph()
    try:
        result = lg.run_claim_langgraph(claim)
    except Exception as e:
        raise server_error("lg.run_claim_langgraph failed", e)
    return result


@router.get("/claims/{thread_id}")
def get_thread(thread_id: str, _: str = Depends(require_api_key)):
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
