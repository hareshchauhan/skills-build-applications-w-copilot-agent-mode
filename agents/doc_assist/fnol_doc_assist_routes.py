"""
FNOL Intelligence Platform — S1-A Document Assist API Routes
=============================================================
Drop-in extension for fnol_api_server.py.

New endpoints under /api/v1/fnol/doc-assist/:
  POST   /api/v1/fnol/doc-assist/classify           — classify a single document
  POST   /api/v1/fnol/doc-assist/batch/{claim_id}   — classify a batch for a claim
  GET    /api/v1/fnol/doc-assist/claims/{claim_id}/documents — list docs for claim
  GET    /api/v1/fnol/doc-assist/claims/{claim_id}/missing   — missing docs report
  GET    /api/v1/fnol/doc-assist/claims/{claim_id}/alerts    — alerts for claim
  PUT    /api/v1/fnol/doc-assist/alerts/{alert_id}/acknowledge — acknowledge alert
  GET    /api/v1/fnol/doc-assist/documents/{document_id}     — single document record
  GET    /api/v1/fnol/doc-assist/health                      — agent health

HOW TO WIRE INTO fnol_api_server.py
-------------------------------------
Add at the bottom of fnol_api_server.py (before the __main__ block):

    import fnol_doc_assist_routes as _doc_assist_routes
    app.include_router(_doc_assist_routes.router)

Or manually copy the routes below into fnol_api_server.py.
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import fnol_doc_assist_agent as doc_assist
from fnol_rbac import require_roles, Role, CLAIMS_ROLES

router = APIRouter(prefix="/api/v1/fnol/doc-assist", tags=["S1-A Document Assist"])


# ───────────────────────────────────────────────────────────────────────────
# Request / Response Schemas
# ───────────────────────────────────────────────────────────────────────────

class DocumentClassifyRequest(BaseModel):
    """Single-document classification request."""
    claim_id:        str
    file_name:       str
    file_b64:        Optional[str]   = None    # Base64-encoded file bytes
    file_size_bytes: int             = 0
    source_channel:  str             = "WEB"   # MOBILE | EMAIL | FAX | API | WEB | DRP_PARTNER
    mime_type:       Optional[str]   = None
    content_text:    Optional[str]   = None    # OCR pre-extract or raw text
    claim_context:   Dict[str, Any]  = Field(default_factory=dict)


class DocumentBatchRequest(BaseModel):
    """Batch document classification for a single claim (Stage S1-A)."""
    documents:     List[Dict[str, Any]]  = Field(default_factory=list)
    claim_context: Dict[str, Any]        = Field(default_factory=dict)


class MissingDocsRequest(BaseModel):
    claim_type:     str = "COLLISION"
    coverage_types: List[str] = Field(default_factory=list)


# ───────────────────────────────────────────────────────────────────────────
# Routes
# ───────────────────────────────────────────────────────────────────────────

@router.get("/health")
def doc_assist_health():
    """S1-A agent liveness + stats."""
    return doc_assist.health()


@router.post("/classify")
def classify_single(
    req: DocumentClassifyRequest,
    _key: str = Depends(require_roles(*CLAIMS_ROLES)),
):
    """
    Classify a single document. Accepts file as base64 or raw text.
    Returns DocumentRecord with type, quality score, extracted data, tasks, alerts.
    """
    try:
        record = doc_assist.classify_document(
            claim_id        = req.claim_id,
            file_name       = req.file_name,
            file_bytes      = None,
            file_b64        = req.file_b64,
            file_size_bytes = req.file_size_bytes,
            source_channel  = req.source_channel,
            mime_type       = req.mime_type,
            content_text    = req.content_text,
            claim_context   = req.claim_context,
        )
        from dataclasses import asdict
        return asdict(record)
    except Exception as exc:
        import logging
        logging.getLogger("fnol.doc_assist.api").exception("classify_single failed")
        raise HTTPException(status_code=500, detail=f"Classification failed: {exc}")


@router.post("/batch/{claim_id}")
def classify_batch(
    claim_id: str,
    req: DocumentBatchRequest,
    _key: str = Depends(require_roles(*CLAIMS_ROLES)),
):
    """
    Process a batch of documents for a single FNOL claim (S1-A full run).
    Returns DocAssistResult with all documents, tasks, alerts, missing-docs report.
    """
    try:
        result = doc_assist.process_claim_documents(
            claim_id      = claim_id,
            documents     = req.documents,
            claim_context = req.claim_context,
        )
        from dataclasses import asdict
        return asdict(result)
    except Exception as exc:
        import logging
        logging.getLogger("fnol.doc_assist.api").exception("classify_batch failed")
        raise HTTPException(status_code=500, detail=f"Batch classification failed: {exc}")


@router.get("/claims/{claim_id}/documents")
def list_claim_documents(
    claim_id: str,
    _key: str = Depends(require_roles(*CLAIMS_ROLES)),
):
    """List all classified documents for a claim."""
    docs = doc_assist.get_documents_for_claim(claim_id)
    return {"claim_id": claim_id, "count": len(docs), "documents": docs}


@router.get("/claims/{claim_id}/alerts")
def list_claim_alerts(
    claim_id: str,
    _key: str = Depends(require_roles(*CLAIMS_ROLES)),
):
    """List all dispatched alerts for a claim."""
    alerts = doc_assist.list_alerts(claim_id)
    return {"claim_id": claim_id, "count": len(alerts), "alerts": alerts}


@router.post("/claims/{claim_id}/missing")
def check_missing_documents(
    claim_id: str,
    req: MissingDocsRequest,
    _key: str = Depends(require_roles(*CLAIMS_ROLES)),
):
    """Check which required documents are missing for a claim."""
    from dataclasses import asdict
    result = doc_assist.get_missing_documents(claim_id, req.claim_type, req.coverage_types)
    return asdict(result)


@router.get("/documents/{document_id}")
def get_document(
    document_id: str,
    _key: str = Depends(require_roles(*CLAIMS_ROLES)),
):
    """Retrieve a single document record."""
    doc = doc_assist.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    return doc


@router.put("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(
    alert_id: str,
    _key: str = Depends(require_roles(*CLAIMS_ROLES)),
):
    """Mark an alert as acknowledged by an adjuster."""
    updated = doc_assist.acknowledge_alert(alert_id)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    return updated
