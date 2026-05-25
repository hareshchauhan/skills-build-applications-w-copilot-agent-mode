"""
FNOL Intelligence Platform — LangGraph L3 Orchestration Engine
==============================================================
Blueprint V2 §L3 maturity — the inversion where the AI agent layer becomes
the primary UX and the BPMN workflow becomes a thin backend service.

Why LangGraph for L3
--------------------
The Blueprint's L3 description is verbatim a LangGraph StateGraph:

  "Orchestrator implements deterministic state machine:
   FNOL.Captured → Validated → Triaged → Investigated → Settled → Closed"

LangGraph closes three structural gaps the L2 engine cannot close:

  1. Durable checkpoints.  SqliteSaver (or PostgresSaver in production)
     persists graph state after every node. A server restart during an
     adjuster's 3-day holiday does not lose the claim.  The L2 engine has
     no persistence outside _PIPELINE_TRACES (process-local, lost on restart).

  2. True HITL pause / resume.  interrupt() literally suspends the graph at
     the node boundary and returns control to the caller with the thread_id
     as a resumption handle.  The adjuster reviews, then POSTs back through
     POST /api/v1/fnol/v3/claims/{thread_id}/resume.  The graph resumes from
     the checkpoint with the adjuster decision injected into state.  The L2
     engine fires hitlRequired=True in the Decision Record but never actually
     waits for the human.

  3. Conditional routing as graph edges.  STP fast-path, TL branch, SIU hold,
     fraud denial, and tender-limits review are explicit edges in the graph,
     not nested if/else inside a linear execution loop.  Adding a new routing
     rule is a new edge + conditional function — not a modification to
     run_pipeline().

Backward compatibility
----------------------
run_pipeline() in fnol_workflow_engine.py is NOT modified.  The LangGraph
engine is a parallel track:

  L2 (existing)   POST /api/v1/fnol/claims         → run_pipeline()
  L3 (new)        POST /api/v1/fnol/v3/claims      → run_claim_langgraph()

The LangGraph state is converted back to the canonical pipeline trace shape
(via _state_to_trace()) so every downstream endpoint — Co-Pilot, A11
evaluation, SIU, Governance — works with L3 traces identically to L2.

Graph topology
--------------
START
  → s0_s1_intake_node
  → s2_coverage_node
  → s3_triage_node
  → [hitl_gate_triage]           ← interrupt if T4_COMPLEX / triage HITL
  → parallel_s4_node             ← S4A Fraud + S4B Damage concurrent
  → [router_after_s4]:
      CRITICAL  → siu_hold_node  ← interrupt; resume routes to s5 or denial
      total_loss → a11_node      → s5_bi_node
      else       → s5_bi_node
  → [hitl_gate_bi]               ← interrupt if tender-limits flag
  → s6_settlement_node
  → s7_subrogation_node
  → END

Node contract
-------------
Each node receives the full FNOLGraphState and returns a PARTIAL state dict
containing only the fields it updated. LangGraph's reducer functions merge
the partial update into the accumulated state. Nodes never return the full
state — that would bypass the reducer semantics.

Public API
----------
  run_claim_langgraph(claim: Claim) -> Dict         Synchronous (HITL auto-approved)
  create_claim_thread(claim: Claim) -> str           Async; returns thread_id
  resume_thread(thread_id, decision) -> Dict         Resume after HITL interrupt
  get_thread_state(thread_id) -> Dict                Current graph state
  list_threads(limit) -> List[Dict]                  All active threads
  get_graph_health() -> Dict                         Adapter health + thread count

Production hardening (pre-go-live)
-----------------------------------
  - Replace SqliteSaver with PostgresSaver (asyncpg) for multi-process deployment
  - Add RBAC on resume endpoint: only the assigned adjuster may approve their claim
  - Wire interrupt() value as a WebSocket push to the adjuster's Co-Pilot surface
  - Implement supervisor escalation timeout: if hitl_gate not resumed in 4h,
    auto-escalate via SLA monitor (Blueprint §siu-hold-subprocess SLA rule)
  - Add graph observability: wire LangSmith tracing for node-level latency telemetry
  - Replace ThreadPoolExecutor inside parallel_s4_node with LangGraph's native
    Send() API fan-out once the stage function signatures are decoupled from
    the shared `claim` object (the shared reference is thread-safe for reads only)
"""

from __future__ import annotations

import logging
import operator
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Annotated, Dict, List, Optional, TypedDict

log = logging.getLogger("fnol.langgraph")

# ───────────────────────────────────────────────────────────────────────────
# LangGraph import guard
# ───────────────────────────────────────────────────────────────────────────
# LangGraph is an optional dependency for the L3 upgrade path.
# The L2 server starts and runs normally when langgraph is not installed;
# only the /v3/ endpoints return 503.

try:
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import interrupt, Command
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    log.warning(
        "langgraph not installed. L3 endpoints will return 503. "
        "Install with: pip install langgraph langgraph-checkpoint-sqlite"
    )

# Platform imports
from fnol_claim import Claim, TelematicsPayload
from fnol_sor_adapter import get_sor_adapter
from fnol_settings import settings
from fnol_workflow_engine import (
    PIPELINE_VERSION,
    stage_s0_pre_fnol,
    stage_s1_fnol_capture,
    stage_s2_coverage,
    stage_s3_triage,
    stage_s4a_fraud,
    stage_s4b_damage,
    stage_a11_total_loss,
    stage_s5_bi_liability,
    stage_s6_settlement,
    stage_s7_subrogation,
    StageResult,
    DecisionRecord,
    _safe_run_stage,
    StageDef,
)
from fnol_llm_adapter import resolve_provider


# ───────────────────────────────────────────────────────────────────────────
# State schema
# ───────────────────────────────────────────────────────────────────────────

def _merge_dicts(a: Dict, b: Dict) -> Dict:
    """Reducer for accumulated dicts (stage outputs, metadata).
    LangGraph calls this as reducer(existing_value, node_update).
    Result: shallow merge — node updates take precedence over prior values
    for overlapping keys. Stage output dicts don't overlap (each uses a
    distinct stage_id key) so this is lossless for the pipeline trace."""
    return {**a, **b}


