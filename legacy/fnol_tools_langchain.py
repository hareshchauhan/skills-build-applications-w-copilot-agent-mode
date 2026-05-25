"""
fnol_tools_langchain.py — LangChain BaseTool wrappers around ToolRegistry.

Aligned to V2 Blueprint (L100 · May 2026 · Industrialization-Aware Edition).
Optional dependency: pip install langchain-core
If LangChain is unavailable, this module falls back to lightweight
duck-typed shims so the rest of the app still imports cleanly.

Usage:
    from fnol_tools_registry import ToolRegistry
    from fnol_tools_langchain import build_langchain_tools
    reg = ToolRegistry()
    tools = build_langchain_tools(reg)            # list[BaseTool]
    # Pass directly to a LangChain agent / executor.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type
import json

try:
    from langchain_core.tools import BaseTool
    from pydantic import BaseModel, Field
    LANGCHAIN_AVAILABLE = True
except Exception:  # pragma: no cover
    LANGCHAIN_AVAILABLE = False

    class BaseModel:  # type: ignore
        pass

    def Field(*a, **kw):  # type: ignore
        return None

    class BaseTool:  # type: ignore
        name: str = ""
        description: str = ""

        def _run(self, *a, **kw):
            raise NotImplementedError

        def run(self, *a, **kw):
            return self._run(*a, **kw)


# ---------------------------------------------------------------------------
# Argument schemas (LangChain prefers pydantic v1/v2 schemas for tool inputs)
# ---------------------------------------------------------------------------

class IntakeArgs(BaseModel):
    narrative: str = Field(..., description="Free-text loss description from claimant.")
    policyNumber: str = Field(..., description="Policy number (e.g., POC-POL-00123).")
    state: str = Field("", description="State code (TX/CA/NY/FL/GA).")
    channel: str = Field("WEB", description="Intake channel (WEB/IVR/MOBILE/AGENT/CALLCENTER).")


class PayloadArg(BaseModel):
    payload: Dict[str, Any] = Field(..., description="Canonical FNOLPayload dict from intake.")


class TriageArgs(BaseModel):
    payload: Dict[str, Any]
    fraudScore: float = Field(0.0, description="0.0–1.0 fraud composite score.")
    coverageComplexity: float = Field(0.5, description="0.0–1.0 coverage complexity from A2.")


class FraudArgs(BaseModel):
    payload: Dict[str, Any]
    priorClaims: int = Field(0, description="Insured prior-claims count.")
    isoHits: int = Field(0, description="ISO ClaimSearch match count.")


class DamageArgs(BaseModel):
    payload: Dict[str, Any]
    presentedEstimate: float = Field(0.0, description="Shop/photo estimate in USD.")


class BIArgs(BaseModel):
    payload: Dict[str, Any]
    faultPct: float = Field(0.0, description="Insured % at fault, 0.0–1.0.")
    attorneyRetained: bool = Field(False)


class SettleArgs(BaseModel):
    payload: Dict[str, Any]
    damageEstimate: float = Field(0.0)
    biReserve: float = Field(0.0)
    fraudScore: float = Field(0.0)
    coverageOK: bool = Field(True)


class SubroArgs(BaseModel):
    payload: Dict[str, Any]
    faultPct: float = Field(1.0, description="Insured fault %; <1.0 implies subro potential.")
    paid: float = Field(0.0)


class ConvStartArgs(BaseModel):
    channel: str = Field("WEB")
    initialPolicy: Optional[str] = Field(None)


class ConvTurnArgs(BaseModel):
    sessionId: str
    text: str


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------

def _make_tool(name: str, description: str, args_schema: Type[BaseModel], fn) -> BaseTool:
    """Build a LangChain BaseTool that delegates to a registry method."""

    class _Tool(BaseTool):
        # LangChain attributes
        pass

    tool = _Tool()
    # Set instance attrs (works for both real BaseTool and shim).
    object.__setattr__(tool, "name", name)
    object.__setattr__(tool, "description", description)
    if LANGCHAIN_AVAILABLE:
        try:
            object.__setattr__(tool, "args_schema", args_schema)
        except Exception:
            pass

    def _run(**kwargs):
        try:
            result = fn(**kwargs)
            return json.dumps(result, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    object.__setattr__(tool, "_run", _run)
    object.__setattr__(tool, "run", lambda **kw: _run(**kw))
    return tool


def build_langchain_tools(registry) -> List[BaseTool]:
    """
    Build the full LangChain tool surface for the FNOL platform.

    Tools mirror the production agent set (A1..A8) plus L3-hook agents
    (Conversational Orchestration, Adjuster Co-Pilot) per V2 Blueprint §02.
    """
    tools: List[BaseTool] = []

    tools.append(_make_tool(
        "fnol_intake",
        "A1 — Extract canonical FNOL payload from narrative + policy. Returns FNOLPayload dict.",
        IntakeArgs, registry.tool_intake,
    ))
    tools.append(_make_tool(
        "coverage_verify",
        "A2 — Verify coverages, in-force status, ROR triggers. Returns CoverageResult dict.",
        PayloadArg, registry.tool_coverage,
    ))
    tools.append(_make_tool(
        "triage_route",
        "A3 — Score 5-dim weighted triage; route STP/T1..T4/CAT/SIU.",
        TriageArgs, registry.tool_triage,
    ))
    tools.append(_make_tool(
        "fraud_score",
        "A4 — Evaluate 40-signal fraud panel; return band + score + signals.",
        FraudArgs, registry.tool_fraud,
    ))
    tools.append(_make_tool(
        "damage_assess",
        "A5 — Damage estimate, total-loss decision, DRP shop selection.",
        DamageArgs, registry.tool_damage,
    ))
    tools.append(_make_tool(
        "bi_evaluate",
        "A6 — Bodily-injury reserve range with attorney/comparative-fault adjustments.",
        BIArgs, registry.tool_bi,
    ))
    tools.append(_make_tool(
        "settle",
        "A7 — Settlement gate (STP cap $15K, fraud/coverage blocks).",
        SettleArgs, registry.tool_settle,
    ))
    tools.append(_make_tool(
        "subrogation",
        "A8 — Third-party fault scoring + statute-of-limitations capture.",
        SubroArgs, registry.tool_subro,
    ))

    # L3-hook agents (V2 Blueprint additions)
    tools.append(_make_tool(
        "conv_session_start",
        "L3 — Start a single-session conversational FNOL with the claimant.",
        ConvStartArgs, registry.tool_conv_start,
    ))
    tools.append(_make_tool(
        "conv_turn",
        "L3 — Process the next turn in a conversational FNOL session.",
        ConvTurnArgs, registry.tool_conv_turn,
    ))
    tools.append(_make_tool(
        "copilot_brief",
        "L3 — Adjuster Co-Pilot: 3-sentence brief, red flags, next-best-actions, drafted comms.",
        PayloadArg, registry.tool_copilot,
    ))

    return tools


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from fnol_tools_registry import ToolRegistry
    reg = ToolRegistry()
    tools = build_langchain_tools(reg)
    print(f"Built {len(tools)} LangChain tools (LangChain available: {LANGCHAIN_AVAILABLE})")
    for t in tools:
        print(f"  - {t.name}: {t.description[:80]}")
