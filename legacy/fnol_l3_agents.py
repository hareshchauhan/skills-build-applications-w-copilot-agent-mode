"""
fnol_l3_agents.py — L3 Maturity Hook Agents
============================================
NEW IN V2 BLUEPRINT (L100 Industrialization-Aware Edition).

V2 Blueprint Section 02 (Industrialization Maturity Model) introduces the
L3 inversion — where the AI Agent + Interface Layer becomes the primary UX
for both claimants and adjusters, with workflow systems collapsed to thin services.

This file provides the two L3-hook agents that wrap the existing 8-agent pipeline:

  L3-A · ConversationalOrchestrationAgent
         Claimant-facing single-session experience — captures, validates,
         decisions, and settles in one continuous conversation across phone,
         app, and web.

  L3-B · AdjusterCoPilotAgent
         Adjuster-facing primary interface that replaces the GWCC desktop
         as the day-to-day work surface. Surfaces decision records,
         pre-briefs the adjuster, drafts comms, and handles HITL flows.

These do NOT change the underlying BPMN — they wrap the L2 pipeline.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fnol_workflow_engine import (
    BaseAgent, DecisionRecord, FNOLPayload, MaturityLevel,
    ENGINE_VERSION, now_iso, log,
)


# ════════════════════════════════════════════════════════════════════════════════
# L3-A · CONVERSATIONAL ORCHESTRATION AGENT
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class ConvTurn:
    role: str                    # claimant | agent | system
    text: str
    timestamp: str
    intent: Optional[str] = None
    extractedFields: Dict[str, Any] = field(default_factory=dict)
    nextAction: Optional[str] = None


@dataclass
class ConvSessionState:
    sessionId: str
    claimNumber: Optional[str]
    policyNumber: Optional[str]
    channel: str                 # PHONE | APP | WEB | CHAT
    state: str                   # GREETING | GATHERING | VALIDATING | DECISIONING | SETTLING | CLOSED
    capturedFields: Dict[str, Any]
    pendingFields: List[str]
    turns: List[ConvTurn]
    startedAt: str
    lastTurnAt: str
    completionPct: float
    safetyCheckCompleted: bool   # "Are you safe?" — V2 §03 first action
    handoffToHumanRequested: bool


class ConversationalOrchestrationAgent(BaseAgent):
    """
    L3-A — Single-session conversational experience for claimants.

    Lifecycle:
      1. GREETING       - safety check ("Are you safe?")
      2. GATHERING      - structured Q&A to fill canonical FNOL fields
      3. VALIDATING     - confirm extracted fields with claimant
      4. DECISIONING    - kick off L2 pipeline (intake → coverage → triage → fraud → damage → BI)
      5. SETTLING       - if STP-eligible, present offer & e-sign release in-session
      6. CLOSED         - claim either settled or routed to adjuster co-pilot

    Required L2 fields (V2 Blueprint canonical FNOL Payload):
      policyNumber · lossDateTime · lossLocation · lossDescription · state ·
      vehicles · parties · injuriesReported · policeReported
    """
    name = "ConversationalOrchestrationAgent"
    REQUIRED_FIELDS = [
        "policyNumber","state","lossDateTime","lossLocation",
        "lossDescription","vehicleInfo","injurySummary","policeReported",
    ]

    def __init__(self, sor=None, maturity=MaturityLevel.L3):
        super().__init__(sor=sor, maturity=maturity)
        self.sessions: Dict[str, ConvSessionState] = {}

    # ----------------------------------------------------------------- start
    def start_session(self, channel: str = "WEB",
                      policy_number: Optional[str] = None) -> ConvSessionState:
        sid = f"CONV-{uuid.uuid4().hex[:10]}"
        sess = ConvSessionState(
            sessionId=sid, claimNumber=None, policyNumber=policy_number,
            channel=channel, state="GREETING",
            capturedFields={}, pendingFields=list(self.REQUIRED_FIELDS),
            turns=[], startedAt=now_iso(), lastTurnAt=now_iso(),
            completionPct=0.0, safetyCheckCompleted=False, handoffToHumanRequested=False,
        )
        # opening turn — V2 Blueprint §03 mandate: safety first
        opening = ConvTurn(
            role="agent",
            text="Hi — I'm your claims assistant. First, are you and everyone else safe? "
                 "If you need emergency help, please call 911 right now.",
            timestamp=now_iso(), intent="SAFETY_CHECK",
            nextAction="WAIT_FOR_SAFETY_RESPONSE",
        )
        sess.turns.append(opening)
        self.sessions[sid] = sess
        return sess

    # ---------------------------------------------------------------- turn
    def process_turn(self, session_id: str, claimant_text: str) -> ConvSessionState:
        sess = self.sessions.get(session_id)
        if not sess:
            raise KeyError(f"unknown session {session_id}")

        # log the claimant turn
        cl_turn = ConvTurn(role="claimant", text=claimant_text, timestamp=now_iso(),
                           intent=self._infer_intent(claimant_text, sess))
        sess.turns.append(cl_turn)

        # state machine
        text_l = claimant_text.lower()
        if sess.state == "GREETING":
            if any(t in text_l for t in ["yes","safe","okay","fine","ok","i'm okay","i am safe"]):
                sess.safetyCheckCompleted = True
                sess.state = "GATHERING"
                ack = ConvTurn(role="agent",
                    text="Glad you're safe. Let's get the basics. What's your policy number, "
                         "and roughly when did the incident happen?",
                    timestamp=now_iso(), intent="ASK_POLICY_AND_TIME",
                    nextAction="EXTRACT_POLICY_AND_TIME")
                sess.turns.append(ack)
            elif any(t in text_l for t in ["no","not safe","emergency","911","help"]):
                sess.handoffToHumanRequested = True
                sess.state = "CLOSED"
                ack = ConvTurn(role="agent",
                    text="Please call 911 immediately. I'm transferring you to a senior adjuster who will help you.",
                    timestamp=now_iso(), intent="EMERGENCY_HANDOFF", nextAction="HUMAN_HANDOFF")
                sess.turns.append(ack)
        elif sess.state == "GATHERING":
            self._extract_into_session(sess, claimant_text)
            self._next_question(sess)
        elif sess.state == "VALIDATING":
            if any(t in text_l for t in ["yes","correct","right","that's right","confirm","confirmed"]):
                sess.state = "DECISIONING"
                ack = ConvTurn(role="agent",
                    text="Confirmed. I'm running the analysis now — coverage, triage, fraud check, and damage. "
                         "Give me a few seconds.",
                    timestamp=now_iso(), intent="DECISIONING_KICKOFF",
                    nextAction="RUN_L2_PIPELINE")
                sess.turns.append(ack)
            else:
                sess.state = "GATHERING"
                ack = ConvTurn(role="agent",
                    text="Got it — what should I correct?",
                    timestamp=now_iso(), intent="ASK_CORRECTION",
                    nextAction="ACCEPT_CORRECTION")
                sess.turns.append(ack)
        elif sess.state == "DECISIONING":
            ack = ConvTurn(role="agent",
                text="Still processing — just a moment.",
                timestamp=now_iso(), intent="HOLD",
                nextAction="WAIT_FOR_PIPELINE")
            sess.turns.append(ack)
        elif sess.state == "SETTLING":
            if any(t in text_l for t in ["yes","accept","i accept","approved","sign"]):
                ack = ConvTurn(role="agent",
                    text="Accepted. Payment will be released to your account on file within one business day. "
                         "You'll receive an email with the release form.",
                    timestamp=now_iso(), intent="SETTLEMENT_ACCEPTED",
                    nextAction="CLOSE_SESSION")
                sess.turns.append(ack)
                sess.state = "CLOSED"
            elif any(t in text_l for t in ["no","reject","decline","more","negotiate","attorney"]):
                sess.handoffToHumanRequested = True
                ack = ConvTurn(role="agent",
                    text="Understood — I'll route you to a human adjuster who can negotiate.",
                    timestamp=now_iso(), intent="SETTLEMENT_DECLINED",
                    nextAction="HUMAN_HANDOFF")
                sess.turns.append(ack)
                sess.state = "CLOSED"

        sess.lastTurnAt = now_iso()
        sess.completionPct = self._completion(sess)
        return sess

    # ------------------------------------------------------- present settlement
    def present_settlement(self, session_id: str, net_payable: float,
                           method: str = "ACH") -> ConvSessionState:
        sess = self.sessions[session_id]
        sess.state = "SETTLING"
        offer = ConvTurn(
            role="agent",
            text=f"Good news — based on your policy and the damage estimate, we can settle "
                 f"this claim immediately for ${net_payable:,.2f} via {method}. "
                 f"Do you accept?",
            timestamp=now_iso(), intent="PRESENT_OFFER",
            nextAction="WAIT_FOR_ACCEPT_DECLINE",
        )
        sess.turns.append(offer)
        sess.lastTurnAt = now_iso()
        return sess

    # ------------------------------------------------------------- helpers
    def _extract_into_session(self, sess: ConvSessionState, text: str) -> None:
        # naive policy number / state / time / vehicle / injuries / police
        m = re.search(r"\b(POC-POL-\d{5}|POL-?\d{5,8})\b", text, re.I)
        if m:
            sess.capturedFields["policyNumber"] = m.group(0).upper()
            sess.policyNumber = m.group(0).upper()
        m = re.search(r"\b([A-Z]{2})\b", text)
        if m and len(m.group(0)) == 2:
            sess.capturedFields["state"] = m.group(0)
        if "yesterday" in text.lower():
            sess.capturedFields["lossDateTime"] = "yesterday"
        if any(k in text.lower() for k in ["police","officer","report"]):
            sess.capturedFields["policeReported"] = True
        if any(k in text.lower() for k in ["injured","hurt","pain","whiplash","ER","ambulance"]):
            sess.capturedFields["injurySummary"] = "yes"
        if any(k in text.lower() for k in ["honda","toyota","tesla","ford","chevy","bmw"]):
            sess.capturedFields["vehicleInfo"] = text[:120]
        if "lossDescription" not in sess.capturedFields and len(text) > 30:
            sess.capturedFields["lossDescription"] = text[:300]
        if any(k in text.lower() for k in ["highway","intersection","i-","street","road","parking"]):
            sess.capturedFields["lossLocation"] = text[:120]
        sess.pendingFields = [f for f in self.REQUIRED_FIELDS if f not in sess.capturedFields]

    def _next_question(self, sess: ConvSessionState) -> None:
        if not sess.pendingFields:
            sess.state = "VALIDATING"
            ack = ConvTurn(role="agent",
                text=f"Quick recap to confirm: policy {sess.capturedFields.get('policyNumber','?')}, "
                     f"in {sess.capturedFields.get('state','?')}, "
                     f"loss at {sess.capturedFields.get('lossLocation','an unspecified location')}, "
                     f"police-reported={sess.capturedFields.get('policeReported',False)}, "
                     f"injuries={sess.capturedFields.get('injurySummary','none')}. Is that right?",
                timestamp=now_iso(), intent="CONFIRM_RECAP",
                nextAction="WAIT_FOR_CONFIRMATION")
            sess.turns.append(ack)
            return
        nxt = sess.pendingFields[0]
        question_map = {
            "policyNumber":  "What's your policy number?",
            "state":         "What state did this happen in?",
            "lossDateTime":  "When did the incident happen — date and time?",
            "lossLocation":  "Where exactly did it happen — street, highway, or intersection?",
            "lossDescription":"In your own words, what happened?",
            "vehicleInfo":   "What's your vehicle — year, make, and model?",
            "injurySummary": "Was anyone hurt? Even minor pain matters.",
            "policeReported":"Did police come to the scene? If yes, do you have a report number?",
        }
        ack = ConvTurn(role="agent", text=question_map.get(nxt, "Can you tell me more?"),
                       timestamp=now_iso(), intent=f"ASK_{nxt.upper()}", nextAction=f"EXTRACT_{nxt}")
        sess.turns.append(ack)

    def _completion(self, sess: ConvSessionState) -> float:
        if not self.REQUIRED_FIELDS:
            return 1.0
        return round(len(sess.capturedFields) / len(self.REQUIRED_FIELDS), 2)

    @staticmethod
    def _infer_intent(text: str, sess: ConvSessionState) -> str:
        text_l = text.lower()
        if any(t in text_l for t in ["transfer","human","agent","representative","speak to someone"]):
            return "REQUEST_HUMAN"
        if any(t in text_l for t in ["yes","correct","confirm"]):
            return "CONFIRM"
        if any(t in text_l for t in ["no","wrong","incorrect"]):
            return "DENY"
        if any(t in text_l for t in ["help","emergency","911"]):
            return "EMERGENCY"
        return "PROVIDE_INFO"


# ════════════════════════════════════════════════════════════════════════════════
# L3-B · ADJUSTER CO-PILOT AGENT
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class CoPilotBrief:
    claimNumber: str
    summary: str                         # AI-drafted 3-sentence summary
    keyDecisions: List[Dict[str, Any]]   # decision records to review
    redFlags: List[str]                  # fraud, BI, coverage gaps
    suggestedActions: List[Dict[str, Any]]
    draftedCommunications: List[Dict[str, Any]]
    slaStatus: Dict[str, Any]            # ack, ROR, statute clocks
    confidence: float
    decisionRecord: DecisionRecord


class AdjusterCoPilotAgent(BaseAgent):
    """
    L3-B — Adjuster's primary work surface.
    Replaces GWCC/Duck Creek desktop as day-to-day UX. The SOR remains
    authoritative; co-pilot writes back through the L5 integration plane.

    Capabilities:
      - Pre-brief (3-sentence claim summary on open)
      - Surface red flags (fraud, BI escalation, coverage gap, SLA breach risk)
      - Suggested actions (next-best-action recommendations)
      - Drafted communications (ack letter, ROR, demand response)
      - SLA clocks (ack 60s, ROR window, statute of limitations)
    """
    name = "AdjusterCoPilotAgent"

    def brief(self, payload: FNOLPayload, *,
              triage_track: str = "T2",
              fraud_band: str = "LOW",
              fraud_score: float = 0.0,
              coverage_decision: str = "COVERED",
              ror_required: bool = False,
              damage_decision: str = "REPAIR",
              ai_estimate: float = 0.0,
              bi_p50: float = 0.0,
              bi_attorney: bool = False,
              hitl_count: int = 0) -> CoPilotBrief:
        t0 = time.time()

        # 3-sentence summary (in production: Claude Sonnet w/ JSON-mode template)
        v = payload.vehicles[0] if payload.vehicles else None
        veh_str = f"{v.year} {v.make} {v.model}" if v else "vehicle (unspecified)"
        summary = (
            f"{payload.claimNumber} · {payload.state} · {payload.lossType} on {veh_str}. "
            f"Triage track {triage_track}, fraud band {fraud_band} ({fraud_score:.2f}), "
            f"coverage {coverage_decision}, damage {damage_decision} (AI est. ${ai_estimate:,.0f}). "
            f"{'Attorney represented; BI exposure $' + format(bi_p50, ',.0f') + '. ' if bi_attorney and bi_p50 else ''}"
            f"{hitl_count} HITL touchpoints pending."
        )

        # Red flags
        red_flags: List[str] = []
        if fraud_band in ("HIGH","CRITICAL"):
            red_flags.append(f"FRAUD {fraud_band} — payment hold active; SIU referral if CRITICAL")
        if coverage_decision == "DENIED":
            red_flags.append("COVERAGE DENIED — denial letter required within state window")
        if ror_required:
            red_flags.append("ROR LETTER REQUIRED — generate within statutory window")
        if bi_p50 > 250_000:
            red_flags.append(f"HIGH BI EXPOSURE — ${bi_p50:,.0f} estimated; senior adjuster")
        if bi_attorney:
            red_flags.append("ATTORNEY REPRESENTED — all comms via legal channel")
        if damage_decision == "TOTAL_LOSS":
            red_flags.append("TOTAL LOSS — title work, salvage, and rental cap")
        if any(p.attorneyRetained for p in payload.parties):
            red_flags.append("THIRD-PARTY ATTORNEY — preserve subrogation evidence")

        # Suggested actions (next-best-action)
        actions: List[Dict[str, Any]] = []
        if coverage_decision == "DENIED":
            actions.append({"action":"DRAFT_DENIAL_LETTER","priority":"P0","sla":"24h"})
        elif ror_required:
            actions.append({"action":"DRAFT_ROR_LETTER","priority":"P0","sla":"24h"})
        if fraud_band in ("HIGH","CRITICAL"):
            actions.append({"action":"OPEN_SIU_REFERRAL","priority":"P0","sla":"4h"})
        if damage_decision == "TOTAL_LOSS":
            actions.append({"action":"REQUEST_SALVAGE_TITLE","priority":"P1","sla":"48h"})
        if bi_p50 > 75_000 and not bi_attorney:
            actions.append({"action":"SCHEDULE_RECORDED_STATEMENT","priority":"P1","sla":"72h"})
        if not actions:
            actions.append({"action":"REVIEW_AI_DECISIONS","priority":"P2","sla":"24h"})

        # Drafted comms (templates)
        comms: List[Dict[str, Any]] = []
        comms.append({
            "kind":"DOI_ACKNOWLEDGMENT",
            "to":(payload.parties[0].firstName + " " + payload.parties[0].lastName).strip() if payload.parties else "Insured",
            "subject":f"Claim {payload.claimNumber} — Acknowledgment",
            "body":f"We have received your claim {payload.claimNumber} reported on "
                   f"{payload.submittedAt}. A claim representative will contact you "
                   f"within 24 hours.",
            "channel":"EMAIL",
            "scheduledAt":now_iso(),
        })
        if ror_required:
            comms.append({
                "kind":"RESERVATION_OF_RIGHTS",
                "to":(payload.parties[0].firstName + " " + payload.parties[0].lastName).strip() if payload.parties else "Insured",
                "subject":f"Claim {payload.claimNumber} — Reservation of Rights",
                "body":f"We are continuing to investigate the loss of {payload.lossDateTime}. "
                       f"This letter reserves all rights under your policy pending completion "
                       f"of our investigation. Please retain all relevant documentation.",
                "channel":"CERTIFIED_MAIL",
                "scheduledAt":now_iso(),
            })
        if damage_decision == "DISPATCH_DRP":
            comms.append({
                "kind":"DRP_DISPATCH",
                "to":(payload.parties[0].firstName + " " + payload.parties[0].lastName).strip() if payload.parties else "Insured",
                "subject":f"Claim {payload.claimNumber} — Repair Shop Assignment",
                "body":f"A repair shop in our Direct Repair Program has been assigned. "
                       f"They will contact you within 24 hours to schedule repair.",
                "channel":"EMAIL+SMS",
                "scheduledAt":now_iso(),
            })

        # SLA clocks
        sla = {
            "ackWithin60s": True,
            "rorWindowHrs": 720 if ror_required else None,
            "statuteOfLimitationsDays": 730 if payload.state in ("TX","CA","FL","GA","NY") else 1095,
            "lossNotificationToFNOLMs": None,
        }

        confidence = 0.85
        if hitl_count > 5: confidence -= 0.05
        if fraud_band == "CRITICAL": confidence -= 0.05

        dr = self.emit(
            claim=payload.claimNumber, dtype="COPILOT_BRIEF",
            value={"redFlags":len(red_flags),"actions":len(actions),"comms":len(comms)},
            conf=confidence, inputs={"track":triage_track,"fraud":fraud_band},
            hitl=False, explanation=f"Co-pilot brief generated in {(time.time()-t0)*1000:.0f}ms",
            model_version="copilot-v1-mock",
        )

        return CoPilotBrief(
            claimNumber=payload.claimNumber, summary=summary,
            keyDecisions=[], redFlags=red_flags,
            suggestedActions=actions, draftedCommunications=comms,
            slaStatus=sla, confidence=round(confidence, 2),
            decisionRecord=dr,
        )


__all__ = [
    "ConversationalOrchestrationAgent","ConvSessionState","ConvTurn",
    "AdjusterCoPilotAgent","CoPilotBrief",
]