class FNOLGraphState(TypedDict):
    """
    Canonical state type for the FNOL LangGraph pipeline.

    LangGraph accumulates state across nodes via reducer functions declared
    in Annotated[T, reducer_fn] fields. Scalar fields use last-write-wins
    (LangGraph default). List fields use operator.add (append). Dict fields
    use _merge_dicts (shallow merge).

    Nodes MUST return partial dicts (only the fields they update). Returning
    the full state would re-trigger every reducer with the full value.
    """

    # ── Identity — set once at claim creation ───────────────────────────
    claim_id:         str
    claim_payload:    Dict[str, Any]   # Claim.model_dump()
    thread_id:        str
    started_at:       str
    pipeline_version: str

    # ── Stage outputs (accumulated: each stage adds its own key) ─────────
    # Format: {"S0": {outputs}, "S1": {outputs}, "S4A": {outputs}, ...}
    # _merge_dicts reducer: non-overlapping keys → lossless accumulation.
    stage_outputs: Annotated[Dict[str, Any], _merge_dicts]

    # ── Stage metadata (accumulated: status + timing per stage) ──────────
    # Format: {"S0": {"status": "ok", "elapsed_ms": 45, "agent": "..."}, ...}
    stage_meta: Annotated[Dict[str, Any], _merge_dicts]

    # ── Decision records (accumulated: append across all stages) ─────────
    all_decisions: Annotated[List[Dict[str, Any]], operator.add]

    # ── Advisories (accumulated: append across all stages) ───────────────
    all_advisories: Annotated[List[str], operator.add]

    # ── Routing signals (last-write-wins scalars) ────────────────────────
    # Set by parallel_s4_node; consumed by conditional edge router.
    stp_eligible:       bool
    fraud_band:         str     # LOW | MEDIUM | HIGH | CRITICAL
    fraud_risk_score:   float
    payment_hold_flag:  bool
    total_loss_flag:    bool
    siu_hold_active:    bool    # True when fraud_band == CRITICAL

    # ── HITL tracking ────────────────────────────────────────────────────
    # pending_hitl: list of stage_ids that raised hitl_required=True.
    # Accumulates across nodes; cleared when the gate node handles them.
    pending_hitl:    Annotated[List[str], operator.add]
    # hitl_decisions: adjuster decisions keyed by stage_id or gate name.
    # Injected via Command(resume={...}) when the graph is resumed.
    hitl_decisions:  Annotated[Dict[str, Any], _merge_dicts]

    # ── Final state (set by terminal logic in s7_subrogation_node) ───────
    final_status:  str
    completed_at:  str
    graph_error:   str     # empty string if no error


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dr_to_dict(dr: DecisionRecord) -> Dict[str, Any]:
    return asdict(dr)


def _stage_to_meta(r: StageResult) -> Dict[str, Any]:
    return {
        "stage_id":   r.stage_id,
        "stage_name": r.stage_name,
        "agent":      r.agent,
        "status":     r.status,
        "elapsed_ms": r.duration_ms,
        "started_at": r.started_at,
        "completed_at": r.completed_at,
        "error":      r.error or "",
    }


def _collect_stage(r: StageResult) -> Dict[str, Any]:
    """Convert a StageResult into a partial state update dict.
    All fields are properly typed for the FNOLGraphState reducers."""
    hitl_stages = [r.stage_id] if r.status == "hitl" else []
    return {
        "stage_outputs": {r.stage_id: r.outputs},
        "stage_meta":    {r.stage_id: _stage_to_meta(r)},
        "all_decisions": [_dr_to_dict(dr) for dr in r.decisions],
        "all_advisories": list(r.advisories),
        "pending_hitl":  hitl_stages,
    }


def _claim_from_state(state: FNOLGraphState) -> Claim:
    """Reconstruct the Claim model from the state payload.
    Uses model_copy to avoid mutating the checkpointed payload."""
    return Claim(**state["claim_payload"])


# ───────────────────────────────────────────────────────────────────────────
# Node implementations
# ───────────────────────────────────────────────────────────────────────────

def _node_s0_s1_intake(state: FNOLGraphState) -> Dict[str, Any]:
    """S0 Pre-FNOL / Crash Detection + S1 FNOL Capture & Validation.

    These two stages run sequentially in the same node because S1 reads
    S0's crash-detection outputs (delta_v, airbag, crash_alert) as part
    of its telematics-informed intake validation. Splitting them into
    separate nodes would require S0's outputs to flow through graph state,
    which adds latency without enabling parallel execution.
    """
    claim = _claim_from_state(state)
    r_s0 = _safe_run_stage(StageDef("S0", stage_s0_pre_fnol, "FNOL Intake Agent"), claim, {})
    ctx_s1 = {"S0": r_s0.outputs}
    r_s1 = _safe_run_stage(StageDef("S1", stage_s1_fnol_capture, "FNOL Intake Agent"),
                            claim, ctx_s1)
    update = _collect_stage(r_s0)
    s1_update = _collect_stage(r_s1)
    # Merge both stage updates into one return dict
    return {
        "stage_outputs":  {**update["stage_outputs"],  **s1_update["stage_outputs"]},
        "stage_meta":     {**update["stage_meta"],     **s1_update["stage_meta"]},
        "all_decisions":  update["all_decisions"]  + s1_update["all_decisions"],
        "all_advisories": update["all_advisories"] + s1_update["all_advisories"],
        "pending_hitl":   update["pending_hitl"]   + s1_update["pending_hitl"],
    }


def _node_s2_coverage(state: FNOLGraphState) -> Dict[str, Any]:
    """S2 Coverage Verification & Reservation.

    Reads S0+S1 outputs from stage_outputs to run the policy in-force check,
    coverage type resolution, exclusion matching, and ROR trigger.
    Coverage dispute → pipeline status = COVERAGE_DISPUTE.
    """
    claim = _claim_from_state(state)
    ctx = dict(state["stage_outputs"])
    r = _safe_run_stage(StageDef("S2", stage_s2_coverage, "Coverage & Liability Agent"),
                        claim, ctx)
    return _collect_stage(r)


def _node_s3_triage(state: FNOLGraphState) -> Dict[str, Any]:
    """S3 Triage, Complexity & Assignment.

    Emits: stp_eligible, recommended_track (STP | T2 | T3 | T4 | T5 | SIU_HOLD),
    track_confidence, assigned_adjuster, litigation_propensity_score.

    Blueprint HITL rule: T4_COMPLEX and T5_CRITICAL require adjuster confirmation
    before advancing to S4. The graph enforces this via hitl_gate_triage which
    runs as the next node when status == "hitl".
    """
    claim = _claim_from_state(state)
    ctx = dict(state["stage_outputs"])
    r = _safe_run_stage(StageDef("S3", stage_s3_triage, "Triage & Assignment Agent"),
                        claim, ctx)
    partial = _collect_stage(r)
    # Surface stp_eligible as a routing signal for the conditional edges
    partial["stp_eligible"] = bool(r.outputs.get("stp_eligible", False))
    return partial


