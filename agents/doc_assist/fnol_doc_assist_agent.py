"""
FNOL Intelligence Platform — S1-A Document Assist & Intelligent Classification
==============================================================================
V3 New Sub-Agent · Runs immediately after FNOL Capture (Stage 01).
Blueprint: 00_-_Claims_FNOL_Auto_Blueprint_V3.html §Stage 01-A

Responsibilities
----------------
1. **Document Ingestion**       — accepts files via base64 payload or URL reference;
                                   deduplicates re-submitted documents against prior claim docs.
2. **Multi-Modal Classification** — LLM vision + NLP identifies document type with confidence.
3. **OCR + LLM Extraction**    — extracts structured fields (parties, dates, amounts, VINs,
                                   ICD codes, policy numbers) from unstructured PDFs/images.
4. **Quality Scoring**          — 0.0–1.0 score per document; < 0.60 triggers re-submission
                                   request to claimant via preferred channel.
5. **SOR Task Auto-Creation**  — creates diary notes / task assignments in Claims SOR for
                                   every actionable document; zero manual keying.
6. **Alert Dispatch**           — priority alerts for time-sensitive documents:
                                     ATTORNEY_LETTER  → BI adjuster < 1 h
                                     COURT_NOTICE     → legal team < 30 min
                                     STATUTE_NOTICE   → adjuster < 1 h
                                     ROR_DEADLINE     → coverage counsel < 2 h
7. **Missing Document Detection** — compares received set against required checklist for
                                   claim type + coverage path; emits gap list.
8. **Pipeline Integration**    — emits StageResult / DecisionRecord compatible with the
                                   existing run_pipeline() orchestrator; can be embedded as
                                   a workflow stage or called independently via API.

Decision Rules (per Blueprint V3 §Stage 01-A)
----------------------------------------------
  ATTORNEY_LETTER → litigationIndicator=true; BI adjuster priority alert ≤1h
  COURT_NOTICE / LAWSUIT_SUMMONS → CRITICAL; legal team ≤30min; answer-deadline tracked
  HIPAA_RELEASE + injuryReported → medical records request; BI Evaluation queued
  qualityScore < 0.60 → re-submission request dispatched; adjuster follow-up task ≤48h
  ESTIMATE from DRP_PARTNER → auto-route to Damage Estimation Agent; no manual keying

SLA: < 90 seconds end-to-end (93% automation rate — per Blueprint V3)

Public API
----------
  classify_document(payload) -> DocumentRecord
  process_claim_documents(claim_id, documents, claim_context) -> DocAssistResult
  get_documents_for_claim(claim_id) -> List[DocumentRecord]
  get_missing_documents(claim_id, claim_type, coverage_types) -> MissingDocResult
  list_alerts(claim_id) -> List[AlertRecord]
  health() -> Dict
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fnol_llm_adapter import complete as llm_complete, resolve_provider
from fnol_runtime import redact_text
from fnol_state_backend import make_store, StateBackend
from fnol_settings import settings

log = logging.getLogger("fnol.doc_assist")

AGENT_ID      = "S1-A"
AGENT_NAME    = "Document Assist & Intelligent Classification"
AGENT_VERSION = "1.0.0"
STAGE_SLA_SEC = 90
AUTOMATION_RATE = 0.93


# ───────────────────────────────────────────────────────────────────────────
# Enumerations & Constants
# ───────────────────────────────────────────────────────────────────────────

class DocumentType:
    POLICE_REPORT         = "POLICE_REPORT"
    ESTIMATE              = "ESTIMATE"
    MEDICAL_RECORD        = "MEDICAL_RECORD"
    ATTORNEY_LETTER       = "ATTORNEY_LETTER"
    ATTORNEY_REPRESENTATION = "ATTORNEY_REPRESENTATION"
    HIPAA_RELEASE         = "HIPAA_RELEASE"
    PHOTO                 = "PHOTO"
    VIDEO                 = "VIDEO"
    COURT_NOTICE          = "COURT_NOTICE"
    LAWSUIT_SUMMONS       = "LAWSUIT_SUMMONS"
    ROR_LETTER            = "ROR_LETTER"
    STATUTE_NOTICE        = "STATUTE_NOTICE"
    SUBROGATION_DEMAND    = "SUBROGATION_DEMAND"
    RENTAL_AGREEMENT      = "RENTAL_AGREEMENT"
    TOW_INVOICE           = "TOW_INVOICE"
    COVERAGE_DECLARATION  = "COVERAGE_DECLARATION"
    RECORDED_STATEMENT    = "RECORDED_STATEMENT"
    OTHER                 = "OTHER"

    ALL = [
        POLICE_REPORT, ESTIMATE, MEDICAL_RECORD, ATTORNEY_LETTER,
        ATTORNEY_REPRESENTATION, HIPAA_RELEASE, PHOTO, VIDEO,
        COURT_NOTICE, LAWSUIT_SUMMONS, ROR_LETTER, STATUTE_NOTICE,
        SUBROGATION_DEMAND, RENTAL_AGREEMENT, TOW_INVOICE,
        COVERAGE_DECLARATION, RECORDED_STATEMENT, OTHER,
    ]

class AlertSeverity:
    CRITICAL = "CRITICAL"   # < 30 min — legal team
    HIGH     = "HIGH"       # < 1 h   — BI adjuster
    MEDIUM   = "MEDIUM"     # < 2 h   — coverage counsel
    LOW      = "LOW"        # < 24 h  — file adjuster

class TaskType:
    REVIEW_DOCUMENT       = "REVIEW_DOCUMENT"
    REQUEST_RESUBMISSION  = "REQUEST_RESUBMISSION"
    REQUEST_MISSING_DOC   = "REQUEST_MISSING_DOC"
    ROUTE_TO_AGENT        = "ROUTE_TO_AGENT"
    LEGAL_ESCALATION      = "LEGAL_ESCALATION"
    MEDICAL_AUTH_REQUEST  = "MEDICAL_AUTH_REQUEST"
    ASSIGN_BI_ADJUSTER    = "ASSIGN_BI_ADJUSTER"

# Required document checklist by claim type / coverage path
REQUIRED_DOCS: Dict[str, List[str]] = {
    "COLLISION": [
        DocumentType.POLICE_REPORT,
        DocumentType.PHOTO,
        DocumentType.ESTIMATE,
    ],
    "COLLISION_INJURY": [
        DocumentType.POLICE_REPORT,
        DocumentType.PHOTO,
        DocumentType.ESTIMATE,
        DocumentType.MEDICAL_RECORD,
        DocumentType.RECORDED_STATEMENT,
    ],
    "COMPREHENSIVE": [
        DocumentType.POLICE_REPORT,
        DocumentType.PHOTO,
    ],
    "PROPERTY_DAMAGE": [
        DocumentType.PHOTO,
        DocumentType.ESTIMATE,
    ],
    "BI": [
        DocumentType.POLICE_REPORT,
        DocumentType.MEDICAL_RECORD,
        DocumentType.HIPAA_RELEASE,
        DocumentType.RECORDED_STATEMENT,
    ],
    "TOTAL_LOSS": [
        DocumentType.POLICE_REPORT,
        DocumentType.PHOTO,
        DocumentType.COVERAGE_DECLARATION,
    ],
    "DEFAULT": [
        DocumentType.PHOTO,
    ],
}

# Alert routing rules: document_type → (severity, audience, deadline_minutes)
ALERT_RULES: Dict[str, Tuple[str, str, int]] = {
    DocumentType.ATTORNEY_LETTER:         (AlertSeverity.HIGH,     "BI_ADJUSTER",    60),
    DocumentType.ATTORNEY_REPRESENTATION: (AlertSeverity.HIGH,     "BI_ADJUSTER",    60),
    DocumentType.COURT_NOTICE:            (AlertSeverity.CRITICAL, "LEGAL_TEAM",     30),
    DocumentType.LAWSUIT_SUMMONS:         (AlertSeverity.CRITICAL, "LEGAL_TEAM",     30),
    DocumentType.STATUTE_NOTICE:          (AlertSeverity.HIGH,     "ADJUSTER",       60),
    DocumentType.ROR_LETTER:              (AlertSeverity.MEDIUM,   "COVERAGE_COUNSEL", 120),
    DocumentType.SUBROGATION_DEMAND:      (AlertSeverity.MEDIUM,   "SUBRO_ADJUSTER", 120),
}


# ───────────────────────────────────────────────────────────────────────────
# Data Models
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class ExtractedData:
    """Structured fields extracted from a document via OCR + LLM."""
    parties:          List[str]          = field(default_factory=list)
    dates:            List[str]          = field(default_factory=list)
    amounts:          List[float]        = field(default_factory=list)
    vins:             List[str]          = field(default_factory=list)
    icd_codes:        List[str]          = field(default_factory=list)
    policy_numbers:   List[str]          = field(default_factory=list)
    claim_numbers:    List[str]          = field(default_factory=list)
    jurisdiction:     Optional[str]      = None
    incident_date:    Optional[str]      = None
    officer_name:     Optional[str]      = None
    report_number:    Optional[str]      = None
    attorney_name:    Optional[str]      = None
    law_firm:         Optional[str]      = None
    repair_total:     Optional[float]    = None
    shop_name:        Optional[str]      = None
    diagnosis:        Optional[str]      = None
    raw_summary:      Optional[str]      = None
    extraction_model: str                = ""
    extraction_confidence: float         = 0.0


@dataclass
class SORTask:
    """A task/diary note created in the Claims System of Record."""
    task_id:        str
    claim_id:       str
    document_id:    str
    task_type:      str
    priority:       str
    description:    str
    assigned_to:    str
    due_in_hours:   int
    created_at:     str
    sor_reference:  Optional[str] = None   # Duck Creek / Guidewire task ID (mocked)


@dataclass
class AlertRecord:
    """An adjuster/legal alert triggered by a time-sensitive document."""
    alert_id:       str
    claim_id:       str
    document_id:    str
    document_type:  str
    severity:       str
    audience:       str
    deadline_minutes: int
    message:        str
    dispatched_at:  str
    acknowledged:   bool = False
    acknowledged_at: Optional[str] = None


@dataclass
class DocumentRecord:
    """Canonical record for a single ingested document."""
    document_id:        str
    claim_id:           str
    file_name:          str
    file_type:          str           # pdf | jpg | png | mp4 | txt | …
    file_size_bytes:    int
    content_hash:       str           # SHA-256 of file bytes (dedup key)
    source_channel:     str           # MOBILE | EMAIL | FAX | API | WEB
    uploaded_at:        str
    document_type:      str           # DocumentType enum value
    classification_confidence: float
    quality_score:      float
    quality_issues:     List[str]
    extracted_data:     ExtractedData
    tasks_created:      List[str]     # SORTask.task_id[]
    alerts_dispatched:  List[str]     # AlertRecord.alert_id[]
    litigation_flag:    bool
    requires_resubmission: bool
    routing_action:     Optional[str] = None
    processing_ms:      int = 0
    agent_version:      str = AGENT_VERSION


@dataclass
class MissingDocResult:
    """Missing required documents for a claim."""
    claim_id:       str
    claim_type:     str
    coverage_types: List[str]
    required:       List[str]
    received:       List[str]
    missing:        List[str]
    request_triggers: List[Dict[str, Any]]
    evaluated_at:   str


@dataclass
class DocAssistResult:
    """Aggregate result for all documents processed in one S1-A run."""
    claim_id:           str
    stage_id:           str = AGENT_ID
    stage_name:         str = AGENT_NAME
    agent_version:      str = AGENT_VERSION
    status:             str = "ok"        # ok | warning | hitl | hold | error
    processed_count:    int = 0
    document_ids:       List[str]         = field(default_factory=list)
    documents:          List[DocumentRecord] = field(default_factory=list)
    tasks_created:      List[SORTask]     = field(default_factory=list)
    alerts_dispatched:  List[AlertRecord] = field(default_factory=list)
    missing_docs:       Optional[MissingDocResult] = None
    litigation_flag:    bool = False
    advisories:         List[str]         = field(default_factory=list)
    started_at:         str = ""
    completed_at:       str = ""
    duration_ms:        int = 0
    automation_rate:    float = AUTOMATION_RATE
    sla_met:            bool = True
    error:              Optional[str] = None


# ───────────────────────────────────────────────────────────────────────────
# In-Memory Stores (POC — swap for Redis / event store in production)
# ───────────────────────────────────────────────────────────────────────────

_DOC_STORE:   StateBackend = make_store("doc_store",  max_size=4096, ttl_seconds=86400)
_TASK_STORE:  StateBackend = make_store("doc_tasks",  max_size=4096, ttl_seconds=86400)
_ALERT_STORE: StateBackend = make_store("doc_alerts", max_size=4096, ttl_seconds=86400)

# Per-claim document index: claim_id → List[document_id]
_CLAIM_DOC_INDEX: Dict[str, List[str]] = {}
_CLAIM_ALERT_INDEX: Dict[str, List[str]] = {}


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _short_id() -> str:
    return str(uuid.uuid4())[:8].upper()

def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"

def _detect_file_type(file_name: str, mime_type: Optional[str]) -> str:
    ext = (file_name or "").rsplit(".", 1)[-1].lower()
    type_map = {
        "pdf": "pdf", "jpg": "jpg", "jpeg": "jpg", "png": "png",
        "mp4": "mp4", "mov": "mov", "avi": "avi",
        "txt": "txt", "doc": "doc", "docx": "docx", "tiff": "tiff",
    }
    if ext in type_map:
        return type_map[ext]
    if mime_type:
        m = mime_type.lower()
        if "pdf" in m:      return "pdf"
        if "image" in m:    return "jpg"
        if "video" in m:    return "mp4"
        if "text" in m:     return "txt"
    return "unknown"


# ───────────────────────────────────────────────────────────────────────────
# LLM — Document Classification
# ───────────────────────────────────────────────────────────────────────────

_CLASSIFY_SYSTEM = """You are the FNOL Document Classification Agent (S1-A) for a P&C auto insurance carrier.
Classify the document described and extract key metadata.

