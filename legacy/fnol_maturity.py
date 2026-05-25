"""
fnol_maturity.py — Industrialization Maturity Model Engine
============================================================
NEW IN V2 BLUEPRINT (L100 Industrialization-Aware Edition).

V2 Blueprint Section 02 — Industrialization Maturity Model.
Maps the L1/L2/L3 capability ladder per FNOL stage and provides:

  - get_active_level()         current maturity level (env var or override)
  - capability_matrix()        full L1/L2/L3 grid (8 stages × 3 levels)
  - architectural_stability()  stable-vs-changing components per V2 §02
  - self_assessment_questions() carrier-readiness checklist
  - score_carrier()            simple scoring engine for carrier inputs
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fnol_workflow_engine import MaturityLevel


# ════════════════════════════════════════════════════════════════════════════════
# ACTIVE LEVEL
# ════════════════════════════════════════════════════════════════════════════════

def get_active_level(override: Optional[str] = None) -> MaturityLevel:
    """Resolve active maturity level from override > env > L2 default."""
    src = override or os.environ.get("FNOL_MATURITY", "L2")
    src = src.upper().strip()
    if src in ("L1","L2","L3"):
        return MaturityLevel(src)
    return MaturityLevel.L2


# ════════════════════════════════════════════════════════════════════════════════
# CAPABILITY MATRIX (V2 Blueprint §02 — 8 stages × 3 levels = 24 capability cells)
# ════════════════════════════════════════════════════════════════════════════════

CAPABILITY_MATRIX: List[Dict[str, Any]] = [
    {
        "stage":"S1·FNOL Capture", "stageId":"S1",
        "L1":"AI assists adjuster with NLP entity extraction; adjuster types into GWCC desktop manually for unparsed fields.",
        "L2":"FNOL Intake Agent (A1) extracts canonical payload, validates, and dispatches DOI ack <60s. Adjuster reviews exceptions only.",
        "L3":"Conversational Orchestration Agent captures, validates, and decides in one continuous claimant-facing session.",
    },
    {
        "stage":"S2·Coverage Verification", "stageId":"S2",
        "L1":"Coverage rules engine returns yes/no; adjuster interprets clauses manually.",
        "L2":"Coverage Verification Agent (A2) maps lossType→clauses, computes complexity, dispatches ROR letter if needed.",
        "L3":"Coverage decision streams into co-pilot context; Conv Orchestration Agent renders explanation in claimant language.",
    },
    {
        "stage":"S3·Triage & Assignment", "stageId":"S3",
        "L1":"ML score advises adjuster; adjuster makes final track decision.",
        "L2":"Triage Agent (A3) routes 70% of claims fully autonomous; HITL only on confidence < 70%.",
        "L3":"Triage runs invisibly; Conv Orchestration claimant sees natural-language outcome ('we can settle this today').",
    },
    {
        "stage":"S4A·Fraud Detection", "stageId":"S4A",
        "L1":"Pilot fraud signals flag claim for SIU review; SIU triages.",
        "L2":"Fraud Agent (A4) runs 40-signal composite at FNOL <5s; payment hold + SIU package automatic.",
        "L3":"Fraud signal updates streamed to co-pilot in real time; SIU referral packet auto-built and routed.",
    },
    {
        "stage":"S4B·Damage Estimation", "stageId":"S4B",
        "L1":"Computer-vision damage triage flags rough severity; adjuster orders inspection.",
        "L2":"Damage Agent (A5) returns AI estimate, ACV, total-loss decision, DRP dispatch — fully automated <10s.",
        "L3":"Damage decision presented to claimant in-session via Conv Orchestration; in-app photo capture loop.",
    },
    {
        "stage":"S5·BI Evaluation", "stageId":"S5",
        "L1":"NLP summarizes medical narrative; BI adjuster handles all valuation.",
        "L2":"BI Agent (A6) reads 200K-token records via Claude Opus, returns P10/P50/P90 settlement spread.",
        "L3":"Co-Pilot drafts demand response & negotiates within authority matrix; senior adjuster on judgment-only escalation.",
    },
    {
        "stage":"S6·Settlement", "stageId":"S6",
        "L1":"Adjuster cuts checks via GWCC payment workflow.",
        "L2":"Settlement Agent (A7) auto-approves PD-only ≤ $15K, gates BI/fraud through HITL.",
        "L3":"Conv Orchestration Agent presents offer in-session; claimant e-signs release; ACH released without adjuster.",
    },
    {
        "stage":"S7·Subrogation", "stageId":"S7",
        "L1":"Subro analyst reviews closed claims for recovery opportunities.",
        "L2":"Subro Agent (A8) identifies recovery at FNOL with ≥80% capture rate (vs. <50% historical).",
        "L3":"Co-Pilot surfaces subrogation pursuit decision at FNOL; auto-letters to third-party carrier.",
    },
]


# ════════════════════════════════════════════════════════════════════════════════
# ARCHITECTURAL STABILITY (V2 §02 — what stays stable vs. what changes)
# ════════════════════════════════════════════════════════════════════════════════

ARCHITECTURAL_STABILITY: Dict[str, List[Dict[str, Any]]] = {
    "stable": [
        {"component":"Canonical FNOL payload schema",
         "note":"Same shape at L1, L2, L3 — only consumers change."},
        {"component":"Event taxonomy & topic names",
         "note":"CrashAlert.Received, FNOL.Captured, Coverage.Verified — identical at all levels."},
        {"component":"SOR adapter pattern",
         "note":"GWCC/Duck Creek/ALIP adapters work identically across maturity."},
        {"component":"Decision Record format",
         "note":"Audit trail format is L1-introduced and L3-required — no schema migration needed."},
        {"component":"BPMN process definition",
         "note":"L1: subset of stages. L2: full. L3: BPMN runs but is invisible."},
    ],
    "changes": [
        {"component":"Number of AI agents",
         "note":"L1: 2–3 pilot agents. L2: 8 production agents. L3: 8 + 2 L3-hook agents (Conv Orch + Co-Pilot)."},
        {"component":"Primary user interface",
         "note":"L1: GWCC desktop. L2: GWCC desktop + AI assist. L3: AI agent layer is the UI."},
        {"component":"Channel strategy",
         "note":"L1/L2: multi-channel intake → workflow. L3: channels collapse into one conversational agent."},
        {"component":"Adjuster role",
         "note":"L1: 100% of claims. L2: ~30% (exceptions). L3: ~10% (judgment-only)."},
        {"component":"HITL frequency",
         "note":"L1: every step. L2: threshold breach. L3: policy & ethical edge cases only."},
    ],
}


# ════════════════════════════════════════════════════════════════════════════════
# CARRIER SELF-ASSESSMENT (V2 §02 readiness checklist)
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class AssessmentQuestion:
    id: str
    question: str
    weight_l1: int
    weight_l2: int
    weight_l3: int


SELF_ASSESSMENT: List[AssessmentQuestion] = [
    AssessmentQuestion("Q1","Do you have a canonical FNOL payload schema published?",1,3,3),
    AssessmentQuestion("Q2","Are events streamed via Kafka/Kinesis/EventHubs?",1,2,3),
    AssessmentQuestion("Q3","Do you publish a Schema Registry with versioned events?",0,2,3),
    AssessmentQuestion("Q4","Do you maintain Decision Records for AI/automated outputs?",0,3,3),
    AssessmentQuestion("Q5","Are SOR (GWCC/Duck Creek) integrations adapter-isolated?",1,2,3),
    AssessmentQuestion("Q6","Do you have a model registry with versioning and approval gates?",0,3,3),
    AssessmentQuestion("Q7","Is HITL coverage <50% of claim flow?",0,2,3),
    AssessmentQuestion("Q8","Do you have an event-driven orchestration layer?",0,2,3),
    AssessmentQuestion("Q9","Are claimants able to converse with an AI in real time?",0,0,3),
    AssessmentQuestion("Q10","Is the adjuster's primary work surface AI-driven (not GWCC desktop)?",0,0,3),
    AssessmentQuestion("Q11","Are 60s DOI acknowledgments automated?",0,2,3),
    AssessmentQuestion("Q12","Are settlement payments auto-released for PD-only ≤ $15K?",0,2,3),
]


def score_carrier(answers: Dict[str, bool]) -> Dict[str, Any]:
    """
    answers: dict of question_id -> True/False (answers for SELF_ASSESSMENT questions).
    Returns carrier maturity score and recommended starting level.
    """
    l1_score = l2_score = l3_score = 0
    l1_max = l2_max = l3_max = 0
    for q in SELF_ASSESSMENT:
        l1_max += q.weight_l1; l2_max += q.weight_l2; l3_max += q.weight_l3
        if answers.get(q.id, False):
            l1_score += q.weight_l1
            l2_score += q.weight_l2
            l3_score += q.weight_l3
    l1_pct = round(l1_score/max(1,l1_max)*100, 1)
    l2_pct = round(l2_score/max(1,l2_max)*100, 1)
    l3_pct = round(l3_score/max(1,l3_max)*100, 1)

    # recommendation
    if l3_pct >= 75:
        rec = MaturityLevel.L3
        rationale = "Carrier exhibits L3-ready posture: canonical schema, event-driven, model registry, HITL minimized."
    elif l2_pct >= 60:
        rec = MaturityLevel.L2
        rationale = "Carrier has L2 building blocks; recommend industrializing the full 8-agent workflow before pursuing L3 inversion."
    else:
        rec = MaturityLevel.L1
        rationale = "Carrier should start with L1 pilots: NLP entity extraction + computer vision damage triage + fraud signal flagging at FNOL."

    return {
        "scores":{"L1":l1_pct,"L2":l2_pct,"L3":l3_pct},
        "recommendedStartingLevel": rec.value,
        "rationale": rationale,
        "L3Trap":"Skipping L1/L2 to chase 'conversational AI claims' produces demo-grade chatbots, not production systems. The path is L1 → L2 → L3 in order.",
    }


def self_assessment_questions() -> List[Dict[str, Any]]:
    return [
        {"id":q.id,"question":q.question,
         "weight_L1":q.weight_l1,"weight_L2":q.weight_l2,"weight_L3":q.weight_l3}
        for q in SELF_ASSESSMENT
    ]


# ════════════════════════════════════════════════════════════════════════════════
# LEVEL DESCRIPTORS
# ════════════════════════════════════════════════════════════════════════════════

LEVEL_DESCRIPTORS: Dict[str, Dict[str, Any]] = {
    "L1":{
        "label":"Pilot — Explore & Pilot",
        "tagline":"AI assists; humans & workflow lead.",
        "agentCount":"2–3 pilot agents",
        "uiSurface":"GWCC / Duck Creek desktop (primary)",
        "adjusterCoverage":"~100% of claims touched by adjuster",
        "hitlPattern":"On every step",
        "channelStrategy":"Multi-channel intake → workflow",
    },
    "L2":{
        "label":"Industrialize — AI Agents in Existing Workflow",
        "tagline":"AI agents own whole workflow stages; workflow is the UI.",
        "agentCount":"8 production agents (A1–A8)",
        "uiSurface":"GWCC / Duck Creek desktop + AI assist panel",
        "adjusterCoverage":"~30% of claims (exceptions only)",
        "hitlPattern":"On threshold breach",
        "channelStrategy":"Multi-channel intake → workflow",
    },
    "L3":{
        "label":"Agentic at the Core — AI-Centric Redesign",
        "tagline":"AI Agent + Interface Layer is the primary UX.",
        "agentCount":"8 + Conversational Orchestration + Co-Pilot",
        "uiSurface":"AI agent layer (claimant-facing & adjuster-facing)",
        "adjusterCoverage":"~10% (judgment-only escalations)",
        "hitlPattern":"Policy & ethical edge cases only",
        "channelStrategy":"Channels collapse into one conversational agent",
    },
}


# ════════════════════════════════════════════════════════════════════════════════
# RUNTIME ACCESSORS (used by API server / UI)
# ════════════════════════════════════════════════════════════════════════════════

def set_active_level(level: str) -> str:
    """Set the active maturity level via env var; returns canonical value."""
    lv = (level or "").upper().strip()
    if lv not in {"L1", "L2", "L3"}:
        raise ValueError("level must be L1, L2, or L3")
    os.environ["FNOL_MATURITY"] = lv
    return lv


def capability_matrix() -> List[Dict[str, Any]]:
    """JSON-friendly view of the capability matrix (V2 §02)."""
    return [
        {"id": row.get("stageId", ""), "stage": row["stage"],
         "L1": row["L1"], "L2": row["L2"], "L3": row["L3"]}
        for row in CAPABILITY_MATRIX
    ]


def architectural_stability() -> List[Dict[str, Any]]:
    """Flat stability table for UI rendering (V2 §02)."""
    out: List[Dict[str, Any]] = []
    for row in ARCHITECTURAL_STABILITY.get("stable", []):
        out.append({"concern": row["component"],
                    "L1": "stable", "L2": "stable", "L3": "stable",
                    "stability": "STABLE", "note": row.get("note", "")})
    for row in ARCHITECTURAL_STABILITY.get("changes", []):
        out.append({"concern": row["component"],
                    "L1": "—", "L2": "—", "L3": "—",
                    "stability": "CHANGES", "note": row.get("note", "")})
    return out


def level_descriptors() -> Dict[str, Dict[str, Any]]:
    """Return the L1/L2/L3 descriptors map."""
    return LEVEL_DESCRIPTORS


# UI-friendly assessment view: each question with Yes/No options.
def assessment_for_ui() -> List[Dict[str, Any]]:
    return [
        {"id": q.id, "text": q.question,
         "options": [{"label": "No"}, {"label": "Yes"}]}
        for q in SELF_ASSESSMENT
    ]


# Capture the original bool-only scorer BEFORE we redefine the name below.
_original_score_carrier = score_carrier


def score_carrier_indexed(answers: Dict[str, Any]) -> Dict[str, Any]:
    """
    Scoring variant tolerant of int (0/1), str ('0'/'1'/'true'), or bool answers
    from the UI. Non-zero / truthy means 'Yes'. Delegates to the original
    bool-only scorer captured above to avoid recursion when score_carrier is
    redefined as a thin wrapper.
    """
    def _coerce(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("1", "true", "yes", "y"):
                return True
            if s in ("0", "false", "no", "n", ""):
                return False
            try:
                return bool(int(s))
            except ValueError:
                return False
        return bool(v)

    bool_answers: Dict[str, bool] = {k: _coerce(v) for k, v in (answers or {}).items()}
    out = _original_score_carrier(bool_answers)
    out["recommended"] = out.get("recommendedStartingLevel")
    out["warning"] = out.get("L3Trap")
    return out


# Replace the public score_carrier name with the int/str-tolerant variant.
def score_carrier(answers: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[no-redef]
    return score_carrier_indexed(answers)


__all__ = [
    "get_active_level", "set_active_level",
    "CAPABILITY_MATRIX", "capability_matrix",
    "ARCHITECTURAL_STABILITY", "architectural_stability",
    "LEVEL_DESCRIPTORS", "level_descriptors",
    "SELF_ASSESSMENT", "assessment_for_ui",
    "score_carrier", "score_carrier_indexed", "self_assessment_questions",
]