def _node_hitl_gate_triage(state: FNOLGraphState) -> Dict[str, Any]:
    """HITL gate after S3 — activates when triage flags T4_COMPLEX / T5_CRITICAL.

    interrupt() suspends graph execution at this node. LangGraph checkpoints
    the full FNOLGraphState to SQLite and returns an interrupt payload to the
    caller (the API route handler). The caller receives:

        {"__interrupt__": [{"value": {...}, "resumable": True}]}

    The API returns the thread_id to the adjuster. The adjuster reviews the
    triage assignment and POSTs:

        POST /api/v1/fnol/v3/claims/{thread_id}/resume
        {"decision": "APPROVED", "adjuster_id": "ADJ-001", "notes": "..."}

    The graph then resumes from this exact node with the decision injected.

    Blueprint §S3 SLA: hitlRequired=true → adjuster notified → 4h SLA before
    supervisor escalation. The SLA timer runs in the adjuster's case management
    system (not in this graph — the graph simply waits indefinitely for resume).
    """
    pending = state.get("pending_hitl", [])
    s3_hitl = "S3" in pending

    if not s3_hitl:
        # Triage completed clean — pass through without interrupt
        return {}

    s3_ctx = state["stage_outputs"].get("S3", {})
    track   = s3_ctx.get("recommended_track", "UNKNOWN")
    adj     = s3_ctx.get("assigned_adjuster", "Unassigned")

    # Suspend graph — adjuster must approve triage assignment before S4 runs
    decision = interrupt({
        "gate":        "TRIAGE_REVIEW",
        "claim_id":    state["claim_id"],
        "thread_id":   state["thread_id"],
        "track":       track,
        "adjuster":    adj,
        "sla_hours":   4,
        "message": (
            f"Triage assigned track {track} to {adj}. "
            "Adjuster confirmation required before S4A/S4B proceed. "
            f"POST /api/v1/fnol/v3/claims/{state['thread_id']}/resume "
            "with {decision: APPROVED|REASSIGN, adjuster_id, notes}."
        ),
    })
    return {
        "hitl_decisions": {"TRIAGE": decision},
    }


def _node_parallel_s4(state: FNOLGraphState) -> Dict[str, Any]:
    """S4A Fraud Detection + S4B Damage Estimation — concurrent execution.

    S4A and S4B are independent given S0–S3 outputs and can execute in parallel
    (Blueprint §S4: ‖ parallel stage). ThreadPoolExecutor with max_workers=2
    mirrors the existing run_pipeline() concurrency model.

    Note: LangGraph's native Send() API could fan-out to separate nodes, but
    that requires decoupling S4A/S4B from the shared `claim` object reference.
    The Claim model is read-only in S4A/S4B so the shared reference is
    thread-safe; ThreadPoolExecutor is simpler and equally performant for
    a 2-stage parallel batch. This can be migrated to Send() in a later sprint.

    Routing signals written to state:
      fraud_band         → consumed by _route_after_s4 conditional edge
      payment_hold_flag  → propagated to S6 (blocks disbursement)
      total_loss_flag    → triggers A11 branch
      siu_hold_active    → triggers SIU hold gate
    """
    claim = _claim_from_state(state)
    ctx = dict(state["stage_outputs"])

    s4a_def = StageDef("S4A", stage_s4a_fraud, "Fraud Detection Agent", parallel_group="S4")
    s4b_def = StageDef("S4B", stage_s4b_damage, "Damage Estimation Agent", parallel_group="S4")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_s4a = pool.submit(_safe_run_stage, s4a_def, claim, ctx)
        f_s4b = pool.submit(_safe_run_stage, s4b_def, claim, ctx)
        r_s4a = f_s4a.result()
        r_s4b = f_s4b.result()

    partial_a = _collect_stage(r_s4a)
    partial_b = _collect_stage(r_s4b)

    fraud_band  = r_s4a.outputs.get("fraud_band") or r_s4a.outputs.get("fraud_risk_band") or "LOW"
    fraud_score = float(r_s4a.outputs.get("fraud_risk_score") or r_s4a.outputs.get("fraud_composite_score") or 0.0)
    pay_hold    = bool(r_s4a.outputs.get("payment_hold_flag", False))
    total_loss  = bool(r_s4b.outputs.get("total_loss", False))
    siu_hold    = fraud_band == "CRITICAL"

    return {
        "stage_outputs":  {**partial_a["stage_outputs"],  **partial_b["stage_outputs"]},
        "stage_meta":     {**partial_a["stage_meta"],     **partial_b["stage_meta"]},
        "all_decisions":  partial_a["all_decisions"]  + partial_b["all_decisions"],
        "all_advisories": partial_a["all_advisories"] + partial_b["all_advisories"],
        "pending_hitl":   partial_a["pending_hitl"]   + partial_b["pending_hitl"],
        # Routing signals
        "fraud_band":        fraud_band,
        "fraud_risk_score":  fraud_score,
        "payment_hold_flag": pay_hold,
        "total_loss_flag":   total_loss,
        "siu_hold_active":   siu_hold,
    }


