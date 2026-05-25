"""
governance/decision_log.py

Decision Record persistence + SHA-256 hash chain for tamper evidence.

Two backends ship in v1:
  · InMemoryStore (default; dev/POC)
  · SQLiteStore   (single-file durable; client demos, small carriers)

Switch via env: FNOL_DECISION_LOG_BACKEND = memory | sqlite
Path via env:   FNOL_DECISION_LOG_DB     = /path/to/file.db  (sqlite only)
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import threading
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Optional, Protocol


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _serialize(dr: Any) -> Dict[str, Any]:
    """Best-effort dataclass → dict serialization."""
    if is_dataclass(dr):
        return asdict(dr)
    if isinstance(dr, dict):
        return dict(dr)
    return dict(getattr(dr, "__dict__", {}))


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# Backend protocol
# ─────────────────────────────────────────────────────────────────────────────

class DecisionLogStore(Protocol):
    def append(self, record: Any) -> None: ...
    def get(self, decision_id: str) -> Optional[Dict[str, Any]]: ...
    def query(self, **filters) -> List[Dict[str, Any]]: ...
    def export_claim(self, claim_number: str) -> List[Dict[str, Any]]: ...
    def last_hash_for_claim(self, claim_number: str) -> Optional[str]: ...
    def compute_hash(self, record: Any, previous_hash: Optional[str]) -> str: ...
    def verify_chain(self, claim_number: str) -> Dict[str, Any]: ...


# ─────────────────────────────────────────────────────────────────────────────
# In-memory backend
# ─────────────────────────────────────────────────────────────────────────────

class InMemoryStore:
    """Default backend: thread-safe in-memory dict."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: Dict[str, Dict[str, Any]] = {}            # decisionId → record
        self._by_claim: Dict[str, List[str]] = {}                # claimNumber → [ids]
        self._last_hash: Dict[str, str] = {}                     # claimNumber → last recordHash

    def compute_hash(self, record: Any, previous_hash: Optional[str]) -> str:
        d = _serialize(record)
        # Hash everything EXCEPT the governance.recordHash field itself
        gov = dict(d.get("governance", {}) or {})
        gov.pop("recordHash", None)
        gov["previousRecordHash"] = previous_hash
        d2 = dict(d)
        d2["governance"] = gov
        return hashlib.sha256(_canonical(d2).encode("utf-8")).hexdigest()

    def append(self, record: Any) -> None:
        d = _serialize(record)
        decision_id = d.get("decisionId")
        claim_number = d.get("claimNumber")
        if not decision_id or not claim_number:
            return
        with self._lock:
            if decision_id in self._records:
                return  # idempotent
            self._records[decision_id] = d
            self._by_claim.setdefault(claim_number, []).append(decision_id)
            rh = (d.get("governance") or {}).get("recordHash")
            if rh:
                self._last_hash[claim_number] = rh

    def get(self, decision_id: str) -> Optional[Dict[str, Any]]:
        return self._records.get(decision_id)

    def query(self, **filters) -> List[Dict[str, Any]]:
        results = list(self._records.values())

        def _f(rec: Dict[str, Any]) -> bool:
            for k, v in filters.items():
                if v is None:
                    continue
                if k == "claim_number":
                    if rec.get("claimNumber") != v: return False
                elif k == "agent":
                    if rec.get("agentName") != v: return False
                elif k == "decision_type":
                    if rec.get("decisionType") != v: return False
                elif k == "hitl_required":
                    if rec.get("hitlRequired") != v: return False
                elif k == "fcra_data_used":
                    if (rec.get("governance") or {}).get("fcraDataUsed") != v: return False
                elif k == "bias_flag_active":
                    if (rec.get("governance") or {}).get("biasFlagActive") != v: return False
                elif k == "maturity_level":
                    if rec.get("maturityLevel") != v: return False
                elif k == "confidence_lt":
                    if not (isinstance(rec.get("confidence"), (int, float)) and rec["confidence"] < v):
                        return False
                elif k == "date_from":
                    if rec.get("timestamp", "") < v: return False
                elif k == "date_to":
                    if rec.get("timestamp", "") > v: return False
            return True

        out = [r for r in results if _f(r)]
        # Newest first
        out.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        limit = filters.get("limit") or 200
        return out[:limit]

    def export_claim(self, claim_number: str) -> List[Dict[str, Any]]:
        ids = self._by_claim.get(claim_number, [])
        recs = [self._records[i] for i in ids if i in self._records]
        recs.sort(key=lambda r: r.get("timestamp", ""))
        return recs

    def last_hash_for_claim(self, claim_number: str) -> Optional[str]:
        return self._last_hash.get(claim_number)

    def verify_chain(self, claim_number: str) -> Dict[str, Any]:
        records = self.export_claim(claim_number)
        if not records:
            return {"valid": True, "records_checked": 0, "broken_at": None,
                    "claimNumber": claim_number, "note": "no records"}
        prev_hash: Optional[str] = None
        for rec in records:
            gov = rec.get("governance") or {}
            stored_prev = gov.get("previousRecordHash")
            stored_curr = gov.get("recordHash")
            if stored_prev != prev_hash:
                return {"valid": False, "broken_at": rec.get("decisionId"),
                        "reason": f"previousRecordHash mismatch (expected {prev_hash}, got {stored_prev})",
                        "records_checked": records.index(rec) + 1,
                        "claimNumber": claim_number}
            recomputed = self.compute_hash(rec, previous_hash=prev_hash)
            if recomputed != stored_curr:
                return {"valid": False, "broken_at": rec.get("decisionId"),
                        "reason": "recordHash recomputation mismatch — record may have been altered",
                        "records_checked": records.index(rec) + 1,
                        "claimNumber": claim_number}
            prev_hash = stored_curr
        return {"valid": True, "records_checked": len(records),
                "broken_at": None, "claimNumber": claim_number,
                "chain_root": records[0].get("governance", {}).get("recordHash"),
                "chain_tip": prev_hash}


