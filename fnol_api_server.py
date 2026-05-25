"""
FNOL Intelligence Platform — API Server
=======================================
FastAPI server exposing the FNOL pipeline + Adjuster Co-Pilot.

Endpoints (versioned at /api/v1):
  GET    /                                — landing redirect to /app
  GET    /app                             — serves the SPA (fnol_app.html)
  GET    /api/v1/health                   — liveness + provider/SOR status
  GET    /api/v1/config                   — UI bootstrap (POC policies, thresholds, version)
  POST   /api/v1/fnol/claims              — submit a new FNOL; runs full pipeline
  GET    /api/v1/fnol/claims              — list claims (in-memory or SOR)
  GET    /api/v1/fnol/claims/{cid}        — single claim record
  GET    /api/v1/fnol/claims/{cid}/pipeline — pipeline trace for a claim
  POST   /api/v1/fnol/policy/lookup       — policy lookup helper
  POST   /api/v1/fnol/copilot             — Adjuster Co-Pilot chat turn
  GET    /api/v1/fnol/copilot/alerts/{cid}— proactive alerts for a claim

Auth: simple X-API-Key header check (POC). Production: replace with OAuth2 +
RBAC + audit logging via gateway (Apigee / Kong / AWS API Gateway).

CORS: allow_origins=['*'] + allow_credentials=False to avoid the Starlette
incompatibility documented in prior sessions.
"""

from __future__ import annotations
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from fnol_llm_adapter import health as llm_health, resolve_provider
from fnol_sor_adapter import get_sor_adapter, CANONICAL_POLICIES
from fnol_workflow_engine import run_pipeline, PIPELINE_VERSION, THRESHOLDS
from fnol_runtime import BoundedStore
from fnol_claim import Claim, TelematicsPayload
from fnol_settings import settings
from fnol_api_deps import (
    require_api_key, rate_limited,
    client_error as _client_error, server_error as _server_error,
)
import fnol_copilot_agent as copilot
import fnol_conversational_agent as convo
import fnol_total_loss_agent as tla

# ── V3 Sub-Agents ──────────────────────────────────────────────────────────
# S1-A: Document Assist & Intelligent Classification
# Location: agents/doc_assist/fnol_doc_assist_routes.py
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent / "agents" / "doc_assist"))
from agents.doc_assist import fnol_doc_assist_routes as _doc_assist_routes
from agents.doc_assist import fnol_vendor_report_routes as _vrt_routes
from agents.doc_assist import fnol_line_creation_routes as _lc_routes
from agents.doc_assist import fnol_geo_supplier_routes as _geo_routes

log = logging.getLogger("fnol.api")
if not log.handlers:
    logging.basicConfig(level=settings.fnol_log_level)

# ───────────────────────────────────────────────────────────────────────────
# App
# ───────────────────────────────────────────────────────────────────────────

# API key validation: refuse to start under a known-default sentinel.
settings.require_valid_api_key()
API_KEY = settings.fnol_api_key
APP_HTML_PATH = Path(__file__).parent / "fnol_app.html"

# CORS allow-list (parsed from FNOL_ALLOWED_ORIGINS).
ALLOWED_ORIGINS = settings.allowed_origins_list

app = FastAPI(
    title="FNOL Intelligence Platform",
    description="Accenture FNOL Intelligence Platform — full 8-agent pipeline + Adjuster Co-Pilot (A9).",
    version=PIPELINE_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key", "Idempotency-Key"],
)

# ── V3 Sub-Agent Routers ────────────────────────────────────────────────────
app.include_router(_doc_assist_routes.router)   # S1-A Document Assist
app.include_router(_vrt_routes.router)           # S1-B Vendor Report Trigger
app.include_router(_lc_routes.router)            # S1-C Automated Line Creation
app.include_router(_geo_routes.router)           # S1-D Geo-Based Supplier Assignment

# ── Next-Gen Routers (SIU, Governance, ISO, LangGraph L3) ───────────────────
import fnol_siu_routes as _siu_routes
import fnol_governance_routes as _gov_routes
import fnol_iso_routes as _iso_routes
import fnol_v3_routes as _v3_routes
app.include_router(_siu_routes.router)           # A12 SIU Case Builder
app.include_router(_gov_routes.router)           # Governance / Compliance
app.include_router(_iso_routes.router)           # Verisk ISO ClaimSearch
app.include_router(_v3_routes.router)            # L3 LangGraph orchestration


