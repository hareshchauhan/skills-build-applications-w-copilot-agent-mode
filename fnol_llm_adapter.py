"""
FNOL Intelligence Platform — LLM Adapter
========================================
Multi-provider abstraction with deterministic rules-based fallback.

Supported providers (auto-detected from env vars; first match wins):
  - anthropic     (ANTHROPIC_API_KEY)
  - azure_openai  (AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT)
  - bedrock       (AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY)
  - openai        (OPENAI_API_KEY)
  - mock          (always available — rules-based, no network)

Provider selection precedence:
  1. FNOL_LLM_PROVIDER env var (explicit override)
  2. First provider whose credentials are present
  3. Falls back to 'mock' if nothing configured

Every call returns a normalized envelope:
  {
    "provider": str,
    "model": str,
    "ok": bool,
    "text": str,
    "usage": {...} | None,
    "elapsed_ms": int,
    "error": str | None,
  }

NOT FOR PRODUCTION USE WITHOUT:
  - Carrier-approved provider + region
  - PII-scrubbing pre-processor
  - Decision Record persistence
  - Champion/Challenger evaluation harness
"""

from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fnol_settings import settings

# ───────────────────────────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────────────────────────

DEFAULT_MODEL = {
    "anthropic": "claude-opus-4-6",
    "azure_openai": "gpt-4o",
    "bedrock": "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "openai": "gpt-4o-mini",
    "mock": "mock-rules-v1",
}

PROVIDER_ENV_REQS = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "azure_openai": ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"],
    "bedrock": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "mock": [],
}


# ───────────────────────────────────────────────────────────────────────────
# Result envelope
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class LLMResult:
    provider: str                          # provider that the caller *requested*
    model: str
    ok: bool
    text: str = ""
    usage: Optional[Dict[str, Any]] = None
    elapsed_ms: int = 0
    error: Optional[str] = None
    fallback_used: bool = False            # True iff mock content substituted
                                           # for a failed real-provider call
    effective_provider: Optional[str] = None  # what actually produced `text`

    def __post_init__(self) -> None:
        if self.effective_provider is None:
            self.effective_provider = self.provider

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "effective_provider": self.effective_provider,
            "model": self.model,
            "ok": self.ok,
            "text": self.text,
            "usage": self.usage,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "fallback_used": self.fallback_used,
        }


# ───────────────────────────────────────────────────────────────────────────
# Provider selection
# ───────────────────────────────────────────────────────────────────────────

def _provider_is_configured(name: str) -> bool:
    reqs = PROVIDER_ENV_REQS.get(name, [])
    return all(os.getenv(k) for k in reqs)


def resolve_provider() -> str:
    explicit = (settings.fnol_llm_provider or "auto").strip().lower()
    if explicit != "auto":
        if explicit in DEFAULT_MODEL:
            return explicit
        return "mock"
    for candidate in ("anthropic", "azure_openai", "bedrock", "openai"):
        if _provider_is_configured(candidate):
            return candidate
    return "mock"


# ───────────────────────────────────────────────────────────────────────────
# Mock provider — deterministic rules-based generation
# ───────────────────────────────────────────────────────────────────────────

