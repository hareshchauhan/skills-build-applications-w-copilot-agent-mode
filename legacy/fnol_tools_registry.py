"""
fnol_tools_registry.py — Native tool registry
==============================================
Every agent capability exposed as a native callable for use by:
  - LangChain (fnol_tools_langchain.py wraps these)
  - MCP server (fnol_tools_mcp.py wraps these)
  - Claude tool-use API (server-side — register these as tools)

A registered tool is the canonical way to invoke an agent. Tools are
maturity-aware: passing maturity=L1/L2/L3 changes agent behavior.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Dict, List, Optional

from fnol_workflow_engine import (
    FNOLPayload, TelematicsSignal, MaturityLevel,
    FNOLIntakeAgent, TriageAssignmentAgent, FraudSignalDetectionAgent,
    get_sor_adapter,
)
from coverage_agent import CoverageVerificationAgent
from fnol_agents_ext import (
    DamageEstimationAgent, BIEvaluationAgent,
    SettlementAgent, SubrogationAgent,
)
from fnol_l3_agents import ConversationalOrchestrationAgent, AdjusterCoPilotAgent


def _to_dict(obj: Any) -> Any:
    if obj is None: return None
    if is_dataclass(obj): return asdict(obj)
    if isinstance(obj, list): return [_to_dict(o) for o in obj]
    if isinstance(obj, dict): return {k:_to_dict(v) for k,v in obj.items()}
    return obj


# ════════════════════════════════════════════════════════════════════════════════
# REGISTRY
# ════════════════════════════════════════════════════════════════════════════════

class ToolRegistry:
    def __init__(self, sor=None, maturity: str = "L2"):
        self.sor = sor or get_sor_adapter()
        self.maturity = MaturityLevel(maturity) if isinstance(maturity, str) else maturity
        self._build()

    def _build(self) -> None:
        self.intake = FNOLIntakeAgent(sor=self.sor, maturity=self.maturity)
        self.coverage = CoverageVerificationAgent(sor=self.sor, maturity=self.maturity)
        self.triage = TriageAssignmentAgent(sor=self.sor, maturity=self.maturity)
        self.fraud = FraudSignalDetectionAgent(sor=self.sor, maturity=self.maturity)
        self.damage = DamageEstimationAgent(sor=self.sor, maturity=self.maturity)
        self.bi = BIEvaluationAgent(sor=self.sor, maturity=self.maturity)
        self.settle = SettlementAgent(sor=self.sor, maturity=self.maturity)
        self.subro = SubrogationAgent(sor=self.sor, maturity=self.maturity)
        self.conv = ConversationalOrchestrationAgent(sor=self.sor, maturity=self.maturity)
        self.copilot = AdjusterCoPilotAgent(sor=self.sor, maturity=self.maturity)
        self._tools = {t["name"]: t for t in self.tool_specs()}

    def set_maturity(self, maturity: str) -> None:
        self.maturity = MaturityLevel(maturity)
        self._build()

    # ------------------------------------------------------------- tool specs
    def tool_specs(self) -> List[Dict[str, Any]]:
        return [
            {
                "name":"fnol_intake",
                "description":"A1 — Extract structured FNOL fields from claimant narrative; "
                              "writes to SOR; returns canonical payload + completeness score.",
                "parameters":{
                    "type":"object",
                    "properties":{
                        "narrative":{"type":"string"},
                        "policyNumber":{"type":"string"},
                        "state":{"type":"string"},
                        "channel":{"type":"string"},
                    },
                    "required":["narrative","policyNumber"],
                },
                "fn": self.tool_intake,
            },
            {
                "name":"coverage_verify",
                "description":"A2 — Verify coverage in-force; map lossType→clauses; flag ROR.",
                "parameters":{
                    "type":"object",
                    "properties":{"payload":{"type":"object"}},
                    "required":["payload"],
                },
                "fn": self.tool_coverage,
            },
            {
                "name":"triage_route",
                "description":"A3 — Score complexity (5 dimensions) and route to track (STP/T1/T2/T3/T4/CAT/SIU).",
                "parameters":{
                    "type":"object",
                    "properties":{"payload":{"type":"object"},"fraudScore":{"type":"number"},
                                  "coverageComplexity":{"type":"number"},"estLossUSD":{"type":"number"}},
                    "required":["payload"],
                },
                "fn": self.tool_triage,
            },
            {
                "name":"fraud_score",
                "description":"A4 — 40-signal fraud composite scoring; returns band + payment-hold flag.",
                "parameters":{
                    "type":"object",
                    "properties":{"payload":{"type":"object"},"priorClaims":{"type":"integer"},
                                  "isoHit":{"type":"boolean"},"attorneyRetained":{"type":"boolean"}},
                    "required":["payload"],
                },
                "fn": self.tool_fraud,
            },
            {
                "name":"damage_assess",
                "description":"A5 — Damage estimation (CV + state total-loss threshold); REPAIR vs TOTAL_LOSS.",
                "parameters":{
                    "type":"object",
                    "properties":{"payload":{"type":"object"},"presentedEstimate":{"type":"number"}},
                    "required":["payload"],
                },
                "fn": self.tool_damage,
            },
            {
                "name":"bi_evaluate",
                "description":"A6 — Bodily injury evaluation; Claude Opus 200K medical record summary.",
                "parameters":{
                    "type":"object",
                    "properties":{"payload":{"type":"object"},"faultPct":{"type":"number"},
                                  "attorneyRetained":{"type":"boolean"}},
                    "required":["payload"],
                },
                "fn": self.tool_bi,
            },
            {
                "name":"settle",
                "description":"A7 — Settlement decisioning; auto-approve PD ≤ $15K otherwise HITL.",
                "parameters":{
                    "type":"object",
                    "properties":{"payload":{"type":"object"},"damageEstimate":{"type":"number"},
                                  "biP50":{"type":"number"},"deductible":{"type":"number"},
                                  "fraudBand":{"type":"string"},"coverageDecision":{"type":"string"},
                                  "paymentHold":{"type":"boolean"}},
                    "required":["payload"],
                },
                "fn": self.tool_settle,
            },
            {
                "name":"subrogation",
                "description":"A8 — Identify subrogation opportunity at FNOL; ≥80% capture target.",
                "parameters":{
                    "type":"object",
                    "properties":{"payload":{"type":"object"},"faultPct":{"type":"number"},"paidAmount":{"type":"number"}},
                    "required":["payload"],
                },
                "fn": self.tool_subro,
            },
            # L3 hooks
            {
                "name":"conv_session_start",
                "description":"L3-A — Start a claimant-facing conversational orchestration session.",
                "parameters":{"type":"object",
                              "properties":{"channel":{"type":"string"},"policyNumber":{"type":"string"}}},
                "fn": self.tool_conv_start,
            },
            {
                "name":"conv_turn",
                "description":"L3-A — Process a claimant message in an active conversational session.",
                "parameters":{"type":"object",
                              "properties":{"sessionId":{"type":"string"},"text":{"type":"string"}},
                              "required":["sessionId","text"]},
                "fn": self.tool_conv_turn,
            },
            {
                "name":"copilot_brief",
                "description":"L3-B — Generate adjuster co-pilot brief (summary, red flags, drafted comms).",
                "parameters":{"type":"object",
                              "properties":{"payload":{"type":"object"},"triageTrack":{"type":"string"},
                                            "fraudBand":{"type":"string"},"coverageDecision":{"type":"string"},
                                            "damageDecision":{"type":"string"},"aiEstimate":{"type":"number"},
                                            "biP50":{"type":"number"}},
                              "required":["payload"]},
                "fn": self.tool_copilot,
            },
        ]

    # ----------------------------------------------------------- tool callables
    @staticmethod
    def _payload_from_dict(d: Dict[str, Any]) -> FNOLPayload:
        tel_d = d.get("telematics")
        tel = TelematicsSignal(**tel_d) if tel_d else None
        from fnol_workflow_engine import Vehicle, Party, Injury
        veh = [Vehicle(**v) for v in d.get("vehicles",[])]
        par = [Party(**p) for p in d.get("parties",[])]
        inj = [Injury(**i) for i in d.get("injuriesReported",[])]
        kwargs = {k:v for k,v in d.items() if k not in ("telematics","vehicles","parties","injuriesReported")}
        return FNOLPayload(telematics=tel, vehicles=veh, parties=par, injuriesReported=inj, **kwargs)

    def tool_intake(self, narrative: str, policyNumber: str, state: str = "",
                    channel: str = "WEB", telematics: Optional[Dict[str, Any]] = None,
                    photos: Optional[List[str]] = None) -> Dict[str, Any]:
        tel = TelematicsSignal(**telematics) if telematics else None
        result = self.intake.process(narrative=narrative, policy_number=policyNumber,
                                     state=state, channel=channel, telematics=tel, photos=photos)
        return _to_dict(result)

    def tool_coverage(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return _to_dict(self.coverage.process(self._payload_from_dict(payload)))

    def tool_triage(self, payload: Dict[str, Any], fraudScore: float = 0.0,
                    coverageComplexity: float = 0.3, estLossUSD: float = 0.0) -> Dict[str, Any]:
        return _to_dict(self.triage.process(
            self._payload_from_dict(payload), fraud_score=fraudScore,
            coverage_complexity=coverageComplexity, est_loss_usd=estLossUSD))

    def tool_fraud(self, payload: Dict[str, Any], priorClaims: int = 0,
                   isoHit: bool = False, attorneyRetained: bool = False) -> Dict[str, Any]:
        return _to_dict(self.fraud.process(
            self._payload_from_dict(payload), prior_claims_count=priorClaims,
            iso_hit=isoHit, attorney_retained=attorneyRetained))

    def tool_damage(self, payload: Dict[str, Any], presentedEstimate: float = 0.0) -> Dict[str, Any]:
        p = self._payload_from_dict(payload)
        return _to_dict(self.damage.process(p, presented_estimate=presentedEstimate or None))

    def tool_bi(self, payload: Dict[str, Any], faultPct: float = 0.0,
                attorneyRetained: Optional[bool] = None) -> Dict[str, Any]:
        return _to_dict(self.bi.process(self._payload_from_dict(payload),
                                        fault_pct=faultPct, attorney_retained=attorneyRetained))

    def tool_settle(self, payload: Dict[str, Any], damageEstimate: float = 0.0,
                    biP50: float = 0.0, deductible: float = 0.0,
                    fraudBand: str = "LOW", coverageDecision: str = "COVERED",
                    paymentHold: bool = False) -> Dict[str, Any]:
        return _to_dict(self.settle.process(
            self._payload_from_dict(payload), damage_estimate=damageEstimate,
            bi_p50=biP50, deductible=deductible, fraud_band=fraudBand,
            coverage_decision=coverageDecision, payment_hold=paymentHold))

    def tool_subro(self, payload: Dict[str, Any], faultPct: float = 1.0,
                   paidAmount: float = 0.0) -> Dict[str, Any]:
        return _to_dict(self.subro.process(self._payload_from_dict(payload),
                                           fault_pct=faultPct, paid_amount=paidAmount))

    def tool_conv_start(self, channel: str = "WEB",
                        policyNumber: Optional[str] = None) -> Dict[str, Any]:
        return _to_dict(self.conv.start_session(channel=channel, policy_number=policyNumber))

    def tool_conv_turn(self, sessionId: str, text: str) -> Dict[str, Any]:
        return _to_dict(self.conv.process_turn(sessionId, text))

    def tool_copilot(self, payload: Dict[str, Any], **kw) -> Dict[str, Any]:
        return _to_dict(self.copilot.brief(self._payload_from_dict(payload),
                                           triage_track=kw.get("triageTrack","T2"),
                                           fraud_band=kw.get("fraudBand","LOW"),
                                           fraud_score=kw.get("fraudScore",0.0),
                                           coverage_decision=kw.get("coverageDecision","COVERED"),
                                           ror_required=kw.get("rorRequired",False),
                                           damage_decision=kw.get("damageDecision","REPAIR"),
                                           ai_estimate=kw.get("aiEstimate",0.0),
                                           bi_p50=kw.get("biP50",0.0),
                                           bi_attorney=kw.get("biAttorney",False),
                                           hitl_count=kw.get("hitlCount",0)))

    def list_tools(self) -> List[Dict[str, Any]]:
        return [{"name":t["name"],"description":t["description"],"parameters":t["parameters"]}
                for t in self._tools.values()]

    def call(self, name: str, **kwargs) -> Dict[str, Any]:
        if name not in self._tools:
            return {"error": f"unknown tool: {name}"}
        try:
            return self._tools[name]["fn"](**kwargs)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}


__all__ = ["ToolRegistry"]