# Bounded in-memory pipeline trace store (POC). Size + TTL prevent unbounded
# growth (DoS / PII retention failure). Production: persist to event store.
_PIPELINE_TRACES = BoundedStore(
    max_size=settings.fnol_trace_max,
    ttl_seconds=settings.fnol_trace_ttl_seconds,
)

# Idempotency: when a client repeats `Idempotency-Key` within the window we
# return the previous result instead of re-running the pipeline.
_IDEMPOTENCY_STORE = BoundedStore(
    max_size=settings.fnol_trace_max,
    ttl_seconds=settings.fnol_trace_ttl_seconds,
)

# Auth + rate limiting + error helpers now live in fnol_api_deps so router
# modules can import them without a circular dependency on this file.
# (re-exported via the import above for any external caller that referenced
# `fnol_api_server.require_api_key` etc.)


# ───────────────────────────────────────────────────────────────────────────
# Schemas
# ───────────────────────────────────────────────────────────────────────────

# FNOL submission shape is now the canonical `Claim` model. Re-aliased here
# so the OpenAPI schema name stays stable for clients.
FNOLSubmission = Claim

class PolicyLookupRequest(BaseModel):
    policy_number: str

class CoPilotRequest(BaseModel):
    claim_id: str
    question: str

class ConversationStartRequest(BaseModel):
    channel: Optional[str] = "WEB"          # WEB / IVR / SMS / MOBILE

class ConversationTurnRequest(BaseModel):
    session_id: Optional[str] = None
    user_message: str


# ───────────────────────────────────────────────────────────────────────────
# Routes — meta
# ───────────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root_redirect():
    if APP_HTML_PATH.exists():
        return RedirectResponse(url="/app")
    return JSONResponse({"message": "FNOL Intelligence Platform API",
                         "docs": "/docs",
                         "ui": "/app (fnol_app.html not found beside server)",
                         "version": PIPELINE_VERSION})

@app.get("/app", include_in_schema=False)
def serve_app():
    if not APP_HTML_PATH.exists():
        raise HTTPException(404, "fnol_app.html not found")
    return FileResponse(APP_HTML_PATH, media_type="text/html")

@app.get("/api/v1/health")
def health():
    """Liveness probe — booleans only. No infrastructure detail."""
    llm = llm_health()
    sor_h = get_sor_adapter().health()
    return {
        "service": "fnol-intelligence-platform",
        "version": PIPELINE_VERSION,
        "status": "ok",
        "llm_active": bool(llm.get("active_provider")),
        "sor_connected": bool(sor_h.get("connected", sor_h.get("healthy", True))),
        "uptime_check": time.time(),
    }