def _node_siu_hold(state: FNOLGraphState) -> Dict[str, Any]:
    """SIU hold gate — fires only when fraud_band == CRITICAL.

    Suspends the graph with a SIU_HOLD interrupt. The SIU investigator:
      1. Opens an A12 case via POST /api/v1/fnol/siu/open
      2. Runs investigation, generates referral memo
      3. Closes the case with CLEARED | CONFIRMED_FRAUD | CLOSED_INCONCLUSIVE
      4. Resumes the graph via POST /api/v1/fnol/v3/claims/{thread_id}/resume
         with {"disposition": "CLEARED"} or {"disposition": "CONFIRMED_FRAUD"}

    Blueprint §siu-hold-subprocess: SIU referral within 4h of CRITICAL flag.
    The SLA clock is tracked by the SIU management system, not this graph.

    On resume with CLEARED      → graph routes to S5 (payment hold released)
    On resume with CONFIRMED_FRAUD → graph routes to END (denial)
    On resume with CLOSED_INCONCLUSIVE → graph routes to S5 (adjuster decides)
    """
    fraud_score = state.get("fraud_risk_score", 0.0)
    siu_disposition = interrupt({
        "gate":          "SIU_HOLD",
        "claim_id":      state["claim_id"],
        "thread_id":     state["thread_id"],
        "fraud_band":    state.get("fraud_band"),
        "fraud_score":   fraud_score,
        "sla_hours":     4,
        "action_required": (
            "1. Open A12 case: POST /api/v1/fnol/siu/open "
            f"with claim_id={state['claim_id']}. "
            "2. Investigate and close via POST /api/v1/fnol/siu/close. "
            f"3. Resume graph: POST /api/v1/fnol/v3/claims/{state['thread_id']}/resume "
            "with {disposition: CLEARED|CONFIRMED_FRAUD|CLOSED_INCONCLUSIVE}."
        ),
    })
    return {
        "hitl_decisions": {"SIU_HOLD": siu_disposition},
    }


def _node_a11_total_loss(state: FNOLGraphState) -> Dict[str, Any]:
    """A11 Total-Loss & Salvage Orchestrator.

    Conditional node — only reached when S4B.total_loss == True. Computes
    state TLT, ACV refinement, branded title recommendation, shadow-quotes
    Copart/IAA, and generates two settlement options.

    The customer notification letter is LLM-drafted and included in outputs.
    A11 is also registered with the TotalLossAgent store so the existing
    A11 API endpoints (/api/v1/fnol/total-loss/*) work on L3 traces.
    """
    claim = _claim_from_state(state)
    ctx = dict(state["stage_outputs"])
    a11_def = StageDef("A11", stage_a11_total_loss, "Total-Loss & Salvage Orchestrator")
    r = _safe_run_stage(a11_def, claim, ctx)
    return _collect_stage(r)


def _node_s5_bi(state: FNOLGraphState) -> Dict[str, Any]:
    """S5 BI Evaluation & Liability.

    Computes BI estimate P10/P50/P90, adverse fault percentage, tender-limits
    flag (≥0.90× per-person BI limit), and fatality escalation.

    Blueprint rule: tender_limits_flag=True → notify excess carrier within 24h
    and require adjuster sign-off before any BI offer. The hitl_gate_bi node
    enforces this via interrupt() when the flag fires.
    """
    claim = _claim_from_state(state)
    ctx = dict(state["stage_outputs"])
    r = _safe_run_stage(StageDef("S5", stage_s5_bi_liability,
                                 "Coverage & Liability Agent + BI Model"), claim, ctx)
    return _collect_stage(r)


def _node_hitl_gate_bi(state: FNOLGraphState) -> Dict[str, Any]:
    """HITL gate after S5 — activates on tender-limits flag or fatality.

    Tender limits: when the BI estimate approaches the per-person policy limit,
    the excess carrier must be notified within 24h and the adjuster must review
    before any BI offer is made. Blueprint §S5 compliance requirement.
    """
    s5_ctx = state["stage_outputs"].get("S5", {})
    tender = bool(s5_ctx.get("tender_limits_flag", False))
    fatality = bool(state["claim_payload"].get("fatality_indicator", False))
    s5_hitl = "S5" in state.get("pending_hitl", [])

    if not (tender or fatality or s5_hitl):
        return {}

    reason = []
    if tender:
        reason.append("Tender-limits flag — BI estimate ≥90% per-person limit")
    if fatality:
        reason.append("Fatality indicator — senior adjuster + supervisor required")

    decision = interrupt({
        "gate":         "BI_REVIEW",
        "claim_id":     state["claim_id"],
        "thread_id":    state["thread_id"],
        "bi_p50":       s5_ctx.get("settlement_p50_usd", 0),
        "bi_p90":       s5_ctx.get("settlement_p90_usd", 0),
        "adverse_fault": s5_ctx.get("adverse_fault_pct", 0),
        "reasons":      reason,
        "sla_hours":    24,
        "message": (
            f"BI review required: {'; '.join(reason)}. "
            f"POST /api/v1/fnol/v3/claims/{state['thread_id']}/resume "
            "with {decision: APPROVED|ADJUST_OFFER, approved_bi_usd, adjuster_id}."
        ),
    })
    return {
        "hitl_decisions": {"BI_REVIEW": decision},
    }


def _node_s6_settlement(state: FNOLGraphState) -> Dict[str, Any]:
    """S6 Settlement & Payment Authorization.

    Duck Creek write-back: authorize_payment() is called by stage_s6_settlement
    when STP conditions are met. Payment reference IDs are written to stage outputs
    and available to the Co-Pilot and adjuster dashboard.

    If payment_hold_flag is True (fraud hold from S4A), S6 outputs
    settlement_status=PAYMENT_HELD and no disbursement is triggered.
    """
    claim = _claim_from_state(state)
    ctx = dict(state["stage_outputs"])
    r = _safe_run_stage(StageDef("S6", stage_s6_settlement, "Settlement Agent"),
                        claim, ctx)
    return _collect_stage(r)


def _node_s7_subrogation(state: FNOLGraphState) -> Dict[str, Any]:
    """S7 Subrogation & Recovery — final pipeline stage.

    Computes subrogation eligibility, TP carrier lookup, NHTSA recall indicator,
    and demand letter trigger. After this node the graph transitions to END.

    Also sets final_status and completed_at on the state so the pipeline trace
    builder has clean terminal values without post-processing.
    """
    claim = _claim_from_state(state)
    ctx = dict(state["stage_outputs"])
    r = _safe_run_stage(StageDef("S7", stage_s7_subrogation, "Subrogation Agent"),
                        claim, ctx)
    partial = _collect_stage(r)

    # Roll up final_status (mirrors run_pipeline() logic)
    s6_ctx = {**state["stage_outputs"], **partial["stage_outputs"]}
    s6_out = s6_ctx.get("S6", s6_ctx.get("S4A", {}))
    s1_out = state["stage_outputs"].get("S1", {})

    all_meta  = {**state.get("stage_meta", {}), **partial["stage_meta"]}
    any_hold  = any(v.get("status") == "hold"  for v in all_meta.values())
    any_hitl  = any(v.get("status") == "hitl"  for v in all_meta.values())
    any_error = any(v.get("status") == "error" for v in all_meta.values())

    if any_hold or state.get("siu_hold_active"):
        final = "ON_HOLD"
    elif s6_out.get("settlement_status") == "AUTHORIZED_STP":
        final = "STP_AUTHORIZED"
    elif any_hitl:
        final = "ADJUSTER_REVIEW"
    elif s1_out.get("fnol_status") == "DISPUTE":
        final = "COVERAGE_DISPUTE"
    elif any_error:
        final = "PIPELINE_ERROR"
    else:
        final = "OPEN"

    partial["final_status"] = final
    partial["completed_at"] = _now()
    partial["graph_error"]  = ""
    return partial