def _mock_complete(system: str, user: str) -> str:
    """Heuristic mock that returns a plausible structured response.
    Used when no LLM credentials are configured, or for offline POC."""
    u = (user or "").lower()
    if "fraud" in u or "anomaly" in u:
        return json.dumps({
            "summary": "Mock fraud assessment: no telematics-narrative mismatch detected, no ISO match, no network cluster flag.",
            "indicators": [],
            "confidence": 0.82,
        })
    if "coverage" in u or "policy" in u:
        return json.dumps({
            "summary": "Mock coverage analysis: policy in force; collision and comprehensive coverages active.",
            "advisories": [],
        })
    if "damage" in u or "estimate" in u:
        return json.dumps({
            "summary": "Mock damage assessment: moderate front-bumper damage consistent with low-speed impact.",
            "estimate_low": 2400,
            "estimate_high": 4100,
        })
    if "bi" in u or "injury" in u or "medical" in u:
        return json.dumps({
            "summary": "Mock BI evaluation: soft-tissue presentation; treatment within expected range.",
            "settlement_range_low": 3500,
            "settlement_range_high": 9500,
        })
    if "subro" in u or "subrogation" in u:
        return json.dumps({
            "summary": "Mock subrogation: adverse fault clear; recovery opportunity present.",
            "recovery_potential_usd": 12500,
        })
    if "intent: summary" in u:
        return ("Bottom line: claim is moving on the standard track with no SIU hold and no coverage dispute.\n"
                "Coverage verified; reserves set to initial guidance. Damage estimate is in the four-figure band, "
                "vehicle drivable, no total loss. Triage routed to a standard adjuster; STP gate not met because "
                "an injury is reported.\n\n"
                "Next-best-actions (ranked):\n"
                "  1. Confirm injury severity with claimant; request medical authorization (HIPAA-compliant form).\n"
                "  2. Issue rental authorization only if vehicle becomes non-drivable on follow-up.\n"
                "  3. Open subrogation watch — rear-ended-by-other plus identifiable adverse carrier.\n\n"
                "(Mock co-pilot response — configure ANTHROPIC_API_KEY, AZURE_OPENAI_API_KEY, "
                "AWS_*, or OPENAI_API_KEY for live LLM reasoning.)")
    if "intent: next_action" in u:
        return ("Bottom line: this claim is ready for the standard track; the single highest-impact action "
                "is medical authorization so BI evaluation can complete.\n\n"
                "Next-best-actions (ranked):\n"
                "  1. Request HIPAA medical authorization from claimant (highest impact on cycle time).\n"
                "  2. Acknowledge third-party carrier contact for subrogation coordination.\n"
                "  3. Confirm photo set covers all four quadrants; request supplemental photos if any gap.\n\n"
                "(Mock co-pilot response — wire a real LLM provider for full reasoning.)")
    if "intent: explain_decision" in u:
        return ("Plain-language explanation:\n"
                "The triage agent scored this claim in the mid range, weighted toward injury severity (30%) "
                "and estimated loss (25%). Because an injury was reported, the system routed it to the BI "
                "track rather than the STP express track — even though the estimated property damage is small. "
                "If the claimant is later confirmed uninjured, the adjuster can override and re-route, with the "
                "override logged in the Decision Record.\n\n"
                "(Mock co-pilot response — wire a real LLM provider for full reasoning.)")
    if "intent: draft_letter" in u:
        return ("Draft — Acknowledgement Letter (subject to adjuster review):\n\n"
                "Dear Policyholder,\n\n"
                "Thank you for reporting this incident. We have opened your claim and assigned an adjuster "
                "who will contact you within one business day. Coverage on your policy has been confirmed "
                "and we have set initial reserves. Please reply to any requests for photos, medical "
                "authorization, or repair-shop information so we can move quickly.\n\n"
                "Sincerely,\nClaims Department\n\n"
                "(Mock co-pilot response — wire a real LLM provider for full personalization.)")
    if "intent: draft_note" in u:
        return ("Diary entry (draft):\n\n"
                "FNOL captured via web channel. Policy verified in-force at loss datetime. Initial reserves "
                "set per authority matrix. Triage routed to standard track; injury reported, BI pathway active. "
                "Fraud composite score within LOW band. Damage estimate within initial PD reserve. "
                "No coverage exclusions triggered. Action: request medical authorization, monitor for "
                "tender-of-limits trigger if injury severity escalates.")
    if "intent: compliance_check" in u:
        return ("Compliance posture: acknowledgement dispatched within SLA. No ROR required because coverage "
                "is verified. State DOI prompt-payment clock starts at acknowledgement timestamp. No state "
                "AI-disclosure requirement currently triggered for this jurisdiction.\n\n"
                "(Mock co-pilot response — verify state-specific requirements in Rules Engine.)")
    if "copilot" in u or "co-pilot" in u or "explain" in u or "next step" in u:
        return ("Mock co-pilot response. To enable rich reasoning, set FNOL_LLM_PROVIDER "
                "and a provider key (ANTHROPIC_API_KEY, AZURE_OPENAI_API_KEY, AWS keys, or OPENAI_API_KEY).")
    return json.dumps({"summary": "Mock LLM response — no provider configured.", "ok": True})


# ───────────────────────────────────────────────────────────────────────────
# Real provider calls (thin wrappers — fail safe to mock)
# ───────────────────────────────────────────────────────────────────────────

def _call_anthropic(system: str, user: str, model: str, max_tokens: int) -> LLMResult:
    t0 = time.time()
    try:
        from anthropic import Anthropic  # type: ignore
        client = Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        usage = {
            "input_tokens": getattr(resp.usage, "input_tokens", None),
            "output_tokens": getattr(resp.usage, "output_tokens", None),
        }
        return LLMResult("anthropic", model, True, text, usage,
                         int((time.time() - t0) * 1000))
    except Exception as e:
        return LLMResult("anthropic", model, False,
                         elapsed_ms=int((time.time() - t0) * 1000),
                         error=f"{type(e).__name__}: {e}")