@app.get("/api/v1/config")
def config(_api_key: str = Depends(require_api_key)):
    """Authenticated bootstrap. Policy listing returns identifiers only —
    never names, emails, phones, or in-force ranges."""
    return {
        "version": PIPELINE_VERSION,
        "llm_provider": resolve_provider(),
        "sor_type": get_sor_adapter().name,
        "thresholds": THRESHOLDS,
        "policies": [
            {"policy_number": k,
             "jurisdiction_state": v["jurisdiction_state"]}
            for k, v in CANONICAL_POLICIES.items()
        ],
        "pipeline_stages": [
            {"id": "S0",  "name": "Pre-FNOL / Crash Detection",         "agent": "FNOL Intake Agent"},
            {"id": "S1",  "name": "FNOL Capture & Validation",           "agent": "FNOL Intake Agent"},
            {"id": "S1A", "name": "Document Assist & Intelligent Classification",
             "agent": "Document Assist Agent", "sub_agent": True,
             "doc_types": ["POLICE_REPORT","ESTIMATE","MEDICAL_RECORD","ATTORNEY_LETTER",
                           "PHOTO","VIDEO","COURT_NOTICE","HIPAA_RELEASE","OTHER"],
             "quality_threshold": 0.60, "sla_seconds": 90, "automation_rate": 0.93},
            {"id": "S1B", "name": "Vendor Report Trigger — VIN, Police & Court Records",
             "agent": "Vendor Report Agent", "sub_agent": True,
             "report_types": ["VIN_DECODE","NHTSA_RECALL","VEHICLE_HISTORY","POLICE_REPORT",
                              "COURT_RECORDS","ISO_CLAIM_SEARCH","NICB_SUPPLEMENT"],
             "sla_seconds": 300, "automation_rate": 0.97,
             "downstream_triggers": ["FRAUD_RESCORE","SUBRO_NOTIFY","BI_FAULT_INJECT",
                                     "LEGAL_TEAM_NOTIFY","ADJUSTER_DIARY_UPDATE"]},
            {"id": "S2",  "name": "Coverage Verification & Reservation", "agent": "Coverage & Liability Agent"},
            {"id": "S3",  "name": "Triage, Complexity & Assignment",     "agent": "Triage & Assignment Agent"},
            {"id": "S4A", "name": "Fraud & Anomaly Detection",           "agent": "Fraud Detection Agent"},
            {"id": "S4B", "name": "AI-Powered Damage Assessment",        "agent": "Damage Estimation Agent"},
            {"id": "A11", "name": "Total-Loss & Salvage Orchestrator",   "agent": "Total-Loss & Salvage Orchestrator",
             "conditional": True, "trigger": "S4B.total_loss==True",
             "next_agent": True, "duck_creek_alignment": True,
             "salvage_vendors": ["COPART", "IAA", "MOCK"]},
            {"id": "A12", "name": "SIU Case Builder",                   "agent": "SIU Case Builder",
             "conditional": True, "trigger": "S4A.fraud_risk_band in (HIGH, CRITICAL)",
             "next_agent": True, "naic_model_bulletin": True,
             "siu_teams": ["Organized Fraud Unit", "Identity & Prior Loss Unit",
                           "Claims Investigation Unit", "Pattern Analysis Unit"]},
            {"id": "S5",  "name": "BI Evaluation & Liability",           "agent": "Coverage & Liability Agent"},
            {"id": "S6",  "name": "Settlement & Payment Authorization",  "agent": "Settlement Agent"},
            {"id": "S7",  "name": "Subrogation & Recovery",              "agent": "Subrogation Agent"},
            {"id": "A9",  "name": "Adjuster Co-Pilot",                   "agent": "Adjuster Co-Pilot Agent"},
            {"id": "A10", "name": "Conversational FNOL Agent",           "agent": "Conversational FNOL Agent",
             "duck_creek_alignment": True},
        ],
    }


# ───────────────────────────────────────────────────────────────────────────
# Routes — claims
# ───────────────────────────────────────────────────────────────────────────

@app.post("/api/v1/fnol/claims", status_code=status.HTTP_201_CREATED)
def submit_claim(submission: FNOLSubmission,
                 idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
                 _api_key: str = Depends(rate_limited)):
    # Idempotency: re-issuing the same key within the TTL returns the prior
    # response instead of triggering a second pipeline run + duplicate claim.
    if idempotency_key:
        prior = _IDEMPOTENCY_STORE.get(idempotency_key)
        if prior is not None:
            return prior
    # Materialise a default telematics block when omitted so S0 has something
    # to read. The Claim model leaves it None by default.
    if submission.telematics is None:
        submission = submission.model_copy(update={"telematics": TelematicsPayload()})
    try:
        pipeline = run_pipeline(submission)
    except Exception as e:
        raise _server_error("run_pipeline failed", e)
    cid = pipeline["claim_id"]
    # Persist the original claim payload alongside the trace so downstream
    # endpoints (tl_evaluate, copilot) can recover full intake fields rather
    # than the SOR-returned summary record (which omits vehicle_year, state, …).
    pipeline["claim_payload"] = submission.model_dump(mode="python")
    _PIPELINE_TRACES.set(cid, pipeline)
    response = {
        "claim_id": cid,
        "final_status": pipeline["final_status"],
        "total_duration_ms": pipeline["total_duration_ms"],
        "llm_provider": pipeline.get("llm_provider"),
        "pipeline": pipeline,
    }
    if idempotency_key:
        _IDEMPOTENCY_STORE.set(idempotency_key, response)
    return response

@app.get("/api/v1/fnol/claims")
def list_claims(_api_key: str = Depends(require_api_key)):
    sor = get_sor_adapter()
    rows = sor.list_claims()
    for r in rows:
        cid = r.get("claim_id")
        pipe = _PIPELINE_TRACES.get(cid) if cid else None
        if pipe:
            r["summary"] = r.get("summary") or {}
            r["summary"]["final_status"] = pipe.get("final_status")
            r["summary"]["pipeline_duration_ms"] = pipe.get("total_duration_ms")
    return {"count": len(rows), "claims": rows}