def _node_confirmed_fraud_denial(state: FNOLGraphState) -> Dict[str, Any]:
    """Terminal node for CONFIRMED_FRAUD disposition.

    Reached when the SIU investigator resumes the graph with
    disposition=CONFIRMED_FRAUD. Updates the SOR claim record to denied
    status and stamps the final_status. An FCRA §615 adverse action notice
    must be generated via POST /api/v1/fnol/governance/adverse-action.
    """
    cid = state["claim_id"]
    try:
        sor = get_sor_adapter()
        sor.update_claim(cid, {
            "status":      "DENIED_FRAUD",
            "updated_at":  _now(),
            "denial_reason": "SIU investigation confirmed fraud — claim denied",
        })
    except Exception as exc:
        log.error("SOR update failed on fraud denial for %s: %s", cid, exc)
    return {
        "final_status": "DENIED_FRAUD",
        "completed_at": _now(),
        "graph_error":  "",
        "all_advisories": [
            "Claim denied — confirmed fraud. "
            "FCRA §615 adverse action notice required: "
            f"POST /api/v1/fnol/governance/adverse-action with claim_id={cid}."
        ],
    }


# ───────────────────────────────────────────────────────────────────────────
# Conditional edge functions (pure — no side effects, no I/O)
# ───────────────────────────────────────────────────────────────────────────

def _route_after_triage(state: FNOLGraphState) -> str:
    """All claims advance to the triage HITL gate regardless of track.
    The gate passes through silently when no HITL is needed."""
    return "hitl_gate_triage"


def _route_after_hitl_triage(state: FNOLGraphState) -> str:
    """After triage HITL gate, all claims go to parallel S4."""
    return "parallel_s4"


def _route_after_s4(state: FNOLGraphState) -> str:
    """Core routing decision after parallel S4A/S4B.

    Blueprint §S4A routing rules (in priority order):
      1. CRITICAL fraud band → SIU hold (mandatory, regardless of TL flag)
      2. Total loss + no SIU hold → A11 branch
      3. Everything else → S5 BI evaluation
    """
    if state.get("siu_hold_active"):
        return "siu_hold"
    if state.get("total_loss_flag"):
        return "a11_total_loss"
    return "s5_bi"


def _route_after_siu_hold(state: FNOLGraphState) -> str:
    """Route after SIU investigator resumes the graph.

    The adjuster/SIU supervisor injects the A12 disposition via
    Command(resume={"disposition": "CLEARED"|"CONFIRMED_FRAUD"|...}).
    This edge reads that decision from hitl_decisions.
    """
    siu_decision = state.get("hitl_decisions", {}).get("SIU_HOLD", {})
    disposition  = (siu_decision.get("disposition") or "CLOSED_INCONCLUSIVE").upper()

    if disposition == "CONFIRMED_FRAUD":
        return "confirmed_fraud_denial"
    # CLEARED or CLOSED_INCONCLUSIVE → continue to BI evaluation
    # Payment hold release is handled in stage_s6_settlement when it reads
    # the updated SOR record (A12 close_case() updates the SOR payment_hold).
    if state.get("total_loss_flag"):
        return "a11_total_loss"
    return "s5_bi"


def _route_after_a11(state: FNOLGraphState) -> str:
    """A11 always routes to BI evaluation — injury component must be assessed
    even on total loss claims (BI is independent of vehicle disposition)."""
    return "hitl_gate_bi"


def _route_after_hitl_bi(state: FNOLGraphState) -> str:
    return "s6_settlement"


# ───────────────────────────────────────────────────────────────────────────
# Graph construction
# ───────────────────────────────────────────────────────────────────────────

def _build_graph() -> Any:  # returns compiled CompiledStateGraph
    """Construct and compile the FNOL LangGraph StateGraph.

    The graph is compiled once at module load time and reused across all
    claim submissions. The checkpointer is attached at compile time — it must
    be the same instance used for all invocations to ensure thread_id routing
    works correctly.

    The compiled graph is a singleton. Thread safety: StateGraph.invoke()
    and StateGraph.stream() are thread-safe — each invocation operates on its
    own thread_id namespace within the checkpointer.
    """
    if not LANGGRAPH_AVAILABLE:
        return None

    db_path = Path(getattr(settings, "langgraph_db_path",
                           "fnol_langgraph_checkpoint.db"))

    # SqliteSaver.from_conn_string() returns a _GeneratorContextManager in
    # langgraph-checkpoint-sqlite >= 2.x — passing it directly to compile()
    # raises "Invalid checkpointer provided. Expected BaseCheckpointSaver."
    #
    # The stable API across all versions is SqliteSaver(conn) where conn is
    # a real sqlite3.Connection opened with check_same_thread=False (required
    # because LangGraph invokes nodes from threads inside ThreadPoolExecutor).
    import sqlite3 as _sqlite3
    _conn = _sqlite3.connect(str(db_path), check_same_thread=False)
    checkpointer = SqliteSaver(_conn)

    g: StateGraph = StateGraph(FNOLGraphState)

    # ── Register nodes ────────────────────────────────────────────────────
    g.add_node("s0_s1_intake",         _node_s0_s1_intake)
    g.add_node("s2_coverage",          _node_s2_coverage)
    g.add_node("s3_triage",            _node_s3_triage)
    g.add_node("hitl_gate_triage",     _node_hitl_gate_triage)
    g.add_node("parallel_s4",          _node_parallel_s4)
    g.add_node("siu_hold",             _node_siu_hold)
    g.add_node("a11_total_loss",       _node_a11_total_loss)
    g.add_node("s5_bi",                _node_s5_bi)
    g.add_node("hitl_gate_bi",         _node_hitl_gate_bi)
    g.add_node("s6_settlement",        _node_s6_settlement)
    g.add_node("s7_subrogation",       _node_s7_subrogation)
    g.add_node("confirmed_fraud_denial", _node_confirmed_fraud_denial)

    # ── Sequential edges (deterministic) ──────────────────────────────────
    g.add_edge(START,          "s0_s1_intake")
    g.add_edge("s0_s1_intake", "s2_coverage")
    g.add_edge("s2_coverage",  "s3_triage")

    # ── Conditional edges ─────────────────────────────────────────────────
    g.add_conditional_edges("s3_triage", _route_after_triage,
                            {"hitl_gate_triage": "hitl_gate_triage"})

    g.add_conditional_edges("hitl_gate_triage", _route_after_hitl_triage,
                            {"parallel_s4": "parallel_s4"})

    g.add_conditional_edges("parallel_s4", _route_after_s4, {
        "siu_hold":      "siu_hold",
        "a11_total_loss": "a11_total_loss",
        "s5_bi":         "s5_bi",
    })

    g.add_conditional_edges("siu_hold", _route_after_siu_hold, {
        "confirmed_fraud_denial": "confirmed_fraud_denial",
        "a11_total_loss":         "a11_total_loss",
        "s5_bi":                  "s5_bi",
    })

    g.add_conditional_edges("a11_total_loss", _route_after_a11,
                            {"hitl_gate_bi": "hitl_gate_bi"})

    g.add_edge("s5_bi", "hitl_gate_bi")

    g.add_conditional_edges("hitl_gate_bi", _route_after_hitl_bi,
                            {"s6_settlement": "s6_settlement"})

    g.add_edge("s6_settlement",          "s7_subrogation")
    g.add_edge("s7_subrogation",         END)
    g.add_edge("confirmed_fraud_denial", END)

    return g.compile(checkpointer=checkpointer)


