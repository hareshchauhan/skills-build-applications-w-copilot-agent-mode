"""
governance/model_cards.py — Model card loader, validator, registry.

Cards live as YAML in governance/cards/{agent_id}.yaml. Loading is eager:
the registry is populated at module import. Validation errors raise.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Try yaml; if not available, fall back to a tiny YAML-ish loader for the simple
# subset we actually use in our cards. Production should install pyyaml.
try:
    import yaml  # type: ignore
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
    yaml = None  # type: ignore


CARDS_DIR = pathlib.Path(__file__).parent / "cards"

# Required top-level fields. Cards missing any of these fail validation.
REQUIRED_FIELDS = {
    "agent_id", "agent_name", "version", "blueprint_stage", "maturity_level",
    "owner", "intended_use", "model_type", "inputs", "outputs",
    "performance", "human_oversight", "regulatory_alignment",
    "audit_log_location", "retention_class",
}

# In-memory registry: card_id -> dict
MODEL_CARD_REGISTRY: Dict[str, Dict[str, Any]] = {}


def _simple_yaml_load(text: str) -> Dict[str, Any]:
    """
    Tiny fallback YAML loader for the minimal subset our cards use:
    - top-level mappings
    - nested mappings
    - lists of scalars or single-line maps
    - scalars (string/int/float/bool)
    No anchors, no flow-style maps, no multi-line strings beyond '|' literals.
    Good enough for governance cards; production must install pyyaml.
    """
    # Try json fallback first (cards may be authored as JSON for testability)
    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    raise RuntimeError(
        "pyyaml is not installed and the model card is not JSON. "
        "Install pyyaml or author cards as JSON. "
        "pip install pyyaml --break-system-packages"
    )


def _load_yaml_file(path: pathlib.Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if _HAS_YAML:
        return yaml.safe_load(text)  # type: ignore[union-attr]
    return _simple_yaml_load(text)


def validate_card(card: Dict[str, Any]) -> List[str]:
    """Returns a list of validation errors. Empty list = card is valid."""
    errors: List[str] = []
    if not isinstance(card, dict):
        return ["card root is not a mapping"]

    missing = REQUIRED_FIELDS - set(card.keys())
    if missing:
        errors.append(f"missing required fields: {sorted(missing)}")

    if "maturity_level" in card and card["maturity_level"] not in ("L1", "L2", "L3"):
        errors.append(f"maturity_level must be L1|L2|L3, got: {card['maturity_level']}")

    if "version" in card and not isinstance(card["version"], str):
        errors.append("version must be a string (semver-ish)")

    if "owner" in card:
        owner = card["owner"]
        if not isinstance(owner, dict) or "team" not in owner:
            errors.append("owner must be a mapping with at least 'team'")

    if "fairness" in card:
        f = card["fairness"]
        if isinstance(f, dict) and f.get("monitored") is True:
            if not f.get("protected_attributes"):
                errors.append("fairness.monitored=true requires protected_attributes")

    if "retention_class" in card and card["retention_class"] not in (
        "STANDARD_7Y", "EXTENDED_10Y", "MINIMAL_3Y", "LITIGATION_HOLD"
    ):
        errors.append(f"retention_class invalid: {card['retention_class']}")

    return errors


def load_card(agent_id: str) -> Dict[str, Any]:
    """
    Load and validate a single card by id. Raises RuntimeError on validation error.
    Returns the parsed card dict.
    """
    path = CARDS_DIR / f"{agent_id}.yaml"
    if not path.exists():
        # Allow .json as a fallback authoring format
        path = CARDS_DIR / f"{agent_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"model card not found for agent_id={agent_id}")

    card = _load_yaml_file(path)
    errors = validate_card(card)
    if errors:
        raise RuntimeError(
            f"model card validation failed for {agent_id}: {errors}"
        )
    return card


def register_card(agent_id: str) -> Dict[str, Any]:
    """
    Idempotently load + validate + register a card. Called by each agent at
    module import. If the card is missing or invalid, the agent's import
    raises — governance is treated as a load-bearing dependency.
    """
    if agent_id in MODEL_CARD_REGISTRY:
        return MODEL_CARD_REGISTRY[agent_id]
    card = load_card(agent_id)
    MODEL_CARD_REGISTRY[agent_id] = card
    return card


def list_cards() -> List[Dict[str, Any]]:
    """Return summary view of all registered cards."""
    return [
        {
            "agent_id": c.get("agent_id"),
            "agent_name": c.get("agent_name"),
            "version": c.get("version"),
            "maturity_level": c.get("maturity_level"),
            "blueprint_stage": c.get("blueprint_stage"),
            "fairness_monitored": (c.get("fairness") or {}).get("monitored", False),
            "retention_class": c.get("retention_class"),
        }
        for c in MODEL_CARD_REGISTRY.values()
    ]


def cards_for_export() -> Dict[str, Dict[str, Any]]:
    """Return the entire registry for audit export / API response."""
    return dict(MODEL_CARD_REGISTRY)


def load_all_cards() -> Dict[str, Dict[str, Any]]:
    """
    Eager-load every card in CARDS_DIR. Called once at package import to
    populate the registry. Logs validation errors but does not raise here —
    individual agent imports will raise when they try to register.
    """
    if not CARDS_DIR.exists():
        return MODEL_CARD_REGISTRY
    for path in sorted(CARDS_DIR.glob("*.yaml")):
        try:
            card = _load_yaml_file(path)
            errors = validate_card(card)
            if not errors and "agent_id" in card:
                MODEL_CARD_REGISTRY[card["agent_id"]] = card
        except Exception:
            continue
    for path in sorted(CARDS_DIR.glob("*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
            errors = validate_card(card)
            if not errors and "agent_id" in card:
                MODEL_CARD_REGISTRY[card["agent_id"]] = card
        except Exception:
            continue
    return MODEL_CARD_REGISTRY


# Eager load at module import
load_all_cards()