@app.get("/api/v1/fnol/claims/{claim_id}")
def get_claim(claim_id: str,
              _api_key: str = Depends(require_api_key)):
    sor = get_sor_adapter()
    rec = sor.get_claim(claim_id)
    if not rec:
        raise _client_error(f"Claim {claim_id} not found", 404)
    # Break the SOR-record ↔ pipeline-trace reference cycle before returning.
    # `pipeline.claim_record` IS `rec` (same dict instance from sor.update_claim),
    # so assigning `rec["pipeline"] = pipeline` makes the encoder recurse forever.
    pipe = _PIPELINE_TRACES.get(claim_id)
    if pipe:
        pipe_safe = {k: v for k, v in pipe.items() if k != "claim_record"}
        out = dict(rec)
        out["pipeline"] = pipe_safe
        return out
    return rec

@app.get("/api/v1/fnol/claims/{claim_id}/pipeline")
def get_pipeline(claim_id: str,
                 _api_key: str = Depends(require_api_key)):
    pipeline = _PIPELINE_TRACES.get(claim_id)
    if not pipeline:
        raise _client_error(f"Pipeline trace not found for claim {claim_id}", 404)
    return pipeline


# ───────────────────────────────────────────────────────────────────────────
# Routes — utility / co-pilot
# ───────────────────────────────────────────────────────────────────────────

@app.post("/api/v1/fnol/policy/lookup")
def policy_lookup(req: PolicyLookupRequest,
                  _api_key: str = Depends(require_api_key)):
    sor = get_sor_adapter()
    pol = sor.lookup_policy(req.policy_number)
    if not pol:
        raise _client_error(f"Policy {req.policy_number} not found", 404)
    return pol

@app.post("/api/v1/fnol/copilot")
def copilot_chat(req: CoPilotRequest,
                 _api_key: str = Depends(rate_limited)):
    sor = get_sor_adapter()
    record = sor.get_claim(req.claim_id)
    pipeline = _PIPELINE_TRACES.get(req.claim_id)
    if not record or not pipeline:
        raise _client_error(f"No claim/pipeline for {req.claim_id}", 404)
    try:
        resp = copilot.chat(req.question, record, pipeline)
    except Exception as e:
        raise _server_error("copilot.chat failed", e)
    return resp.to_dict()

@app.get("/api/v1/fnol/copilot/alerts/{claim_id}")
def copilot_alerts(claim_id: str,
                   _api_key: str = Depends(require_api_key)):
    sor = get_sor_adapter()
    record = sor.get_claim(claim_id)
    pipeline = _PIPELINE_TRACES.get(claim_id)
    if not record or not pipeline:
        raise _client_error(f"No claim/pipeline for {claim_id}", 404)
    return {"claim_id": claim_id, "alerts": copilot.proactive_alerts(record, pipeline)}


# ───────────────────────────────────────────────────────────────────────────
# Routes — Conversational FNOL (A10 · L3 vision)
# ───────────────────────────────────────────────────────────────────────────

@app.post("/api/v1/fnol/conversation/start")
def convo_start(req: ConversationStartRequest,
                _api_key: str = Depends(require_api_key)):
    return convo.start_session()


@app.post("/api/v1/fnol/conversation/turn")
def convo_turn(req: ConversationTurnRequest,
               _api_key: str = Depends(rate_limited)):
    try:
        result = convo.turn(req.session_id, req.user_message)
    except Exception as e:
        raise _server_error("convo.turn failed", e)
    # If a claim was finalized, store its pipeline trace too.
    if result.get("done") and result.get("claim_result"):
        cr = result["claim_result"]
        pipe = cr.get("pipeline")
        if pipe and pipe.get("claim_id"):
            _PIPELINE_TRACES.set(pipe["claim_id"], pipe)
    return result


@app.get("/api/v1/fnol/conversation/{session_id}")
def convo_view(session_id: str,
               _api_key: str = Depends(require_api_key)):
    v = convo.session_view(session_id)
    if not v:
        raise _client_error(f"Session {session_id} not found", 404)
    return v


# ───────────────────────────────────────────────────────────────────────────
# Routes — A11 Total-Loss & Salvage Orchestrator
# ───────────────────────────────────────────────────────────────────────────

class TotalLossEvaluateRequest(BaseModel):
    claim_id: str
    state: Optional[str] = None             # override; else inferred from claim