Respond ONLY with valid JSON — no markdown fences, no preamble.

Schema:
{
  "document_type": "<one of the enum values below>",
  "confidence": <float 0.0–1.0>,
  "quality_score": <float 0.0–1.0>,
  "quality_issues": [<string>, ...],
  "litigation_flag": <bool>,
  "summary": "<1-2 sentence summary>",
  "extracted": {
    "parties": [<string>, ...],
    "dates": [<string YYYY-MM-DD>, ...],
    "amounts": [<float>, ...],
    "vins": [<string>, ...],
    "icd_codes": [<string>, ...],
    "policy_numbers": [<string>, ...],
    "claim_numbers": [<string>, ...],
    "jurisdiction": "<string or null>",
    "incident_date": "<YYYY-MM-DD or null>",
    "officer_name": "<string or null>",
    "report_number": "<string or null>",
    "attorney_name": "<string or null>",
    "law_firm": "<string or null>",
    "repair_total": <float or null>,
    "shop_name": "<string or null>",
    "diagnosis": "<string or null>"
  }
}

Document type enum: POLICE_REPORT | ESTIMATE | MEDICAL_RECORD | ATTORNEY_LETTER |
ATTORNEY_REPRESENTATION | HIPAA_RELEASE | PHOTO | VIDEO | COURT_NOTICE | LAWSUIT_SUMMONS |
ROR_LETTER | STATUTE_NOTICE | SUBROGATION_DEMAND | RENTAL_AGREEMENT | TOW_INVOICE |
COVERAGE_DECLARATION | RECORDED_STATEMENT | OTHER

