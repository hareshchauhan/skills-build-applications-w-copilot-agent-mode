"""
governance/adverse_action.py — FCRA §615 + state UCSPA notice generation.

Five base templates ship with the layer:
  · fcra_adverse_action      — when CRA-derived data influenced the adverse decision
  · coverage_denial_ror      — Reservation of Rights letter
  · settlement_reduction     — claim paid below claimed amount
  · fraud_investigation_hold — SIU referral / payment hold notice
  · claim_denial_general     — generic adverse action without CRA involvement

State-specific addenda load from governance/templates/state_addenda/{STATE}.txt.
Output: text + HTML; PDF generation hook stubbed (uses pdf skill in production).
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates"
ADDENDA_DIR   = TEMPLATES_DIR / "state_addenda"


# ─────────────────────────────────────────────────────────────────────────────
# Output dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AdverseActionNotice:
    decisionRecordId: str
    claimNumber: str
    templateUsed: str
    stateAddendumUsed: Optional[str]
    text: str
    html: str
    pdfBase64: Optional[str] = None
    generatedAt: str = ""
    decisionRecordHash: Optional[str] = None
    modelCardVersion: Optional[str] = None
    fcraDataSources: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Template loader
# ─────────────────────────────────────────────────────────────────────────────

def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _template(name: str) -> str:
    p = TEMPLATES_DIR / f"{name}.txt"
    return _read(p)


def _state_addendum(state: str) -> str:
    if not state:
        return _read(ADDENDA_DIR / "DEFAULT.txt")
    p = ADDENDA_DIR / f"{state.upper()}.txt"
    if p.exists():
        return _read(p)
    return _read(ADDENDA_DIR / "DEFAULT.txt")


def available_templates() -> List[str]:
    if not TEMPLATES_DIR.exists():
        return []
    return sorted([p.stem for p in TEMPLATES_DIR.glob("*.txt")])


def available_state_addenda() -> List[str]:
    if not ADDENDA_DIR.exists():
        return []
    return sorted([p.stem for p in ADDENDA_DIR.glob("*.txt")])


# ─────────────────────────────────────────────────────────────────────────────
# Template selection
# ─────────────────────────────────────────────────────────────────────────────

def select_template(decision_record: Dict[str, Any]) -> str:
    g = decision_record.get("governance") or {}
    dt = decision_record.get("decisionType")
    val = decision_record.get("decisionValue") or {}

    if g.get("fcraDataUsed") and (
        (isinstance(val, dict) and val.get("status") in ("DENIED", "BLOCKED_FRAUD"))
        or (isinstance(val, dict) and val.get("decision") == "DENIED")
    ):
        return "fcra_adverse_action"

    if dt == "COVERAGE_VERIFY" and isinstance(val, dict) and val.get("decision") == "DENIED":
        return "coverage_denial_ror"

    if dt == "FRAUD_SCORE" and isinstance(val, dict) and val.get("band") in ("HIGH", "CRITICAL"):
        return "fraud_investigation_hold"

    if dt == "SETTLE" and isinstance(val, dict):
        status = val.get("status", "")
        if status in ("BLOCKED_FRAUD", "BLOCKED_COVERAGE"):
            return "fraud_investigation_hold" if status == "BLOCKED_FRAUD" else "coverage_denial_ror"
        if val.get("netReductionPct", 0) > 0.20:
            return "settlement_reduction"

    return "claim_denial_general"


# ─────────────────────────────────────────────────────────────────────────────
# Renderer
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_addr(claimant: Dict[str, Any]) -> str:
    parts = [claimant.get("address1") or "", claimant.get("address2") or "",
             f"{claimant.get('city','')}, {claimant.get('state','')} {claimant.get('zip','')}".strip(", ")]
    return "\n".join([p for p in parts if p.strip()])


def _render(template_text: str, ctx: Dict[str, Any]) -> str:
    # tiny {key} substitution; missing keys → "[N/A]"
    out = template_text
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", str(v) if v is not None else "[N/A]")
    # any unresolved tokens
    import re
    out = re.sub(r"\{[a-zA-Z0-9_]+\}", "[N/A]", out)
    return out


def _to_html(text: str, ctx: Dict[str, Any]) -> str:
    """Wrap plain text in a printable HTML letter — light theme."""
    body = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Adverse Action Notice — {ctx.get('claim_number','')}</title>
<style>
  body {{ background:#fafafa; color:#09090b; font-family:Georgia,'Crimson Pro',serif;
          font-size:13px; line-height:1.7; margin:0; padding:48px 56px; max-width:760px; }}
  .meta {{ font-family:'Syne Mono',monospace; font-size:9px; letter-spacing:.14em;
           color:#52525b; text-transform:uppercase; border-top:1px solid #e4e4e7;
           margin-top:36px; padding-top:14px; }}
</style></head><body>{body}<div class="meta">Generated by FNOL Intelligence Platform · Decision Record {ctx.get('decision_record_id','')} · Hash {ctx.get('decision_record_hash','')[:16] if ctx.get('decision_record_hash') else 'n/a'}</div></body></html>"""