# Singleton graph — compiled once at module import.
_GRAPH: Any = None


def _get_graph() -> Any:
    """Return the compiled graph singleton, or None if build failed.

    Errors in _build_graph() (wrong SqliteSaver API, file permissions,
    graph compilation failure, etc.) are caught here so they don't propagate
    as unhandled 500s to every L3 route. Callers must guard for None.
    """
    global _GRAPH
    if _GRAPH is None:
        try:
            _GRAPH = _build_graph()
        except Exception as exc:
            log.error("LangGraph _build_graph failed: %s", exc)
            return None   # deliberately do NOT set _GRAPH — retry on next call
    return _GRAPH


# ───────────────────────────────────────────────────────────────────────────
# State → pipeline trace conversion (backward compatibility)
# ───────────────────────────────────────────────────────────────────────────

_STAGE_ORDER = ["S0", "S1", "S2", "S3", "S4A", "S4B", "A11", "S5", "S6", "S7"]

_AGENT_NAMES = {
    "S0":  "FNOL Intake Agent",
    "S1":  "FNOL Intake Agent",
    "S2":  "Coverage & Liability Agent",
    "S3":  "Triage & Assignment Agent",
    "S4A": "Fraud Detection Agent",
    "S4B": "Damage Estimation Agent",
    "A11": "Total-Loss & Salvage Orchestrator",
    "S5":  "Coverage & Liability Agent + BI Model",
    "S6":  "Settlement Agent",
    "S7":  "Subrogation Agent",
}

_STAGE_NAMES = {
    "S0":  "Pre-FNOL / Crash Detection",
    "S1":  "FNOL Capture & Validation",
    "S2":  "Coverage Verification & Reservation",
    "S3":  "Triage, Complexity & Assignment",
    "S4A": "Fraud & Anomaly Detection",
    "S4B": "AI-Powered Damage Assessment",
    "A11": "Total-Loss & Salvage Orchestrator",
    "S5":  "BI Evaluation & Liability",
    "S6":  "Settlement & Payment Authorization",
    "S7":  "Subrogation & Recovery",
}


def _state_to_trace(state: FNOLGraphState, started_at: str, elapsed_ms: int) -> Dict[str, Any]:
    """Convert FNOLGraphState to the canonical pipeline trace dict.

    The pipeline trace shape is consumed by: Co-Pilot (A9), SIU (A12),
    Governance (bias proxy, decision log), Total-Loss detail endpoint, and
    the UI pipeline tab. All those consumers are L2-format-aware, so this
    function bridges L3 state → L2 trace without modifying any consumer.
    """
    stage_outputs = state.get("stage_outputs", {})
    stage_meta    = state.get("stage_meta",    {})

    stages_list = []
    for sid in _STAGE_ORDER:
        if sid not in stage_outputs and sid not in stage_meta:
            continue
        meta = stage_meta.get(sid, {})
        out  = stage_outputs.get(sid, {})
        # Filter decisions for this stage from the flat all_decisions list
        stage_decisions = [
            d for d in state.get("all_decisions", [])
            if d.get("stage_id") == sid
        ]
        stages_list.append({
            "stage_id":    sid,
            "stage_name":  meta.get("stage_name") or _STAGE_NAMES.get(sid, sid),
            "agent":       meta.get("agent") or _AGENT_NAMES.get(sid, sid),
            "status":      meta.get("status", "ok"),
            "started_at":  meta.get("started_at", ""),
            "completed_at": meta.get("completed_at", ""),
            "elapsed_ms":  meta.get("elapsed_ms", 0),
            "duration_ms": meta.get("elapsed_ms", 0),
            "outputs":     out,
            "decisions":   stage_decisions,
            "advisories":  [a for a in state.get("all_advisories", [])
                            if sid.lower() in a.lower()],
            "error":       meta.get("error", ""),
        })

    s3_out  = stage_outputs.get("S3",  {})
    s4a_out = stage_outputs.get("S4A", {})
    s4b_out = stage_outputs.get("S4B", {})
    s6_out  = stage_outputs.get("S6",  {})

    return {
        "claim_id":        state["claim_id"],
        "pipeline_version": f"{state.get('pipeline_version', PIPELINE_VERSION)}-lg3",
        "llm_provider":    resolve_provider(),
        "started_at":      started_at,
        "completed_at":    state.get("completed_at", _now()),
        "total_duration_ms": elapsed_ms,
        "final_status":    state.get("final_status", "OPEN"),
        "stages":          stages_list,
        "hitl_pending":    state.get("pending_hitl", []),
        "hitl_decisions":  state.get("hitl_decisions", {}),
        "fraud_band":      state.get("fraud_band", ""),
        "siu_hold_active": state.get("siu_hold_active", False),
        "orchestrator":    "langgraph-v3",
        "claim_record": {
            "claim_id":    state["claim_id"],
            "status":      state.get("final_status", "OPEN"),
            "summary": {
                "track":      s3_out.get("recommended_track"),
                "adjuster":   s3_out.get("assigned_adjuster"),
                "fraud_band": s4a_out.get("fraud_band") or s4a_out.get("fraud_risk_band"),
                "damage_point": s4b_out.get("ai_damage_estimate_point_usd"),
                "settlement_status": s6_out.get("settlement_status"),
                "settlement_amount_usd": s6_out.get("amount_authorized_usd"),
                "total_loss": state.get("total_loss_flag", False),
            },
        },
    }


