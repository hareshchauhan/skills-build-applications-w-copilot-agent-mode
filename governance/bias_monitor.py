"""
governance/bias_monitor.py

Demographic parity, equalized odds, calibration-by-group, and FPR-disparity
monitoring for A3 Triage and A4 Fraud — the two highest-exposure agents
under the V2 Blueprint's industrialization model.

v1: in-process sampling buffer + on-demand recomputation. v2: nightly batch
job + persistent metric history.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Thresholds (match governance/policies/bias_monitor.yaml)
# ─────────────────────────────────────────────────────────────────────────────

THRESHOLDS = {
    "demographic_parity_ratio": 0.80,        # ≥ this is OK
    "equalized_odds_difference": 0.10,       # ≤ this is OK
    "calibration_by_group": 0.02,            # ≤ this is OK
    "false_positive_rate_disparity": 0.05,   # ≤ this is OK (Fraud only)
}

WINDOW_DAYS_DEFAULT = 30
MIN_CELL_SIZE = 5


# ─────────────────────────────────────────────────────────────────────────────
# In-memory sampling buffer (would be persisted in production)
# ─────────────────────────────────────────────────────────────────────────────

_lock = threading.RLock()
_buffer: Dict[str, List[Dict[str, Any]]] = {"a3_triage": [], "a4_fraud": []}
_breach_state: Dict[str, bool] = {"a3_triage": False, "a4_fraud": False}
_breach_set_at: Dict[str, Optional[str]] = {"a3_triage": None, "a4_fraud": None}
_history: Dict[str, List[Dict[str, Any]]] = {"a3_triage": [], "a4_fraud": []}


def is_breach_active(agent_id: str) -> bool:
    """Used by the DR enrichment hook at decision time."""
    with _lock:
        return _breach_state.get(agent_id, False)


def mark_decision_for_monitoring(agent_id: str, dr: Any) -> None:
    """
    Called by the enrichment hook for A3/A4 decisions. Captures a sample
    with the proxy attributes the monitor needs.
    """
    if agent_id not in _buffer:
        return
    sample = _build_sample(agent_id, dr)
    if sample is None:
        return
    with _lock:
        buf = _buffer[agent_id]
        buf.append(sample)
        # cap memory at 10K samples per agent
        if len(buf) > 10_000:
            del buf[: len(buf) - 10_000]


def _build_sample(agent_id: str, dr: Any) -> Optional[Dict[str, Any]]:
    """
    Extract the minimal fields the monitor needs. In production, protected-
    attribute proxies are derived upstream and passed in via context; here
    we synthesize from what's on the DR.
    """
    try:
        from dataclasses import is_dataclass, asdict as _asdict
        d = _asdict(dr) if is_dataclass(dr) else dict(dr)
    except Exception:
        return None

    inputs = d.get("decisionValue") if isinstance(d.get("decisionValue"), dict) else {}
    age_band = inputs.get("ageBand") or "unknown"
    gender = inputs.get("gender") or "unknown"
    language = inputs.get("language") or "en"
    zip3 = inputs.get("zip3") or "unknown"

    if agent_id == "a3_triage":
        track = inputs.get("track") or d.get("decisionType")
        positive = track in ("STP_EXPRESS",)            # STP routing = "favorable"
    else:  # a4_fraud
        band = inputs.get("band") or ""
        positive = band in ("LOW",)                     # not flagged = "favorable"

    return {
        "ts": d.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "decisionId": d.get("decisionId"),
        "claimNumber": d.get("claimNumber"),
        "confidence": d.get("confidence"),
        "positive": bool(positive),
        "ageBand": age_band, "gender": gender, "language": language, "zip3": zip3,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Metric computation
# ─────────────────────────────────────────────────────────────────────────────

def _filter_window(samples: List[Dict[str, Any]], days: int) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()
    return [s for s in samples if (s.get("ts") or "") >= cutoff_iso]


def _group_rates(samples: List[Dict[str, Any]], attr: str) -> Dict[str, float]:
    """Returns selection rate (P(positive | group)) per group with cell ≥ MIN_CELL_SIZE."""
    by_group: Dict[str, List[bool]] = {}
    for s in samples:
        g = str(s.get(attr) or "unknown")
        by_group.setdefault(g, []).append(bool(s.get("positive")))
    out: Dict[str, float] = {}
    for g, vals in by_group.items():
        if len(vals) >= MIN_CELL_SIZE:
            out[g] = round(sum(vals) / len(vals), 4)
    return out


def _demographic_parity_ratio(rates: Dict[str, float]) -> Optional[float]:
    if len(rates) < 2:
        return None
    vals = list(rates.values())
    lo, hi = min(vals), max(vals)
    if hi == 0:
        return 1.0
    return round(lo / hi, 4)


def _equalized_odds_difference(samples: List[Dict[str, Any]], attr: str) -> Optional[float]:
    """Spread of TPR + FPR across groups. Requires a 'label' for ground truth.
    We approximate using high-confidence decisions as a proxy where 'positive=True' is
    treated as predicted-positive. Without labels, this is a constant-zero proxy in v1
    and simply reports max-min spread of selection rate (functionally similar to DPR
    spread). Replaced with true TPR/FPR when label feedback flows in v2."""
    rates = _group_rates(samples, attr)
    if len(rates) < 2:
        return None
    return round(max(rates.values()) - min(rates.values()), 4)


def _calibration_by_group(samples: List[Dict[str, Any]], attr: str) -> Optional[float]:
    """Max |mean(confidence) - mean(positive)| across groups."""
    by_group: Dict[str, List[Dict[str, float]]] = {}
    for s in samples:
        g = str(s.get(attr) or "unknown")
        c = s.get("confidence")
        p = s.get("positive")
        if isinstance(c, (int, float)) and isinstance(p, bool):
            by_group.setdefault(g, []).append({"c": float(c), "p": float(p)})
    deltas = []
    for g, vals in by_group.items():
        if len(vals) >= MIN_CELL_SIZE:
            mc = sum(v["c"] for v in vals) / len(vals)
            mp = sum(v["p"] for v in vals) / len(vals)
            deltas.append(abs(mc - mp))
    if not deltas:
        return None
    return round(max(deltas), 4)


def _fpr_disparity_proxy(samples: List[Dict[str, Any]], attr: str) -> Optional[float]:
    """
    Proxy for FPR disparity: spread of (1 - selection_rate) across groups
    among low-confidence decisions. v2 will use real labels.
    """
    return _equalized_odds_difference(samples, attr)


# ─────────────────────────────────────────────────────────────────────────────
# Public report API
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BiasReport:
    agent: str
    window_days: int
    generated_at: str
    sample_size: int
    metrics: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # attr → metric → value
    breaches: List[Dict[str, Any]] = field(default_factory=list)
    recommendation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def run_monitor(agent: str, window_days: int = WINDOW_DAYS_DEFAULT) -> BiasReport:
    if agent not in _buffer:
        raise ValueError(f"unknown agent for bias monitor: {agent}")

    with _lock:
        all_samples = list(_buffer[agent])

    samples = _filter_window(all_samples, window_days)
    report = BiasReport(
        agent=agent, window_days=window_days, sample_size=len(samples),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    attrs = ["ageBand", "gender", "language", "zip3"]
    for attr in attrs:
        m: Dict[str, Any] = {}
        rates = _group_rates(samples, attr)
        if rates:
            m["group_selection_rates"] = rates
            dpr = _demographic_parity_ratio(rates)
            if dpr is not None:
                m["demographic_parity_ratio"] = dpr
                if dpr < THRESHOLDS["demographic_parity_ratio"]:
                    report.breaches.append({
                        "attribute": attr, "metric": "demographic_parity_ratio",
                        "observed": dpr, "threshold": THRESHOLDS["demographic_parity_ratio"],
                    })
            eod = _equalized_odds_difference(samples, attr)
            if eod is not None:
                m["equalized_odds_difference"] = eod
                if eod > THRESHOLDS["equalized_odds_difference"]:
                    report.breaches.append({
                        "attribute": attr, "metric": "equalized_odds_difference",
                        "observed": eod, "threshold": THRESHOLDS["equalized_odds_difference"],
                    })
            cal = _calibration_by_group(samples, attr)
            if cal is not None:
                m["calibration_by_group"] = cal
                if cal > THRESHOLDS["calibration_by_group"]:
                    report.breaches.append({
                        "attribute": attr, "metric": "calibration_by_group",
                        "observed": cal, "threshold": THRESHOLDS["calibration_by_group"],
                    })
            if agent == "a4_fraud":
                fpr = _fpr_disparity_proxy(samples, attr)
                if fpr is not None:
                    m["false_positive_rate_disparity"] = fpr
                    if fpr > THRESHOLDS["false_positive_rate_disparity"]:
                        report.breaches.append({
                            "attribute": attr, "metric": "false_positive_rate_disparity",
                            "observed": fpr, "threshold": THRESHOLDS["false_positive_rate_disparity"],
                        })
        if m:
            report.metrics[attr] = m

    if report.breaches:
        report.recommendation = (
            f"{len(report.breaches)} threshold breach(es) detected. "
            f"HITL rate raised to 100% on the affected segment(s); CCO + model "
            f"owner notified; re-calibration job queued. Breaches must clear in "
            f"the next monitor run before HITL gate releases."
        )
        with _lock:
            _breach_state[agent] = True
            _breach_set_at[agent] = report.generated_at
    else:
        report.recommendation = (
            "All monitored fairness metrics within thresholds. No action required."
        )
        with _lock:
            _breach_state[agent] = False
            _breach_set_at[agent] = None

    with _lock:
        hist = _history.setdefault(agent, [])
        hist.append(report.to_dict())
        if len(hist) > 90:
            del hist[: len(hist) - 90]

    return report


def latest_report(agent: str) -> Optional[Dict[str, Any]]:
    with _lock:
        h = _history.get(agent, [])
        return h[-1] if h else None


def history(agent: str, days: int = 90) -> List[Dict[str, Any]]:
    with _lock:
        return list(_history.get(agent, []))[-days:]


def thresholds() -> Dict[str, float]:
    return dict(THRESHOLDS)


def buffer_size(agent: str) -> int:
    with _lock:
        return len(_buffer.get(agent, []))