Quality scoring: 1.0=excellent/fully-legible. Penalize for: blurry/dark photos, missing pages,
partial text, poor scan resolution, missing required fields, illegible signatures.
quality_issues examples: ["blurry image", "missing page 2", "signature illegible", "dark exposure"]

litigation_flag: true if document indicates or anticipates legal action (attorney letter,
court notice, lawsuit, demand letter).
"""

def _classify_via_llm(
    file_name: str,
    file_type: str,
    content_snippet: str,
    file_size_bytes: int,
    source_channel: str,
    claim_context: Dict[str, Any],
) -> Dict[str, Any]:
    """Call LLM to classify a document and extract structured data."""
    ctx = json.dumps({
        "claim_type":    claim_context.get("claim_type", "COLLISION"),
        "coverage_types": claim_context.get("coverage_types", []),
        "injury_reported": claim_context.get("injury_reported", False),
        "source_channel": source_channel,
    })

    user_msg = (
        f"Document to classify:\n"
        f"  file_name: {file_name}\n"
        f"  file_type: {file_type}\n"
        f"  file_size_bytes: {file_size_bytes}\n"
        f"  source_channel: {source_channel}\n"
        f"  content_snippet (first 800 chars, OCR/text):\n{content_snippet[:800]}\n\n"
        f"Claim context: {ctx}\n\n"
        f"Classify and extract. Return JSON only."
    )

    result = llm_complete(
        system=_CLASSIFY_SYSTEM,
        user=user_msg,
        max_tokens=1200,
    )

    if not result.ok and not result.text:
        return _fallback_classification(file_name, file_type, content_snippet)

    try:
        text = result.text.strip()
        # Strip markdown fences if present despite instruction
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        log.warning("S1-A: LLM returned non-JSON; using fallback classifier")
        return _fallback_classification(file_name, file_type, content_snippet)


def _fallback_classification(file_name: str, file_type: str, content: str) -> Dict[str, Any]:
    """Deterministic rule-based fallback when LLM is unavailable."""
    name_lower = (file_name or "").lower()
    content_lower = (content or "").lower()

    doc_type = DocumentType.OTHER
    confidence = 0.70
    quality = 0.80
    litigation = False

    # File-type hints
    if file_type in ("jpg", "png", "tiff"):
        doc_type = DocumentType.PHOTO
        confidence = 0.85
    elif file_type in ("mp4", "mov", "avi"):
        doc_type = DocumentType.VIDEO
        confidence = 0.90

    # Name hints
    if any(k in name_lower for k in ("police", "incident_report", "crash_report", "crash report")):
        doc_type = DocumentType.POLICE_REPORT; confidence = 0.92
    elif any(k in name_lower for k in ("estimate", "repair_estimate", "supplement")):
        doc_type = DocumentType.ESTIMATE; confidence = 0.90
    elif any(k in name_lower for k in ("medical", "hospital", "eob", "bill")):
        doc_type = DocumentType.MEDICAL_RECORD; confidence = 0.88
    elif any(k in name_lower for k in ("attorney", "counsel", "law_firm", "representation")):
        doc_type = DocumentType.ATTORNEY_LETTER; litigation = True; confidence = 0.91
    elif any(k in name_lower for k in ("hipaa", "authorization", "release")):
        doc_type = DocumentType.HIPAA_RELEASE; confidence = 0.87
    elif any(k in name_lower for k in ("summons", "lawsuit", "complaint")):
        doc_type = DocumentType.LAWSUIT_SUMMONS; litigation = True; confidence = 0.93
    elif any(k in name_lower for k in ("court_notice", "court notice", "legal_notice")):
        doc_type = DocumentType.COURT_NOTICE; litigation = True; confidence = 0.89
    elif any(k in name_lower for k in ("ror", "reservation_of_rights")):
        doc_type = DocumentType.ROR_LETTER; confidence = 0.88

    # Content hints override
    for kw, dt in [
        ("police report", DocumentType.POLICE_REPORT),
        ("collision report", DocumentType.POLICE_REPORT),
        ("repair estimate", DocumentType.ESTIMATE),
        ("dear counsel", DocumentType.ATTORNEY_LETTER),
        ("our client", DocumentType.ATTORNEY_LETTER),
        ("hipaa authorization", DocumentType.HIPAA_RELEASE),
        ("you are hereby summoned", DocumentType.LAWSUIT_SUMMONS),
        ("reservation of rights", DocumentType.ROR_LETTER),
    ]:
        if kw in content_lower:
            doc_type = dt
            confidence = max(confidence, 0.85)
            if dt in (DocumentType.ATTORNEY_LETTER, DocumentType.LAWSUIT_SUMMONS,
                      DocumentType.COURT_NOTICE, DocumentType.ROR_LETTER):
                litigation = True
            break

    # Quality hints
    quality_issues = []
    if file_type in ("jpg", "png") and file_size_bytes < 50_000:
        quality -= 0.25
        quality_issues.append("Low resolution / small file")
    if not content or len(content) < 50:
        quality -= 0.20
        quality_issues.append("Minimal extractable text")

    quality = max(0.0, min(1.0, quality))

    return {
        "document_type": doc_type,
        "confidence": confidence,
        "quality_score": quality,
        "quality_issues": quality_issues,
        "litigation_flag": litigation,
        "summary": f"Auto-classified as {doc_type} based on filename/content heuristics.",
        "extracted": {
            "parties": [], "dates": [], "amounts": [], "vins": [],
            "icd_codes": [], "policy_numbers": [], "claim_numbers": [],
            "jurisdiction": None, "incident_date": None,
            "officer_name": None, "report_number": None,
            "attorney_name": None, "law_firm": None,
            "repair_total": None, "shop_name": None, "diagnosis": None,
        },
    }


# ───────────────────────────────────────────────────────────────────────────
# SOR Task Builder
# ───────────────────────────────────────────────────────────────────────────

def _create_sor_tasks(
    doc: DocumentRecord,
    claim_context: Dict[str, Any],
) -> List[SORTask]:
    """Generate SOR diary notes / tasks based on document type and attributes."""
    tasks: List[SORTask] = []
    now = _utcnow()
    claim_id = doc.claim_id
    doc_id   = doc.document_id
    doc_type = doc.document_type

    def _task(task_type: str, priority: str, description: str, assigned_to: str, due_hours: int) -> SORTask:
        t = SORTask(
            task_id    = _new_id("TASK"),
            claim_id   = claim_id,
            document_id = doc_id,
            task_type  = task_type,
            priority   = priority,
            description = description,
            assigned_to = assigned_to,
            due_in_hours = due_hours,
            created_at = now,
            sor_reference = f"DC-{_short_id()}",   # Mock Duck Creek task ref
        )
        return t

    # Every document gets a review task
    tasks.append(_task(
        TaskType.REVIEW_DOCUMENT, "NORMAL",
        f"Review and verify incoming {doc_type.replace('_', ' ').title()}: {doc.file_name}",
        "FILE_ADJUSTER", 24,
    ))

    # Quality re-submission
    if doc.requires_resubmission:
        tasks.append(_task(
            TaskType.REQUEST_RESUBMISSION, "HIGH",
            f"Document quality score {doc.quality_score:.2f} < 0.60 — request re-submission from claimant within 48h. Issues: {', '.join(doc.quality_issues or ['poor quality'])}",
            "FILE_ADJUSTER", 48,
        ))

    # Type-specific tasks
    if doc_type == DocumentType.ATTORNEY_LETTER:
        tasks.append(_task(
            TaskType.ASSIGN_BI_ADJUSTER, "CRITICAL",
            "ATTORNEY LETTER received — assign to BI adjuster immediately. Litigation indicator set to TRUE. Claim moves to human negotiation track. AI advisory mode only.",
            "BI_ADJUSTER", 1,
        ))

    elif doc_type == DocumentType.ATTORNEY_REPRESENTATION:
        tasks.append(_task(
            TaskType.ASSIGN_BI_ADJUSTER, "CRITICAL",
            f"Attorney representation letter received from {doc.extracted_data.law_firm or 'unknown firm'}. BI track assignment required. All future correspondence through counsel.",
            "BI_ADJUSTER", 1,
        ))

    elif doc_type in (DocumentType.COURT_NOTICE, DocumentType.LAWSUIT_SUMMONS):
        tasks.append(_task(
            TaskType.LEGAL_ESCALATION, "CRITICAL",
            f"LEGAL: {doc_type.replace('_', ' ')} received. Answer deadline must be tracked. Assign defense counsel. File transferred to litigation unit.",
            "LEGAL_TEAM", 1,
        ))

    elif doc_type == DocumentType.HIPAA_RELEASE:
        injury = claim_context.get("injury_reported", False)
        if injury:
            tasks.append(_task(
                TaskType.MEDICAL_AUTH_REQUEST, "HIGH",
                "HIPAA release received and injury reported — trigger medical records request per auth. Queue BI Evaluation Agent (A5). Log medical auth timestamp for statute tracking.",
                "BI_ADJUSTER", 4,
            ))

    elif doc_type == DocumentType.ESTIMATE:
        channel = claim_context.get("source_channel", "")
        if channel == "DRP_PARTNER":
            tasks.append(_task(
                TaskType.ROUTE_TO_AGENT, "NORMAL",
                "DRP Partner estimate — auto-routing to Damage Estimation Agent (S4B) for cross-validation. No manual keying required.",
                "DAMAGE_AGENT", 2,
            ))

    elif doc_type == DocumentType.ROR_LETTER:
        tasks.append(_task(
            TaskType.REVIEW_DOCUMENT, "HIGH",
            "Reservation of Rights letter received — route to coverage counsel. ROR deadline tracking initiated.",
            "COVERAGE_COUNSEL", 2,
        ))

    elif doc_type == DocumentType.SUBROGATION_DEMAND:
        tasks.append(_task(
            TaskType.REVIEW_DOCUMENT, "MEDIUM",
            "Subrogation demand received — route to Subrogation Adjuster (A7). Response deadline tracking initiated.",
            "SUBRO_ADJUSTER", 4,
        ))

    return tasks


# ───────────────────────────────────────────────────────────────────────────
# Alert Builder
# ───────────────────────────────────────────────────────────────────────────

def _dispatch_alerts(
    doc: DocumentRecord,
    claim_context: Dict[str, Any],
) -> List[AlertRecord]:
    """Generate priority alerts for time-sensitive documents."""
    alerts: List[AlertRecord] = []
    rule = ALERT_RULES.get(doc.document_type)
    if not rule:
        return alerts

    severity, audience, deadline_min = rule
    now = _utcnow()
    claim_id = doc.claim_id

    msg_map = {
        DocumentType.ATTORNEY_LETTER:
            f"⚠ ATTORNEY LETTER received for claim {claim_id}. Litigation indicator set TRUE. BI adjuster assignment required within {deadline_min} min. All AI recommendations advisory only.",
        DocumentType.ATTORNEY_REPRESENTATION:
            f"⚠ ATTORNEY REPRESENTATION received for claim {claim_id} from {doc.extracted_data.law_firm or 'counsel'}. All future communication through attorney. BI adjuster must acknowledge within {deadline_min} min.",
        DocumentType.COURT_NOTICE:
            f"🚨 COURT NOTICE / LEGAL ACTION — Claim {claim_id}. Answer deadline at risk. Legal team must acknowledge within {deadline_min} min.",
        DocumentType.LAWSUIT_SUMMONS:
            f"🚨 LAWSUIT SUMMONS received — Claim {claim_id}. Immediate defense counsel assignment required. Answer deadline tracking started. Acknowledge within {deadline_min} min.",
        DocumentType.STATUTE_NOTICE:
            f"⚠ STATUTE NOTICE — Claim {claim_id}. Response deadline tracking required. Adjuster must review within {deadline_min} min.",
        DocumentType.ROR_LETTER:
            f"⚠ RESERVATION OF RIGHTS letter — Claim {claim_id}. Coverage counsel review required within {deadline_min} min.",
        DocumentType.SUBROGATION_DEMAND:
            f"ℹ SUBROGATION DEMAND — Claim {claim_id}. Route to Subrogation Adjuster within {deadline_min} min.",
    }

    message = msg_map.get(doc.document_type, f"Time-sensitive document received: {doc.document_type}")

    alert = AlertRecord(
        alert_id        = _new_id("ALERT"),
        claim_id        = claim_id,
        document_id     = doc.document_id,
        document_type   = doc.document_type,
        severity        = severity,
        audience        = audience,
        deadline_minutes = deadline_min,
        message         = message,
        dispatched_at   = now,
    )
    alerts.append(alert)
    return alerts


# ───────────────────────────────────────────────────────────────────────────
# Core Classification Pipeline
# ───────────────────────────────────────────────────────────────────────────

def classify_document(
    claim_id:        str,
    file_name:       str,
    file_bytes:      Optional[bytes],
    file_b64:        Optional[str],
    file_size_bytes: int,
    source_channel:  str,
    mime_type:       Optional[str],
    content_text:    Optional[str],
    claim_context:   Dict[str, Any],
) -> DocumentRecord:
    """
    Classify a single document. Main entry point for single-doc classification.

    Either file_bytes or file_b64 must be provided; content_text is an
    optional OCR pre-extract or the raw text body of a text/PDF document.
    """
    import time as _time
    t0 = _time.time()

    # 1. Resolve bytes
    raw: bytes = b""
    if file_bytes:
        raw = file_bytes
    elif file_b64:
        try:
            raw = base64.b64decode(file_b64)
        except Exception:
            raw = b""
    if not raw and content_text:
        raw = content_text.encode("utf-8", errors="replace")

    content_hash = _sha256(raw) if raw else _sha256(file_name.encode())

    # 2. Dedup check — skip re-classification if same content already seen
    for existing_id in _CLAIM_DOC_INDEX.get(claim_id, []):
        existing = _DOC_STORE.get(existing_id)
        if existing and isinstance(existing, dict) and existing.get("content_hash") == content_hash:
            log.info("S1-A: Duplicate document detected for claim %s — returning existing record", claim_id)
            # Re-hydrate from stored dict (BoundedStore stores serialized dicts)
            return _dict_to_doc(existing)

    # 3. Infer file type
    file_type = _detect_file_type(file_name, mime_type)

    # 4. Build content snippet for LLM (text extract or partial base64 hint)
    snippet = (content_text or "")[:1200]
    if not snippet and file_type in ("pdf", "doc", "docx", "txt"):
        snippet = "[No OCR text provided — classification based on filename and metadata]"
    elif not snippet:
        snippet = f"[Binary {file_type} file — classification based on filename and metadata]"

    # 5. LLM classification
    doc_id = _new_id("DOC")
    now = _utcnow()

    clf = _classify_via_llm(
        file_name    = file_name,
        file_type    = file_type,
        content_snippet = snippet,
        file_size_bytes = file_size_bytes,
        source_channel = source_channel,
        claim_context  = claim_context,
    )

    doc_type   = clf.get("document_type", DocumentType.OTHER)
    confidence = float(clf.get("confidence", 0.70))
    quality    = float(clf.get("quality_score", 0.80))
    quality_issues = clf.get("quality_issues", [])
    litigation_flag = bool(clf.get("litigation_flag", False))
    summary    = clf.get("summary", "")

    ex = clf.get("extracted", {})
    extracted = ExtractedData(
        parties          = ex.get("parties", []),
        dates            = ex.get("dates", []),
        amounts          = [float(a) for a in ex.get("amounts", []) if a is not None],
        vins             = ex.get("vins", []),
        icd_codes        = ex.get("icd_codes", []),
        policy_numbers   = ex.get("policy_numbers", []),
        claim_numbers    = ex.get("claim_numbers", []),
        jurisdiction     = ex.get("jurisdiction"),
        incident_date    = ex.get("incident_date"),
        officer_name     = ex.get("officer_name"),
        report_number    = ex.get("report_number"),
        attorney_name    = ex.get("attorney_name"),
        law_firm         = ex.get("law_firm"),
        repair_total     = float(ex["repair_total"]) if ex.get("repair_total") is not None else None,
        shop_name        = ex.get("shop_name"),
        diagnosis        = ex.get("diagnosis"),
        raw_summary      = summary,
        extraction_model = resolve_provider(),
        extraction_confidence = confidence,
    )

    requires_resubmission = quality < 0.60

    # 6. Determine routing action
    routing_action: Optional[str] = None
    if doc_type == DocumentType.ESTIMATE and source_channel == "DRP_PARTNER":
        routing_action = "ROUTE_TO_DAMAGE_ESTIMATION_AGENT"
    elif doc_type == DocumentType.HIPAA_RELEASE and claim_context.get("injury_reported"):
        routing_action = "QUEUE_BI_EVALUATION_AGENT"
    elif litigation_flag:
        routing_action = "ROUTE_TO_BI_ADJUSTER"

    # 7. Build DocumentRecord (without tasks/alerts yet)
    doc = DocumentRecord(
        document_id           = doc_id,
        claim_id              = claim_id,
        file_name             = file_name,
        file_type             = file_type,
        file_size_bytes       = file_size_bytes or len(raw),
        content_hash          = content_hash,
        source_channel        = source_channel,
        uploaded_at           = now,
        document_type         = doc_type,
        classification_confidence = confidence,
        quality_score         = quality,
        quality_issues        = quality_issues,
        extracted_data        = extracted,
        tasks_created         = [],
        alerts_dispatched     = [],
        litigation_flag       = litigation_flag,
        requires_resubmission = requires_resubmission,
        routing_action        = routing_action,
        processing_ms         = int((_time.time() - t0) * 1000),
        agent_version         = AGENT_VERSION,
    )

    # 8. Create SOR tasks
    tasks = _create_sor_tasks(doc, claim_context)
    for t in tasks:
        _TASK_STORE.set(t.task_id, asdict(t))
    doc.tasks_created = [t.task_id for t in tasks]

    # 9. Dispatch alerts
    alerts = _dispatch_alerts(doc, claim_context)
    for a in alerts:
        _ALERT_STORE.set(a.alert_id, asdict(a))
        _CLAIM_ALERT_INDEX.setdefault(claim_id, []).append(a.alert_id)
    doc.alerts_dispatched = [a.alert_id for a in alerts]

    # 10. Persist
    _DOC_STORE.set(doc_id, _doc_to_dict(doc))
    _CLAIM_DOC_INDEX.setdefault(claim_id, []).append(doc_id)

    elapsed = int((_time.time() - t0) * 1000)
    doc.processing_ms = elapsed
    _DOC_STORE.set(doc_id, _doc_to_dict(doc))

    log.info(
        "S1-A: Classified %s → %s (confidence=%.2f, quality=%.2f, litigation=%s) in %dms",
        file_name, doc_type, confidence, quality, litigation_flag, elapsed,
    )
    return doc


# ───────────────────────────────────────────────────────────────────────────
# Batch Processing (called from workflow engine)
# ───────────────────────────────────────────────────────────────────────────

def process_claim_documents(
    claim_id:      str,
    documents:     List[Dict[str, Any]],
    claim_context: Dict[str, Any],
) -> DocAssistResult:
    """
    Process a batch of documents for a single claim.
    Called by the workflow engine after Stage 01 (FNOL Capture).

    Each `documents` entry should contain:
      file_name, file_b64 (optional), file_size_bytes, source_channel,
      mime_type (optional), content_text (optional)
    """
    import time as _time
    t0 = _time.time()
    now = _utcnow()

    result = DocAssistResult(
        claim_id   = claim_id,
        started_at = now,
    )

    all_tasks:  List[SORTask]    = []
    all_alerts: List[AlertRecord] = []
    processed:  List[DocumentRecord] = []

    for doc_spec in (documents or []):
        try:
            doc = classify_document(
                claim_id        = claim_id,
                file_name       = doc_spec.get("file_name", "unknown.pdf"),
                file_bytes      = None,
                file_b64        = doc_spec.get("file_b64"),
                file_size_bytes = doc_spec.get("file_size_bytes", 0),
                source_channel  = doc_spec.get("source_channel", "WEB"),
                mime_type       = doc_spec.get("mime_type"),
                content_text    = doc_spec.get("content_text"),
                claim_context   = claim_context,
            )
            processed.append(doc)

            # Collect tasks
            for tid in doc.tasks_created:
                t_dict = _TASK_STORE.get(tid)
                if t_dict:
                    all_tasks.append(_dict_to_task(t_dict))

            # Collect alerts
            for aid in doc.alerts_dispatched:
                a_dict = _ALERT_STORE.get(aid)
                if a_dict:
                    all_alerts.append(_dict_to_alert(a_dict))

            if doc.litigation_flag:
                result.litigation_flag = True

        except Exception as exc:
            log.exception("S1-A: Error classifying document %s: %s",
                          doc_spec.get("file_name"), exc)
            result.advisories.append(f"Classification error for {doc_spec.get('file_name')}: {exc}")

    # Missing doc check
    claim_type     = claim_context.get("claim_type", "DEFAULT")
    coverage_types = claim_context.get("coverage_types", [])
    result.missing_docs = _check_missing_documents(claim_id, claim_type, coverage_types, processed)

    # Aggregate advisories
    for doc in processed:
        if doc.requires_resubmission:
            result.advisories.append(
                f"Re-submission required: {doc.file_name} (quality={doc.quality_score:.2f})"
            )
        if doc.routing_action:
            result.advisories.append(f"Routing: {doc.file_name} → {doc.routing_action}")

    if result.litigation_flag:
        result.advisories.insert(0, "⚠ LITIGATION FLAG SET — One or more documents indicate legal representation or legal action.")
        result.status = "hitl"
    elif result.missing_docs and result.missing_docs.missing:
        result.status = "warning"

    elapsed_ms = int((_time.time() - t0) * 1000)
    result.processed_count   = len(processed)
    result.document_ids      = [d.document_id for d in processed]
    result.documents         = processed
    result.tasks_created     = all_tasks
    result.alerts_dispatched = all_alerts
    result.completed_at      = _utcnow()
    result.duration_ms       = elapsed_ms
    result.sla_met           = elapsed_ms <= (STAGE_SLA_SEC * 1000)

    return result


# ───────────────────────────────────────────────────────────────────────────
# Missing Document Detection
# ───────────────────────────────────────────────────────────────────────────

def _check_missing_documents(
    claim_id:       str,
    claim_type:     str,
    coverage_types: List[str],
    received_docs:  List[DocumentRecord],
) -> MissingDocResult:
    """Compare received document types against required checklist."""
    # Build the required set from claim_type + coverage_types
    required_set = set(REQUIRED_DOCS.get(claim_type, REQUIRED_DOCS["DEFAULT"]))
    for cov in (coverage_types or []):
        required_set.update(REQUIRED_DOCS.get(cov, []))

    received_types = {d.document_type for d in received_docs}
    missing = sorted(required_set - received_types)

    request_triggers = []
    for m in missing:
        request_triggers.append({
            "missing_type": m,
            "request_method": "PREFERRED_CHANNEL",
            "window_hours": 48,
            "task_created": True,
            "description": f"Request {m.replace('_', ' ').title()} from claimant within 48 hours",
        })

    return MissingDocResult(
        claim_id        = claim_id,
        claim_type      = claim_type,
        coverage_types  = coverage_types,
        required        = sorted(required_set),
        received        = sorted(received_types),
        missing         = missing,
        request_triggers = request_triggers,
        evaluated_at    = _utcnow(),
    )


# ───────────────────────────────────────────────────────────────────────────
# Public Retrieval API
# ───────────────────────────────────────────────────────────────────────────

def get_documents_for_claim(claim_id: str) -> List[Dict[str, Any]]:
    """Return all document records for a claim (serialized dicts)."""
    doc_ids = _CLAIM_DOC_INDEX.get(claim_id, [])
    out = []
    for did in doc_ids:
        d = _DOC_STORE.get(did)
        if d:
            out.append(d)
    return out


def get_document(document_id: str) -> Optional[Dict[str, Any]]:
    return _DOC_STORE.get(document_id)


def list_alerts(claim_id: str) -> List[Dict[str, Any]]:
    """Return all alerts for a claim."""
    alert_ids = _CLAIM_ALERT_INDEX.get(claim_id, [])
    out = []
    for aid in alert_ids:
        a = _ALERT_STORE.get(aid)
        if a:
            out.append(a)
    return out


def acknowledge_alert(alert_id: str) -> Optional[Dict[str, Any]]:
    a = _ALERT_STORE.get(alert_id)
    if not a:
        return None
    a["acknowledged"] = True
    a["acknowledged_at"] = _utcnow()
    _ALERT_STORE.set(alert_id, a)
    return a


def get_missing_documents(claim_id: str, claim_type: str, coverage_types: List[str]) -> MissingDocResult:
    received_docs_dicts = get_documents_for_claim(claim_id)
    received_docs = [_dict_to_doc(d) for d in received_docs_dicts]
    return _check_missing_documents(claim_id, claim_type, coverage_types, received_docs)


def health() -> Dict[str, Any]:
    return {
        "agent": AGENT_ID,
        "name": AGENT_NAME,
        "version": AGENT_VERSION,
        "status": "ok",
        "documents_indexed": sum(len(v) for v in _CLAIM_DOC_INDEX.values()),
        "active_alerts": sum(len(v) for v in _CLAIM_ALERT_INDEX.values()),
        "llm_provider": resolve_provider(),
        "sla_sec": STAGE_SLA_SEC,
        "automation_rate": AUTOMATION_RATE,
    }


# ───────────────────────────────────────────────────────────────────────────
# Serialization Helpers
# ───────────────────────────────────────────────────────────────────────────

def _doc_to_dict(doc: DocumentRecord) -> Dict[str, Any]:
    d = asdict(doc)
    return d

def _dict_to_doc(d: Dict[str, Any]) -> DocumentRecord:
    ex = d.get("extracted_data", {})
    if isinstance(ex, dict):
        extracted = ExtractedData(**{
            k: ex.get(k, v)
            for k, v in ExtractedData.__dataclass_fields__.items()
        })
    else:
        extracted = ex
    return DocumentRecord(
        document_id           = d["document_id"],
        claim_id              = d["claim_id"],
        file_name             = d["file_name"],
        file_type             = d["file_type"],
        file_size_bytes       = d["file_size_bytes"],
        content_hash          = d["content_hash"],
        source_channel        = d["source_channel"],
        uploaded_at           = d["uploaded_at"],
        document_type         = d["document_type"],
        classification_confidence = d["classification_confidence"],
        quality_score         = d["quality_score"],
        quality_issues        = d.get("quality_issues", []),
        extracted_data        = extracted,
        tasks_created         = d.get("tasks_created", []),
        alerts_dispatched     = d.get("alerts_dispatched", []),
        litigation_flag       = d.get("litigation_flag", False),
        requires_resubmission = d.get("requires_resubmission", False),
        routing_action        = d.get("routing_action"),
        processing_ms         = d.get("processing_ms", 0),
        agent_version         = d.get("agent_version", AGENT_VERSION),
    )

def _dict_to_task(d: Dict[str, Any]) -> SORTask:
    return SORTask(**{k: d.get(k) for k in SORTask.__dataclass_fields__})

def _dict_to_alert(d: Dict[str, Any]) -> AlertRecord:
    return AlertRecord(**{k: d.get(k) for k in AlertRecord.__dataclass_fields__})