# ─────────────────────────────────────────────────────────────────────────────
# SQLite backend
# ─────────────────────────────────────────────────────────────────────────────

class SQLiteStore:
    """Single-file durable store. Same hash-chain semantics as InMemoryStore."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS decision_records (
      decision_id TEXT PRIMARY KEY,
      claim_number TEXT NOT NULL,
      agent_name TEXT NOT NULL,
      decision_type TEXT,
      timestamp TEXT,
      hitl_required INTEGER,
      record_hash TEXT,
      previous_hash TEXT,
      payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_dr_claim ON decision_records(claim_number);
    CREATE INDEX IF NOT EXISTS idx_dr_agent ON decision_records(agent_name);
    CREATE INDEX IF NOT EXISTS idx_dr_ts ON decision_records(timestamp);
    """

    def __init__(self, path: str) -> None:
        self.path = path
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()

    def compute_hash(self, record: Any, previous_hash: Optional[str]) -> str:
        return InMemoryStore.compute_hash(self, record, previous_hash)  # type: ignore[arg-type]

    def append(self, record: Any) -> None:
        d = _serialize(record)
        decision_id = d.get("decisionId")
        if not decision_id:
            return
        gov = d.get("governance") or {}
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO decision_records VALUES (?,?,?,?,?,?,?,?,?)",
                    (decision_id, d.get("claimNumber"), d.get("agentName"),
                     d.get("decisionType"), d.get("timestamp"),
                     int(bool(d.get("hitlRequired"))),
                     gov.get("recordHash"), gov.get("previousRecordHash"),
                     _canonical(d)),
                )
                self._conn.commit()
            except Exception:
                pass

    def get(self, decision_id: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT payload_json FROM decision_records WHERE decision_id=?",
            (decision_id,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def query(self, **filters) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if filters.get("claim_number"): clauses.append("claim_number=?"); params.append(filters["claim_number"])
        if filters.get("agent"): clauses.append("agent_name=?"); params.append(filters["agent"])
        if filters.get("decision_type"): clauses.append("decision_type=?"); params.append(filters["decision_type"])
        if filters.get("hitl_required") is not None: clauses.append("hitl_required=?"); params.append(int(filters["hitl_required"]))
        if filters.get("date_from"): clauses.append("timestamp>=?"); params.append(filters["date_from"])
        if filters.get("date_to"): clauses.append("timestamp<=?"); params.append(filters["date_to"])
        sql = "SELECT payload_json FROM decision_records"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(filters.get("limit") or 200)
        cur = self._conn.execute(sql, tuple(params))
        recs = [json.loads(r[0]) for r in cur.fetchall()]
        # Apply governance-level filters in Python (rare path)
        if filters.get("fcra_data_used") is not None:
            recs = [r for r in recs if (r.get("governance") or {}).get("fcraDataUsed") == filters["fcra_data_used"]]
        if filters.get("bias_flag_active") is not None:
            recs = [r for r in recs if (r.get("governance") or {}).get("biasFlagActive") == filters["bias_flag_active"]]
        return recs

    def export_claim(self, claim_number: str) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT payload_json FROM decision_records WHERE claim_number=? ORDER BY timestamp ASC",
            (claim_number,))
        return [json.loads(r[0]) for r in cur.fetchall()]

    def last_hash_for_claim(self, claim_number: str) -> Optional[str]:
        cur = self._conn.execute(
            "SELECT record_hash FROM decision_records WHERE claim_number=? ORDER BY timestamp DESC LIMIT 1",
            (claim_number,))
        row = cur.fetchone()
        return row[0] if row else None

    def verify_chain(self, claim_number: str) -> Dict[str, Any]:
        # Delegate logic to InMemoryStore by loading and re-checking
        records = self.export_claim(claim_number)
        if not records:
            return {"valid": True, "records_checked": 0, "broken_at": None,
                    "claimNumber": claim_number, "note": "no records"}
        prev_hash: Optional[str] = None
        for rec in records:
            gov = rec.get("governance") or {}
            stored_prev = gov.get("previousRecordHash")
            stored_curr = gov.get("recordHash")
            if stored_prev != prev_hash:
                return {"valid": False, "broken_at": rec.get("decisionId"),
                        "reason": "previousRecordHash mismatch",
                        "claimNumber": claim_number}
            recomputed = self.compute_hash(rec, previous_hash=prev_hash)
            if recomputed != stored_curr:
                return {"valid": False, "broken_at": rec.get("decisionId"),
                        "reason": "recordHash recomputation mismatch",
                        "claimNumber": claim_number}
            prev_hash = stored_curr
        return {"valid": True, "records_checked": len(records),
                "broken_at": None, "claimNumber": claim_number,
                "chain_tip": prev_hash}


# ─────────────────────────────────────────────────────────────────────────────
# Singleton accessor + module-level helpers
# ─────────────────────────────────────────────────────────────────────────────

_STORE: Optional[Any] = None


def set_backend(backend: str = "memory", path: Optional[str] = None) -> None:
    """Reset the singleton store. backend ∈ {'memory','sqlite'}."""
    global _STORE
    if backend == "sqlite":
        path = path or os.environ.get("FNOL_DECISION_LOG_DB", "/tmp/fnol_decision_log.db")
        _STORE = SQLiteStore(path)
    else:
        _STORE = InMemoryStore()


def get_store() -> Any:
    global _STORE
    if _STORE is None:
        backend = os.environ.get("FNOL_DECISION_LOG_BACKEND", "memory").lower()
        set_backend(backend)
    return _STORE


# Convenience pass-throughs
def append(record: Any) -> None: get_store().append(record)
def query(**filters) -> List[Dict[str, Any]]: return get_store().query(**filters)
def verify_chain(claim_number: str) -> Dict[str, Any]: return get_store().verify_chain(claim_number)
def export_claim(claim_number: str) -> List[Dict[str, Any]]: return get_store().export_claim(claim_number)
