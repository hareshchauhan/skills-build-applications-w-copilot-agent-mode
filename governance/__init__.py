"""
FNOL Intelligence Platform · Governance Layer · v1.0

Cross-cutting governance for the 8-agent (+2 L3 hook) pipeline:
  · Model cards per agent (validated on import)
  · Bias monitor for A3 Triage and A4 Fraud
  · Decision Record query API + SHA-256 hash chain
  · Configurable retention & redaction
  · FCRA-compliant adverse action notice generation
  · NAIC / Colorado / NYDFS / state UCSPA crosswalk

Loading this package patches the engine's DecisionRecord enrichment hook
so every emit() automatically carries governance metadata. No agent code
changes required.
"""
from typing import Any, Dict, List, Optional

# Public surface
from governance.model_cards import (
    load_card, list_cards, validate_card, register_card,
    cards_for_export, MODEL_CARD_REGISTRY,
)
from governance.decision_log import (
    DecisionLogStore, get_store, append, query, verify_chain,
    export_claim, set_backend,
)
from governance.bias_monitor import (
    BiasReport, run_monitor, latest_report, history,
    is_breach_active, mark_decision_for_monitoring,
)
from governance.retention import (
    classify_retention, schedule_redaction, redact_claim, redaction_status,
    load_policy as load_retention_policy,
)
from governance.adverse_action import (
    AdverseActionNotice, generate_notice, available_templates,
)
from governance.audit_export import build_audit_bundle

__version__ = "1.0.0"


# ─────────────────────────────────────────────────────────────────────────────
# DR ENRICHMENT HOOK — patches fnol_workflow_engine on import
# ─────────────────────────────────────────────────────────────────────────────

# Map agentName (as set by each agent class) → model card id.
AGENT_TO_CARD: Dict[str, str] = {
    "FNOLIntakeAgent":              "a1_intake",
    "CoverageVerificationAgent":    "a2_coverage",
    "TriageAssignmentAgent":        "a3_triage",
    "FraudSignalDetectionAgent":    "a4_fraud",
    "DamageEstimationAgent":        "a5_damage",
    "BIEvaluationAgent":            "a6_bi",
    "SettlementAgent":              "a7_settlement",
    "SubrogationAgent":             "a8_subrogation",
    "ConversationalOrchestrationAgent": "l3a_conversational",
    "AdjusterCoPilotAgent":         "l3b_copilot",
}

# CRA / external-data sources that trigger FCRA §615.
FCRA_DATA_SOURCES = {"ISO_ClaimSearch", "LexisNexis_CLUE", "Verisk", "TransUnion_Auto"}


def _enrich(dr: Any) -> None:
    """
    Called by DecisionRecord.make() after construction. Populates dr.governance.
    Must NEVER raise — pipeline correctness wins over governance completeness.
    """
    try:
        card_id = AGENT_TO_CARD.get(dr.agentName, "unknown")
        card = MODEL_CARD_REGISTRY.get(card_id)

        # Identify FCRA data sources from the agent's input feature hash context
        # (best-effort: agents may declare this via thread-local in v2; v1 = card-level)
        fcra_sources: List[str] = []
        data_sources: List[str] = []
        if card:
            for src in card.get("inputs", {}).get("data_sources", []) or []:
                src_name = src.get("source", "") if isinstance(src, dict) else str(src)
                data_sources.append(src_name)
                for cra in FCRA_DATA_SOURCES:
                    if cra.lower() in src_name.lower():
                        fcra_sources.append(cra)

        # Bias-flag check (A3 Triage / A4 Fraud only)
        bias_flag = False
        if card_id in ("a3_triage", "a4_fraud"):
            bias_flag = is_breach_active(card_id)

        # Retention class — rules-based per decisionType
        retention_class = classify_retention(
            decision_type=dr.decisionType,
            decision_value=dr.decisionValue,
            agent_name=dr.agentName,
        )

        # Populate governance metadata FIRST (without recordHash). This is the
        # state that will be hashed and the state verify_chain will see — they
        # must match exactly. recordHash is the only field added post-hash.
        store = get_store()
        prev_hash = store.last_hash_for_claim(dr.claimNumber)

        dr.governance = {
            "modelCardId": card_id,
            "modelCardVersion": (card or {}).get("version", "n/a"),
            "dataSourcesUsed": data_sources,
            "fcraDataUsed": bool(fcra_sources),
            "fcraSources": fcra_sources,
            "biasFlagActive": bias_flag,
            "retentionClass": retention_class,
            "redactionApplied": False,
            "explanationTemplateId": None,
            "overrideOf": None,
            "overrideReason": None,
            "previousRecordHash": prev_hash,
            "recordHash": None,
        }
        # Now compute hash over the fully populated DR (compute_hash strips
        # recordHash and forces previousRecordHash = prev_hash, so this is
        # deterministic and reproducible by verify_chain).
        record_hash = store.compute_hash(dr, previous_hash=prev_hash)
        dr.governance["recordHash"] = record_hash

        # Persist
        store.append(dr)

        # Mark for bias-monitor sampling
        if card_id in ("a3_triage", "a4_fraud"):
            mark_decision_for_monitoring(card_id, dr)

    except Exception:
        # Best-effort. Never break the pipeline.
        try:
            dr.governance = dr.governance or {"error": "enrichment_failed"}
        except Exception:
            pass


def _install_hook() -> None:
    """Monkey-patch the engine's DR enrichment hook."""
    try:
        import fnol_workflow_engine as eng
        eng._governance_enrich_hook = _enrich           # type: ignore[attr-defined]
    except ImportError:
        pass


# Auto-install on package import
_install_hook()


__all__ = [
    "load_card", "list_cards", "validate_card", "register_card",
    "cards_for_export", "MODEL_CARD_REGISTRY", "AGENT_TO_CARD",
    "DecisionLogStore", "get_store", "append", "query", "verify_chain",
    "export_claim", "set_backend",
    "BiasReport", "run_monitor", "latest_report", "history",
    "classify_retention", "schedule_redaction", "redact_claim", "redaction_status",
    "AdverseActionNotice", "generate_notice", "available_templates",
    "build_audit_bundle",
]