# ───────────────────────────────────────────────────────────────────────────
# Initial state builder
# ───────────────────────────────────────────────────────────────────────────

def _initial_state(claim: Claim, thread_id: str) -> FNOLGraphState:
    """Build the initial FNOLGraphState for a new claim thread.

    All Annotated list/dict fields MUST be initialised to empty
    containers — LangGraph's reducers apply on top of the initial value,
    so an uninitialised field raises a KeyError on first node update.
    """
    if not claim.claim_id:
        claim.claim_id = f"CLM-{uuid.uuid4().hex.upper()}"
    if not claim.created_at:
        claim.created_at = _now()

    # Create SOR record
    try:
        get_sor_adapter().create_claim(claim.to_sor_payload())
    except Exception as exc:
        log.warning("SOR create_claim failed for %s: %s", claim.claim_id, exc)

    return FNOLGraphState(
        claim_id=claim.claim_id,
        claim_payload=claim.model_dump(mode="python"),
        thread_id=thread_id,
        started_at=_now(),
        pipeline_version=PIPELINE_VERSION,
        # Accumulated fields — must initialise to empty containers
        stage_outputs={},
        stage_meta={},
        all_decisions=[],
        all_advisories=[],
        pending_hitl=[],
        hitl_decisions={},
        # Routing signals — sensible defaults
        stp_eligible=False,
        fraud_band="LOW",
        fraud_risk_score=0.0,
        payment_hold_flag=False,
        total_loss_flag=False,
        siu_hold_active=False,
        # Terminal fields — set by s7_subrogation_node
        final_status="OPEN",
        completed_at="",
        graph_error="",
    )


def _make_config(thread_id: str) -> Dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


# ───────────────────────────────────────────────────────────────────────────
# Public API
# ───────────────────────────────────────────────────────────────────────────

def run_claim_langgraph(claim: Claim) -> Dict[str, Any]:
    """Synchronous L3 pipeline execution.

    Runs the full graph from START to END. HITL gates pass through
    automatically (interrupt() is not reached because the graph completes
    without any adjuster action needed — this is equivalent to the L2
    behaviour and is appropriate for clean STP claims and automated testing).

    For claims that DO reach an interrupt gate (T4, CRITICAL fraud, tender
    limits), use create_claim_thread() + resume_thread() instead.

    Returns a dict in the canonical pipeline trace format, compatible with
    all L2 downstream consumers (Co-Pilot, A11, SIU, Governance).
    """
    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError(
            "langgraph not installed. "
            "pip install langgraph langgraph-checkpoint-sqlite"
        )
    graph = _get_graph()
    if graph is None:
        raise RuntimeError(
            "LangGraph graph failed to compile. Check server logs for details. "
            "Try: pip install --upgrade langgraph langgraph-checkpoint-sqlite"
        )
    thread_id = f"TH-{uuid.uuid4().hex[:12].upper()}"
    config    = _make_config(thread_id)
    init      = _initial_state(claim, thread_id)
    t0        = time.time()

    try:
        final_state = graph.invoke(init, config=config)
    except Exception as exc:
        log.error("LangGraph invoke failed for claim %s: %s", claim.claim_id, exc)
        raise

    elapsed_ms = int((time.time() - t0) * 1000)
    return _state_to_trace(final_state, init["started_at"], elapsed_ms)


def create_claim_thread(claim: Claim) -> Dict[str, Any]:
    """Start an L3 claim thread.  Returns after the first interrupt or END.

    Creates a new thread_id, initialises the graph state, and streams the
    graph forward until it either:
      a) Completes (hits END) — returns the full pipeline trace.
      b) Hits an interrupt() — returns the thread_id + interrupt payload.

    The caller uses the thread_id to:
      - Poll graph state:  GET /api/v1/fnol/v3/claims/{thread_id}
      - Resume after HITL: POST /api/v1/fnol/v3/claims/{thread_id}/resume

    This is the preferred entry point for production claims where HITL is
    expected (T4_COMPLEX, CRITICAL fraud, tender limits).
    """
    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError("langgraph not installed.")

    graph = _get_graph()
    if graph is None:
        raise RuntimeError(
            "LangGraph graph failed to compile. "
            "pip install --upgrade langgraph langgraph-checkpoint-sqlite"
        )
    thread_id = f"TH-{uuid.uuid4().hex[:12].upper()}"
    config    = _make_config(thread_id)
    init      = _initial_state(claim, thread_id)
    t0        = time.time()

    interrupted = False
    interrupt_payload: Any = None
    final_state: Optional[Dict[str, Any]] = None

    try:
        # stream() yields {"node_name": state_update} after each node.
        # We consume until StopIteration (END) or an interrupt is detected.
        for chunk in graph.stream(init, config=config, stream_mode="updates"):
            # LangGraph surfaces interrupt as a special chunk key
            if "__interrupt__" in chunk:
                interrupted = True
                interrupt_payload = chunk["__interrupt__"]
                break
    except Exception as exc:
        log.error("LangGraph stream failed for %s: %s", claim.claim_id, exc)
        raise

    elapsed_ms = int((time.time() - t0) * 1000)

    if interrupted:
        return {
            "thread_id":   thread_id,
            "claim_id":    claim.claim_id,
            "status":      "AWAITING_HITL",
            "interrupt":   interrupt_payload,
            "elapsed_ms":  elapsed_ms,
            "orchestrator": "langgraph-v3",
            "message": (
                "Graph suspended at HITL gate. "
                f"POST /api/v1/fnol/v3/claims/{thread_id}/resume "
                "with {decision: APPROVED, ...} to continue."
            ),
        }

    # Graph completed — retrieve final state from checkpointer
    final_state_snapshot = graph.get_state(config).values
    return _state_to_trace(
        FNOLGraphState(**final_state_snapshot),
        init["started_at"], elapsed_ms
    )


