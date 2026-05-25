"""
governance/retention.py — retention class assignment and PII redaction.

Retention classes (configurable via governance/policies/retention_default.yaml):
  · STANDARD_7Y       — default; routine closed claims
  · EXTENDED_10Y      — BI claims, fatality, litigation, fraud HIGH/CRITICAL
  · MINIMAL_3Y        — denied claims, no payment
  · LITIGATION_HOLD   — indefinite, manual release

Redaction is field-level via a small DSL in governance/policies/redaction_default.yaml.
"""
from __future__ import annotations

import json
import pathlib
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


POLICIES_DIR = pathlib.Path(__file__).parent / "policies"

# ── In-memory redaction state (POC) ─────────────────────────────────────────
_lock = threading.RLock()
_redacted_claims: Dict[str, Dict[str, Any]] = {}   # claimNumber → {appliedAt, fieldsAffected}


def load_policy() -> Dict[str, Any]:
    """Load retention + redaction policies (returns merged dict)."""
    out: Dict[str, Any] = {"retention": {}, "redaction": {}}
    for name, key in [("retention_default.json", "retention"),
                      ("retention_default.yaml", "retention"),
                      ("redaction_default.json", "redaction"),
                      ("redaction_default.yaml", "redaction")]:
        p = POLICIES_DIR / name
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
            if name.endswith(".json"):
                out[key] = json.loads(text)
            else:
                try:
                    import yaml  # type: ignore
                    out[key] = yaml.safe_load(text)
                except ImportError:
                    pass
        except Exception:
            pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Retention classification
# ─────────────────────────────────────────────────────────────────────────────

def classify_retention(*, decision_type: str, decision_value: Any,
                       agent_name: str) -> str:
    """Apply retention rules in priority order. Returns retention class."""
    # Litigation hold
    if isinstance(decision_value, dict) and decision_value.get("litigationFlag"):
        return "LITIGATION_HOLD"

    # BI evaluations always get extended
    if decision_type in ("BI_EVALUATE", "INJURY_ASSESS"):
        return "EXTENDED_10Y"

    # Fraud HIGH/CRITICAL → extended
    if decision_type == "FRAUD_SCORE" and isinstance(decision_value, dict):
        band = decision_value.get("band", "")
        if band in ("HIGH", "CRITICAL"):
            return "EXTENDED_10Y"

    # Settlement-related
    if decision_type == "SETTLE" and isinstance(decision_value, dict):
        status = decision_value.get("status", "")
        if status in ("BLOCKED_FRAUD", "BLOCKED_COVERAGE"):
            return "EXTENDED_10Y"
        if status == "DENIED":
            return "MINIMAL_3Y"

    if decision_type == "COVERAGE_VERIFY" and isinstance(decision_value, dict):
        if decision_value.get("decision") == "DENIED":
            return "MINIMAL_3Y"

    # Subrogation pursuits → extended
    if decision_type == "SUBROGATION_ASSESS" and isinstance(decision_value, dict):
        if decision_value.get("pursue"):
            return "EXTENDED_10Y"

    return "STANDARD_7Y"


# ─────────────────────────────────────────────────────────────────────────────
# Redaction
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_REDACTION_RULES: List[Dict[str, str]] = [
    {"path": "parties.ssn", "action": "tokenize"},
    {"path": "parties.lastName", "action": "redact"},
    {"path": "parties.firstName", "action": "redact"},
    {"path": "parties.dateOfBirth", "action": "tokenize"},
    {"path": "parties.address", "action": "redact_to_zip3"},
    {"path": "claimant.email", "action": "redact"},
    {"path": "claimant.phone", "action": "tokenize"},
    {"path": "lossDescription", "action": "keep"},  # actuarial value
    {"path": "photosUploaded", "action": "delete_object"},
]


def schedule_redaction(claim_number: str, retention_class: str) -> Dict[str, Any]:
    """
    POC: returns a 'scheduled' marker. In production, a worker triggers
    redaction at (claim_close_date + retention_class duration).
    """
    durations_days = {"STANDARD_7Y": 365 * 7, "EXTENDED_10Y": 365 * 10,
                      "MINIMAL_3Y": 365 * 3, "LITIGATION_HOLD": -1}
    d = durations_days.get(retention_class, 365 * 7)
    return {
        "claimNumber": claim_number, "retentionClass": retention_class,
        "scheduledRedactionInDays": d,
        "scheduledAt": datetime.now(timezone.utc).isoformat(),
    }


def redact_claim(claim_number: str, claim_data: Optional[Dict[str, Any]] = None,
                 rules: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """
    Apply redaction to a claim's data in-place (returns a redacted copy).
    POC entry point — production runs as a scheduled job.
    """
    rules = rules or DEFAULT_REDACTION_RULES
    affected: List[str] = []
    out = dict(claim_data) if claim_data else {"claimNumber": claim_number}

    for rule in rules:
        path = rule["path"]
        action = rule["action"]
        if action == "keep":
            continue
        # naive top-level path application; v2 walks nested paths
        seg = path.split(".")[0]
        if seg in out:
            if action == "redact":
                out[seg] = "[REDACTED]"
            elif action == "tokenize":
                import hashlib
                v = str(out[seg])
                out[seg] = "TOK-" + hashlib.sha256(v.encode()).hexdigest()[:12]
            elif action == "redact_to_zip3":
                v = str(out.get(seg, ""))
                # crude — just keep first 3 chars of any zip-like field
                out[seg] = v[:3] + "**" if len(v) >= 3 else "[REDACTED]"
            elif action == "delete_object":
                out[seg] = None
            affected.append(path)

    with _lock:
        _redacted_claims[claim_number] = {
            "appliedAt": datetime.now(timezone.utc).isoformat(),
            "fieldsAffected": affected,
            "rulesApplied": len(rules),
        }
    return {"claimNumber": claim_number, "redacted": out, "fieldsAffected": affected,
            "appliedAt": _redacted_claims[claim_number]["appliedAt"]}


def redaction_status(claim_number: str) -> Dict[str, Any]:
    with _lock:
        if claim_number in _redacted_claims:
            return {"claimNumber": claim_number, "redacted": True,
                    **_redacted_claims[claim_number]}
        return {"claimNumber": claim_number, "redacted": False}