def _call_openai(system: str, user: str, model: str, max_tokens: int) -> LLMResult:
    t0 = time.time()
    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI()
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = resp.choices[0].message.content or ""
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens if resp.usage else None,
            "completion_tokens": resp.usage.completion_tokens if resp.usage else None,
        }
        return LLMResult("openai", model, True, text, usage,
                         int((time.time() - t0) * 1000))
    except Exception as e:
        return LLMResult("openai", model, False,
                         elapsed_ms=int((time.time() - t0) * 1000),
                         error=f"{type(e).__name__}: {e}")


def _call_azure_openai(system: str, user: str, model: str, max_tokens: int) -> LLMResult:
    t0 = time.time()
    try:
        from openai import AzureOpenAI  # type: ignore
        client = AzureOpenAI(
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        )
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", model)
        resp = client.chat.completions.create(
            model=deployment,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = resp.choices[0].message.content or ""
        return LLMResult("azure_openai", deployment, True, text, None,
                         int((time.time() - t0) * 1000))
    except Exception as e:
        return LLMResult("azure_openai", model, False,
                         elapsed_ms=int((time.time() - t0) * 1000),
                         error=f"{type(e).__name__}: {e}")


def _call_bedrock(system: str, user: str, model: str, max_tokens: int) -> LLMResult:
    t0 = time.time()
    try:
        import boto3  # type: ignore
        client = boto3.client("bedrock-runtime",
                              region_name=os.getenv("AWS_REGION", "us-east-1"))
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        })
        resp = client.invoke_model(modelId=model, body=body)
        payload = json.loads(resp["body"].read())
        text = "".join(b.get("text", "") for b in payload.get("content", []))
        return LLMResult("bedrock", model, True, text, None,
                         int((time.time() - t0) * 1000))
    except Exception as e:
        return LLMResult("bedrock", model, False,
                         elapsed_ms=int((time.time() - t0) * 1000),
                         error=f"{type(e).__name__}: {e}")


# ───────────────────────────────────────────────────────────────────────────
# Public API
# ───────────────────────────────────────────────────────────────────────────

def complete(
    system: str,
    user: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = 1024,
    fallback_to_mock: bool = True,
) -> LLMResult:
    """Provider-agnostic completion. Fails safe to mock on any error."""
    p = (provider or resolve_provider()).lower()
    m = model or DEFAULT_MODEL.get(p, DEFAULT_MODEL["mock"])

    if p == "mock":
        t0 = time.time()
        return LLMResult("mock", m, True,
                         _mock_complete(system, user),
                         {"input_tokens": len(user) // 4, "output_tokens": 64},
                         int((time.time() - t0) * 1000))

    if p == "anthropic":
        result = _call_anthropic(system, user, m, max_tokens)
    elif p == "openai":
        result = _call_openai(system, user, m, max_tokens)
    elif p == "azure_openai":
        result = _call_azure_openai(system, user, m, max_tokens)
    elif p == "bedrock":
        result = _call_bedrock(system, user, m, max_tokens)
    else:
        result = LLMResult(p, m, False, error=f"unknown provider {p!r}")

    if not result.ok and fallback_to_mock:
        t0 = time.time()
        # Preserve the caller-requested provider so downstream code that
        # branches on `provider` does not mis-detect this as a normal mock
        # response. `fallback_used=True` and `effective_provider="mock"` are
        # the explicit signals callers should check.
        return LLMResult(
            provider=p,
            model=m,
            ok=False,
            text=_mock_complete(system, user),
            usage={"fallback_from": p},
            elapsed_ms=int((time.time() - t0) * 1000),
            error=f"fallback_from_{p}",
            fallback_used=True,
            effective_provider="mock",
        )
    return result


def health() -> Dict[str, Any]:
    """Reports which providers are configured. Does not make network calls."""
    p = resolve_provider()
    return {
        "active_provider": p,
        "active_model": DEFAULT_MODEL.get(p, "mock-rules-v1"),
        "providers_configured": {
            name: _provider_is_configured(name) for name in DEFAULT_MODEL.keys()
        },
    }


if __name__ == "__main__":
    print(json.dumps(health(), indent=2))
    res = complete("You are an insurance assistant.",
                   "Summarize coverage for a fender-bender claim.")
    print(json.dumps(res.to_dict(), indent=2))