def resume_thread(thread_id: str, decision: Dict[str, Any]) -> Dict[str, Any]:
    """Resume a graph suspended at a HITL interrupt.

    Args:
        thread_id: The thread identifier returned by create_claim_thread().
        decision:  The adjuster's response dict, e.g.:
                   {"decision": "APPROVED", "adjuster_id": "ADJ-001"}
                   {"disposition": "CLEARED", "investigator_notes": "..."}
                   {"decision": "ADJUST_OFFER", "approved_bi_usd": 45000}

    The graph resumes from the checkpointed state with `decision` injected
    as the return value of the interrupt() call inside the gate node.

    Returns the pipeline trace if the graph runs to END, or the new interrupt
    payload if another gate is encountered downstream.
    """
    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError("langgraph not installed.")

    graph  = _get_graph()
    config = _make_config(thread_id)
    t0     = time.time()

    interrupted = False
    interrupt_payload: Any = None

    try:
        # Command(resume=value) injects `value` as the return of interrupt()
        # and continues graph execution from the suspended node.
        for chunk in graph.stream(
            Command(resume=decision), config=config, stream_mode="updates"
        ):
            if "__interrupt__" in chunk:
                interrupted = True
                interrupt_payload = chunk["__interrupt__"]
                break
    except Exception as exc:
        log.error("LangGraph resume failed for thread %s: %s", thread_id, exc)
        raise

    elapsed_ms = int((time.time() - t0) * 1000)
    snapshot = graph.get_state(config)
    state_values = FNOLGraphState(**snapshot.values)

    if interrupted:
        return {
            "thread_id":   thread_id,
            "claim_id":    state_values.get("claim_id", ""),
            "status":      "AWAITING_HITL",
            "interrupt":   interrupt_payload,
            "elapsed_ms":  elapsed_ms,
            "orchestrator": "langgraph-v3",
        }

    return _state_to_trace(state_values, state_values.get("started_at", ""), elapsed_ms)


def get_thread_state(thread_id: str) -> Dict[str, Any]:
    """Retrieve the current graph state for a thread.

    Returns the latest checkpointed state including all stage outputs,
    routing signals, HITL status, and final_status.  Works for completed,
    interrupted, and in-progress threads.

    Raises KeyError if thread_id is unknown to the checkpointer.
    """
    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError("langgraph not installed.")

    graph = _get_graph()
    if graph is None:
        raise RuntimeError("LangGraph graph not available — check server logs")
    config   = _make_config(thread_id)
    snapshot = graph.get_state(config)
    if snapshot is None or not snapshot.values:
        raise KeyError(f"Thread {thread_id} not found in checkpoint store")

    state_values = FNOLGraphState(**snapshot.values)
    next_nodes = list(snapshot.next) if snapshot.next else []

    return {
        "thread_id":       thread_id,
        "claim_id":        state_values.get("claim_id", ""),
        "final_status":    state_values.get("final_status", "OPEN"),
        "fraud_band":      state_values.get("fraud_band", ""),
        "payment_hold":    state_values.get("payment_hold_flag", False),
        "siu_hold_active": state_values.get("siu_hold_active", False),
        "pending_hitl":    state_values.get("pending_hitl", []),
        "hitl_decisions":  state_values.get("hitl_decisions", {}),
        "completed_at":    state_values.get("completed_at", ""),
        "next_nodes":      next_nodes,
        "stage_meta":      state_values.get("stage_meta", {}),
        "orchestrator":    "langgraph-v3",
    }


def list_threads(limit: int = 50) -> List[Dict[str, Any]]:
    """List active / recent threads from the checkpoint store."""
    if not LANGGRAPH_AVAILABLE:
        return []
    graph = _get_graph()
    if graph is None:
        return []   # graph failed to build — return empty list, not 500
    threads: List[Dict[str, Any]] = []
    try:
        for item in graph.checkpointer.list(config=None, limit=limit):
            tid = item.config.get("configurable", {}).get("thread_id", "")
            vals = item.checkpoint.get("channel_values", {})
            threads.append({
                "thread_id":    tid,
                "claim_id":     vals.get("claim_id", ""),
                "final_status": vals.get("final_status", "OPEN"),
                "fraud_band":   vals.get("fraud_band", ""),
                "pending_hitl": vals.get("pending_hitl", []),
                "completed_at": vals.get("completed_at", ""),
            })
    except Exception as exc:
        log.warning("list_threads failed: %s", exc)
    return threads[:limit]


def get_graph_health() -> Dict[str, Any]:
    """LangGraph engine health summary."""
    if not LANGGRAPH_AVAILABLE:
        return {
            "status":     "unavailable",
            "reason":     "langgraph not installed — pip install langgraph langgraph-checkpoint-sqlite",
            "l3_enabled": False,
        }
    graph = _get_graph()
    db_path = Path(getattr(settings, "langgraph_db_path", "fnol_langgraph_checkpoint.db"))
    if graph is None:
        # langgraph is installed but graph compilation failed (wrong SqliteSaver
        # API version, file permission error, import failure in a node function, etc.)
        return {
            "status":     "build_failed",
            "l3_enabled": False,
            "reason":     (
                "LangGraph is installed but the graph failed to compile. "
                "Check server logs for the _build_graph error. "
                "Common causes: wrong SqliteSaver import path for your langgraph version, "
                "file permission on checkpoint DB, or a node import error. "
                "Try: pip install --upgrade langgraph langgraph-checkpoint-sqlite"
            ),
            "checkpoint_db":      str(db_path),
            "pipeline_version":   f"{PIPELINE_VERSION}-lg3",
            "langgraph_available": LANGGRAPH_AVAILABLE,
        }
    try:
        node_names = list(graph.nodes.keys())
    except Exception:
        node_names = []
    return {
        "status":       "ok",
        "l3_enabled":   True,
        "graph_nodes":  node_names,
        "checkpointer": "SqliteSaver",
        "checkpoint_db": str(db_path),
        "pipeline_version": f"{PIPELINE_VERSION}-lg3",
        "langgraph_available": LANGGRAPH_AVAILABLE,
    }
