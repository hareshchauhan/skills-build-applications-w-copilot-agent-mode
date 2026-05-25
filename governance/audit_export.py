"""
governance/audit_export.py — exam-ready audit bundle for a claim.

Produces an in-memory dict (and optional zip bytes) containing:
  · all DecisionRecords for the claim (chronological)
  · chain verification result
  · applicable model cards (one per agent that touched the claim)
  · any adverse-action notices generated
  · retention class summary
  · regulatory crosswalk
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def build_audit_bundle(claim_number: str,
                       carrier_context: Optional[Dict[str, Any]] = None,
                       claimant_context: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
    """
    Assemble the audit bundle. Returns a dict; callers may also call
    `bundle_to_zip(bundle)` to materialize a downloadable zip.
    """
    from governance.decision_log import export_claim, verify_chain
    from governance.model_cards import MODEL_CARD_REGISTRY
    from governance.adverse_action import generate_notice
    from governance import AGENT_TO_CARD

    records = export_claim(claim_number)
    chain = verify_chain(claim_number)

    # which agents touched this claim?
    agent_names = {r.get("agentName") for r in records}
    cards = {}
    for agent_name in agent_names:
        card_id = AGENT_TO_CARD.get(agent_name)
        if card_id and card_id in MODEL_CARD_REGISTRY:
            cards[card_id] = MODEL_CARD_REGISTRY[card_id]

    # generate adverse-action notices for any DR that warrants one
    notices: List[Dict[str, Any]] = []
    for r in records:
        g = r.get("governance") or {}
        v = r.get("decisionValue") or {}
        triggers = (
            g.get("fcraDataUsed") or
            (r.get("decisionType") == "COVERAGE_VERIFY" and isinstance(v, dict) and v.get("decision") == "DENIED") or
            (r.get("decisionType") == "FRAUD_SCORE" and isinstance(v, dict) and v.get("band") in ("HIGH","CRITICAL")) or
            (r.get("decisionType") == "SETTLE" and isinstance(v, dict) and v.get("status") in ("BLOCKED_FRAUD","BLOCKED_COVERAGE","DENIED"))
        )
        if triggers:
            try:
                n = generate_notice(r, carrier_context, claimant_context)
                notices.append(n.to_dict())
            except Exception:
                pass

    # retention summary
    retention_classes = {}
    for r in records:
        rc = (r.get("governance") or {}).get("retentionClass", "STANDARD_7Y")
        retention_classes[rc] = retention_classes.get(rc, 0) + 1

    return {
        "claimNumber": claim_number,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "decisionRecords": records,
        "recordCount": len(records),
        "chainVerification": chain,
        "modelCardsApplied": cards,
        "adverseActionNoticesGenerated": notices,
        "retentionSummary": retention_classes,
        "regulatoryCrosswalk": _crosswalk(),
    }


def bundle_to_zip(bundle: Dict[str, Any]) -> bytes:
    """Materialize the audit bundle as a downloadable zip (bytes)."""
    buf = io.BytesIO()
    claim = bundle.get("claimNumber", "claim")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{claim}/manifest.json", json.dumps(
            {"claimNumber": claim, "generatedAt": bundle.get("generatedAt"),
             "recordCount": bundle.get("recordCount"),
             "chainValid": bundle.get("chainVerification", {}).get("valid"),
             "noticesGenerated": len(bundle.get("adverseActionNoticesGenerated", []))},
            indent=2,
        ))
        z.writestr(f"{claim}/decision_records.json", json.dumps(bundle.get("decisionRecords", []), indent=2, default=str))
        z.writestr(f"{claim}/chain_verification.json", json.dumps(bundle.get("chainVerification", {}), indent=2))
        z.writestr(f"{claim}/retention_summary.json", json.dumps(bundle.get("retentionSummary", {}), indent=2))
        for cid, card in bundle.get("modelCardsApplied", {}).items():
            z.writestr(f"{claim}/model_cards/{cid}.json", json.dumps(card, indent=2))
        for i, notice in enumerate(bundle.get("adverseActionNoticesGenerated", []), start=1):
            z.writestr(f"{claim}/adverse_action_notices/notice_{i:03d}.html", notice.get("html", ""))
            z.writestr(f"{claim}/adverse_action_notices/notice_{i:03d}.txt", notice.get("text", ""))
        z.writestr(f"{claim}/regulatory_crosswalk.json", json.dumps(_crosswalk(), indent=2))
    return buf.getvalue()


def _crosswalk() -> List[Dict[str, str]]:
    return [
        {"regulation": "NAIC Model Bulletin §4",
         "requirement": "Documented governance framework and model inventory.",
         "feature": "model_cards.py · 10 cards · validated on import"},
        {"regulation": "NAIC Model Bulletin §5",
         "requirement": "Ongoing testing for unfair discrimination.",
         "feature": "bias_monitor.py · A3 + A4 · auto-remediation hooks"},
        {"regulation": "Colorado Reg 10-1-1 §5",
         "requirement": "Quantitative testing for algorithmic discrimination.",
         "feature": "DPR / EOD / calibration metrics with breach gates"},
        {"regulation": "NYDFS Circular Letter No. 7",
         "requirement": "External-data governance, disparate impact testing.",
         "feature": "FCRA-data flag on Decision Records · A3/A4 monitors"},
        {"regulation": "FCRA §615(a)",
         "requirement": "Adverse action notice when CRA data influenced denial.",
         "feature": "adverse_action.py · template + 50-state addenda"},
        {"regulation": "State UCSPAs (50)",
         "requirement": "Timely investigation; written reasons for denial.",
         "feature": "SLA gates · ROR letters · denial templates"},
        {"regulation": "NIST AI RMF GOVERN-1.4",
         "requirement": "Documented model risk profile and accountability.",
         "feature": "Model card change_log + override audit on Decision Records"},
        {"regulation": "Tamper-evidence (NAIC implicit)",
         "requirement": "Audit log integrity in market conduct exam.",
         "feature": "SHA-256 hash chain across DecisionRecords per claim"},
    ]