class SalvageAssignRequest(BaseModel):
    evaluation_id: str
    vendor: str = "auto"                    # auto | copart | iaa | mock

class OwnerDecisionRequest(BaseModel):
    evaluation_id: str
    choice: str                             # carrier_retains_salvage | owner_retains_salvage

class OwnerLetterRequest(BaseModel):
    evaluation_id: str
    choice: Optional[str] = None


@app.post("/api/v1/fnol/total-loss/evaluate")
def tl_evaluate(req: TotalLossEvaluateRequest,
                _api_key: str = Depends(rate_limited)):
    pipe = _PIPELINE_TRACES.get(req.claim_id)
    if not pipe:
        raise _client_error(f"Claim {req.claim_id} not found in pipeline trace store", 404)
    s4b = next((s["outputs"] for s in pipe.get("stages", []) if s["stage_id"] == "S4B"), {})
    # Prefer the original intake payload (vehicle_year, state, mileage…) over
    # the SOR-returned record, which is a thin summary missing those fields.
    payload = dict(pipe.get("claim_payload") or pipe.get("claim_record") or {})
    payload["claim_id"] = req.claim_id
    try:
        claim = Claim(**payload)
    except Exception as e:
        raise _client_error(f"persisted claim payload failed validation: {e}", 400)
    try:
        ev = tla.evaluate(claim, s4b, state=req.state)
    except Exception as e:
        raise _server_error("A11 evaluate failed", e)
    return tla.get_evaluation(ev.evaluation_id)


@app.post("/api/v1/fnol/total-loss/assign-salvage")
def tl_assign_salvage(req: SalvageAssignRequest,
                      _api_key: str = Depends(rate_limited)):
    try:
        ev = tla.assign_salvage(req.evaluation_id, vendor=req.vendor)
    except KeyError:
        raise _client_error(f"Evaluation {req.evaluation_id} not found", 404)
    except ValueError as e:
        raise _client_error(str(e), 400)
    except NotImplementedError:
        # Live-mode vendor adapter not wired — surface as 501, not 500.
        raise _client_error(f"Salvage vendor '{req.vendor}' not configured for live mode", 501)
    except Exception as e:
        raise _server_error("assign_salvage failed", e)
    return tla.get_evaluation(ev.evaluation_id)


@app.post("/api/v1/fnol/total-loss/owner-decision")
def tl_owner_decision(req: OwnerDecisionRequest,
                      _api_key: str = Depends(require_api_key)):
    try:
        ev = tla.record_owner_decision(req.evaluation_id, req.choice)
    except KeyError:
        raise _client_error(f"Evaluation {req.evaluation_id} not found", 404)
    except ValueError as e:
        # ValueError messages from the agent are validation-shaped — safe to surface.
        raise _client_error(str(e), 400)
    return tla.get_evaluation(ev.evaluation_id)


@app.post("/api/v1/fnol/total-loss/letter")
def tl_letter(req: OwnerLetterRequest,
              _api_key: str = Depends(rate_limited)):
    try:
        letter = tla.generate_owner_letter(req.evaluation_id, choice=req.choice)
    except KeyError:
        raise _client_error(f"Evaluation {req.evaluation_id} not found", 404)
    except Exception as e:
        raise _server_error("generate_owner_letter failed", e)
    return {"evaluation_id": req.evaluation_id, "letter": letter}


@app.get("/api/v1/fnol/total-loss/{evaluation_id}")
def tl_get(evaluation_id: str,
           _api_key: str = Depends(require_api_key)):
    ev = tla.get_evaluation(evaluation_id)
    if not ev:
        raise _client_error(f"Evaluation {evaluation_id} not found", 404)
    return ev


@app.get("/api/v1/fnol/total-loss/by-claim/{claim_id}")
def tl_by_claim(claim_id: str,
                _api_key: str = Depends(require_api_key)):
    ev = tla.get_evaluation_by_claim(claim_id)
    if not ev:
        raise _client_error(f"No evaluation found for claim {claim_id}", 404)
    return ev


@app.get("/api/v1/fnol/total-loss")
def tl_list(_api_key: str = Depends(require_api_key), limit: int = 50):
    return {"evaluations": tla.list_evaluations(limit=limit)}


# ───────────────────────────────────────────────────────────────────────────
# Entrypoint
# ───────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("fnol_api_server:app",
                host=settings.fnol_host, port=settings.fnol_port, reload=False)