def generate_notice(decision_record: Dict[str, Any],
                    carrier_context: Optional[Dict[str, Any]] = None,
                    claimant_context: Optional[Dict[str, Any]] = None) -> AdverseActionNotice:
    """
    Generate an FCRA-compliant adverse action notice from a Decision Record.
    decision_record: the DR dict (as returned by store.get())
    carrier_context: { carrier_name, carrier_address, adjuster_name, adjuster_phone, adjuster_email,
                       cra_name, cra_address, cra_phone, cra_url }
    claimant_context: { name, address1, city, state, zip, ... }
    """
    cc = dict(carrier_context or {})
    pc = dict(claimant_context or {})

    template_name = select_template(decision_record)
    template_text = _template(template_name)
    state = (pc.get("state") or "").upper() or (decision_record.get("decisionValue") or {}).get("state", "")
    addendum = _state_addendum(state)

    g = decision_record.get("governance") or {}
    ctx = {
        "date": datetime.now(timezone.utc).strftime("%B %d, %Y"),
        "claimant_name": pc.get("name") or "Policyholder",
        "claimant_address": _fmt_addr(pc),
        "claim_number": decision_record.get("claimNumber") or "[N/A]",
        "decision_record_id": decision_record.get("decisionId") or "[N/A]",
        "decision_record_hash": g.get("recordHash") or "[N/A]",
        "model_card_version": g.get("modelCardVersion") or "[N/A]",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "carrier_name": cc.get("carrier_name") or "[Carrier Name]",
        "carrier_address": cc.get("carrier_address") or "[Carrier Address]",
        "adjuster_name": cc.get("adjuster_name") or "[Adjuster Name]",
        "adjuster_phone": cc.get("adjuster_phone") or "[Adjuster Phone]",
        "adjuster_email": cc.get("adjuster_email") or "[Adjuster Email]",
        "cra_name": cc.get("cra_name") or "ISO ClaimSearch",
        "cra_address": cc.get("cra_address") or "Verisk Analytics, 545 Washington Boulevard, Jersey City, NJ 07310",
        "cra_phone": cc.get("cra_phone") or "1-800-995-2310",
        "cra_url": cc.get("cra_url") or "https://www.verisk.com",
        "state_specific_addendum": addendum or "",
        "adverse_action_summary": _summarize_adverse_action(decision_record),
        "plain_language_reasons": _explain_reasons(decision_record),
    }

    text = _render(template_text or _fallback_template(), ctx)
    html = _to_html(text, ctx)

    notice = AdverseActionNotice(
        decisionRecordId=ctx["decision_record_id"],
        claimNumber=ctx["claim_number"],
        templateUsed=template_name,
        stateAddendumUsed=state if addendum else None,
        text=text, html=html,
        generatedAt=ctx["generated_at"],
        decisionRecordHash=g.get("recordHash"),
        modelCardVersion=g.get("modelCardVersion"),
        fcraDataSources=g.get("fcraSources") or [],
    )
    return notice


def _summarize_adverse_action(dr: Dict[str, Any]) -> str:
    dt = dr.get("decisionType")
    v = dr.get("decisionValue") or {}
    if dt == "COVERAGE_VERIFY" and v.get("decision") == "DENIED":
        return "Coverage for the loss reported has been denied."
    if dt == "FRAUD_SCORE" and v.get("band") in ("HIGH", "CRITICAL"):
        return ("Your claim has been placed on investigative hold pending review by our "
                "Special Investigations Unit (SIU).")
    if dt == "SETTLE":
        s = v.get("status")
        if s == "BLOCKED_FRAUD":
            return "Settlement has been suspended pending fraud investigation."
        if s == "BLOCKED_COVERAGE":
            return "Settlement has been suspended pending coverage review."
        if s == "DENIED":
            return "Your claim has been denied."
        if v.get("netReductionPct", 0) > 0.20:
            return "Your settlement has been adjusted below the originally claimed amount."
    return "An adverse decision has been recorded on your claim."


def _explain_reasons(dr: Dict[str, Any]) -> str:
    expl = dr.get("explanation") or ""
    if expl and len(expl) > 8:
        return expl
    dt = dr.get("decisionType")
    return f"Decision based on {dt} agent rules; full reasoning available upon request."


def _fallback_template() -> str:
    """Used if no .txt templates are present (sandbox / first-run)."""
    return (
        "{date}\n\n{claimant_name}\n{claimant_address}\n\n"
        "Re: Claim {claim_number} — Notice of Adverse Action\n\n"
        "Dear {claimant_name}:\n\n"
        "{adverse_action_summary}\n\n"
        "REASONS FOR THE ACTION\n{plain_language_reasons}\n\n"
        "INFORMATION OBTAINED FROM A CONSUMER REPORTING AGENCY\n"
        "This action was based, in whole or in part, on information from:\n"
        "  {cra_name}\n  {cra_address}\n  {cra_phone}\n  {cra_url}\n\n"
        "The agency above did NOT make the decision and cannot provide the specific "
        "reasons for it.\n\n"
        "YOUR RIGHTS UNDER THE FAIR CREDIT REPORTING ACT\n"
        "You have the right to a free copy of your consumer report from the agency "
        "above by requesting it within 60 days of receiving this notice. You also "
        "have the right to dispute the accuracy or completeness of any information "
        "in the report.\n\n"
        "QUESTIONS\n  {adjuster_name}\n  {adjuster_phone}\n  {adjuster_email}\n\n"
        "{state_specific_addendum}\n\n"
        "Sincerely,\n{carrier_name} Claims Department\n\n"
        "─────────────────────────────────────────────────────────\n"
        "Generated by FNOL Intelligence Platform on {generated_at}\n"
        "Decision Record ID: {decision_record_id}\n"
        "Decision Record Hash: {decision_record_hash}\n"
        "Model Card Version:   {model_card_version}\n"
    )
