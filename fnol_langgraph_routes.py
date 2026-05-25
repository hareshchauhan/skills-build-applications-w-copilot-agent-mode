"""
FNOL Intelligence Platform — L3 LangGraph Live Routes
======================================================
Resolves RSK-03: completes /langgraph/status and adds real-time
WebSocket streaming so the UX tab can show live node-completion events.

Endpoints (prefix /api/v1/fnol/langgraph):
  GET   /status                  — system-level L3 status (RSK-03 WIP → done)
  POST  /run                     — async claim submit → thread_id (202)
  WS    /ws/{thread_id}          — real-time node-event stream
  GET   /hitl                    — threads awaiting HITL decision
  PUT   /hitl/{thread_id}        — post HITL decision / resume suspended graph

Wire into fnol_api_server.py:
    import fnol_langgraph_routes as _lg_live_routes
    app.include_router(_lg_live_routes.router)

Streaming design
----------------
POST /run   creates an asyncio.Queue per thread_id, launches a background
            asyncio Task that runs graph.stream() in a ThreadPoolExecutor,
            and pushes structured JSON events onto the queue.

WS /ws/{t}  hot-path  — reads from the queue in real time (for threads
            submitted via POST /run).
            poll-path — polls lg.get_thread_state() at 400 ms intervals
            for threads submitted via the synchronous POST /v3/claims.

Both paths emit the same message schema so the UI works identically:
  {"type":"connected",      "thread_id":"TH-...","ts":"..."}
  {"type":"node_complete",  "node":"...", "stage_id":"...", "elapsed_ms":N,"ts":"..."}
  {"type":"hitl_required",  "thread_id":"...","pending":[...],"ts":"..."}
  {"type":"graph_complete", "final_status":"...", "total_ms":N,"ts":"..."}
  {"type":"state_update",   "state":{...},"ts":"..."}
  {"type":"ping",           "ts":"..."}
  {"type":"error",          "message":"...","ts":"..."}

Client → Server:
  {"action":"ping"}

Internal sentinel (never sent to client):
  {"type":"__DONE__"}
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

import fnol_langgraph_engine as lg
from fnol_claim import Claim
from fnol_api_deps import client_error, server_error, _check_api_key
from fnol_rbac import require_roles, require_roles_rate_limited, Role, CLAIMS_ROLES, READ_ROLES, SUPERVISOR_UP

log = logging.getLogger("fnol.lg.live")

router = APIRouter(
    prefix="/api/v1/fnol/langgraph",
    tags=["L3 LangGraph Live"],
)

# ── Constants ─────────────────────────────────────────────────────────────────
_WS_PING_INTERVAL = 15      # seconds between server pings on idle WS
_WS_POLL_INTERVAL = 0.4     # seconds between state polls on poll-path
_QUEUE_MAXSIZE    = 256     # max buffered events per thread

# ── Node-name → human stage-id map ───────────────────────────────────────────
_NODE_STAGE: Dict[str, str] = {
    "s0_s1_intake_node":   "S0/S1",
    "s2_coverage_node":    "S2",
    "s3_triage_node":      "S3",
    "hitl_gate_triage":    "HITL·TRIAGE",
    "parallel_s4_node":    "S4A/S4B",
    "siu_hold_node":       "SIU·HOLD",
    "a11_total_loss_node": "A11",
    "s5_bi_node":          "S5",
    "hitl_gate_bi":        "HITL·BI",
    "s6_settlement_node":  "S6",
    "s7_subrogation_node": "S7",
    "confirmed_fraud_node":"FRAUD·DENIAL",
}

# ── In-process thread state ───────────────────────────────────────────────────
# These are process-local (POC); replace with Redis pub/sub in production.
_THREAD_QUEUES: Dict[str, asyncio.Queue] = {}   # thread_id → event queue
_THREAD_META:   Dict[str, Dict]          = {}   # thread_id → {claim_id, started_at, status}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_lg(label: str = "endpoint") -> None:
    if not getattr(lg, "LANGGRAPH_AVAILABLE", False):
        raise client_error(
            "LangGraph not installed. "
            "pip install langgraph langgraph-checkpoint-sqlite",
            503,
        )


# ── Pydantic models ───────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    """Async L3 claim submission payload."""
    claim: Claim


class HitlDecision(BaseModel):
    """Adjuster or SIU decision to resume a suspended graph thread."""
    decision:           Optional[str]   = "APPROVED"
    adjuster_id:        Optional[str]   = None
    adjuster_notes:     Optional[str]   = None
    approved_bi_usd:    Optional[float] = None
    investigator_notes: Optional[str]   = None
    disposition:        Optional[str]   = None


# ── Background graph runner ───────────────────────────────────────────────────

async def _bg_run_graph(
    thread_id: str,
    claim: Claim,
    queue: asyncio.Queue,
) -> None:
    """
    Run graph.stream() inside a ThreadPoolExecutor and push JSON events
    to the asyncio.Queue so the WebSocket handler can forward them.

    Uses loop.call_soon_threadsafe() for cross-thread queue writes.
    Events are dropped silently if the queue is full (WS probably gone).
    """
    loop = asyncio.get_running_loop()

    def _enqueue(evt: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, evt)
        except Exception:
            pass  # queue full or WS already closed

    def _sync_stream() -> None:
        if not getattr(lg, "LANGGRAPH_AVAILABLE", False):
            _enqueue({"type": "error",
                      "message": "LangGraph not installed on this server.",
                      "ts": _now()})
            _enqueue({"type": "__DONE__"})
            return

        graph = lg._get_graph()  # noqa: SLF001 — internal access is intentional
        if graph is None:
            _enqueue({"type": "error",
                      "message": "LangGraph graph build failed — check server logs.",
                      "ts": _now()})
            _enqueue({"type": "__DONE__"})
            return

        config  = lg._make_config(thread_id)  # noqa: SLF001
        init    = lg._initial_state(claim, thread_id)  # noqa: SLF001
        t0      = time.time()

        try:
            for chunk in graph.stream(init, config=config, stream_mode="updates"):
                if "__interrupt__" in chunk:
                    interrupt_payload = chunk["__interrupt__"]
                    _enqueue({
                        "type":      "hitl_required",
                        "thread_id": thread_id,
                        "interrupt": interrupt_payload,
                        "ts":        _now(),
                    })
                    _enqueue({"type": "__DONE__"})
                    return

                # Normal node completion(s) in this chunk
                for node_name, update in chunk.items():
                    stage_id = _NODE_STAGE.get(node_name, node_name)
                    elapsed  = int((time.time() - t0) * 1000)
                    evt: Dict[str, Any] = {
                        "type":       "node_complete",
                        "node":       node_name,
                        "stage_id":   stage_id,
                        "elapsed_ms": elapsed,
                        "ts":         _now(),
                    }
                    # Include summary of stage outputs (keys only to avoid huge payloads)
                    if isinstance(update, dict):
                        sm = update.get("stage_meta", {})
                        so = update.get("stage_outputs", {})
                        if sm:
                            # Include the new stage_meta entries
                            evt["stage_meta"] = {
                                sid: {"status": v.get("status", ""),
                                      "elapsed_ms": v.get("elapsed_ms", 0)}
                                for sid, v in sm.items()
                            }
                        if so:
                            evt["stage_output_keys"] = {
                                sid: list(v.keys()) if isinstance(v, dict) else []
                                for sid, v in so.items()
                            }
                    _enqueue(evt)

            # Graph ran to END — fetch final state from checkpointer
            snapshot = graph.get_state(config)
            sv = snapshot.values if snapshot else {}
            _enqueue({
                "type":         "graph_complete",
                "final_status": sv.get("final_status", "—"),
                "total_ms":     int((time.time() - t0) * 1000),
                "thread_id":    thread_id,
                "ts":           _now(),
            })

        except Exception as exc:
            log.error("bg_run_graph error for thread %s: %s", thread_id, exc)
            _enqueue({"type": "error", "message": str(exc), "ts": _now()})
        finally:
            _enqueue({"type": "__DONE__"})
            # Update in-process meta to completed
            if thread_id in _THREAD_META:
                _THREAD_META[thread_id]["status"] = "DONE"

    await loop.run_in_executor(None, _sync_stream)


# ── WebSocket ping loop ───────────────────────────────────────────────────────

async def _ws_ping_loop(ws: WebSocket) -> None:
    """Send a keepalive ping every _WS_PING_INTERVAL seconds."""
    while True:
        await asyncio.sleep(_WS_PING_INTERVAL)
        try:
            await ws.send_json({"type": "ping", "ts": _now()})
        except Exception:
            break


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/status")
def langgraph_status(_: str = Depends(require_roles(*READ_ROLES))):
    """
    System-level L3 status — RSK-03 completion.

    Returns L3 engine health, all active/recent threads from the checkpoint
    store, HITL queue depth, in-flight async runs (POST /run), and the
    full graph node topology.
    """
    health  = lg.get_graph_health()
    threads: List[Dict] = []
    hitl_pending: List[Dict] = []

    if health.get("l3_enabled"):
        try:
            threads = lg.list_threads(limit=100)
            hitl_pending = [t for t in threads if t.get("pending_hitl")]
        except Exception as exc:
            log.warning("langgraph_status list_threads error: %s", exc)

    # In-flight runs that were submitted via POST /run and are still executing
    # (not yet checkpointed to completion so not in list_threads result)
    checkpointed_ids = {t.get("thread_id") for t in threads}
    in_flight = [
        {"thread_id": tid, **meta}
        for tid, meta in _THREAD_META.items()
        if tid not in checkpointed_ids
    ]

    return {
        "l3_enabled":       health.get("l3_enabled", False),
        "status":           health.get("status", "unavailable"),
        "graph_nodes":      health.get("graph_nodes", []),
        "checkpoint_db":    health.get("checkpoint_db", ""),
        "pipeline_version": health.get("pipeline_version", ""),
        "active_threads":   len(threads),
        "in_flight_count":  len(in_flight),
        "hitl_queue_depth": len(hitl_pending),
        "threads":          threads,
        "in_flight":        in_flight,
        "hitl_pending":     hitl_pending,
        "ts":               _now(),
    }


@router.post("/run", status_code=202)
async def async_run(body: RunRequest, _: str = Depends(require_roles_rate_limited(*CLAIMS_ROLES))):
    """
    Async L3 claim submission.

    Returns HTTP 202 with a thread_id immediately; the graph executes in
    a background task.  Connect WebSocket to /ws/{thread_id} to receive
    live node-completion events as the graph progresses.

    For HITL-suspended threads: POST /hitl/{thread_id} to resume.
    """
    _require_lg("POST /run")

    thread_id = f"TH-{uuid.uuid4().hex[:12].upper()}"
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _THREAD_QUEUES[thread_id] = queue
    _THREAD_META[thread_id] = {
        "claim_id":   body.claim.claim_id,
        "started_at": _now(),
        "status":     "RUNNING",
    }

    asyncio.create_task(_bg_run_graph(thread_id, body.claim, queue))

    return {
        "thread_id":  thread_id,
        "claim_id":   body.claim.claim_id,
        "status":     "RUNNING",
        "ws_url":     f"/api/v1/fnol/langgraph/ws/{thread_id}",
        "poll_url":   f"/api/v1/fnol/v3/claims/{thread_id}",
        "hitl_url":   f"/api/v1/fnol/langgraph/hitl/{thread_id}",
        "message": (
            "Graph executing in background. "
            f"Connect WebSocket to /api/v1/fnol/langgraph/ws/{thread_id}"
            "?api_key=<key> for live node events."
        ),
        "ts": _now(),
    }


@router.websocket("/ws/{thread_id}")
async def ws_stream(websocket: WebSocket, thread_id: str):
    """
    WebSocket endpoint — real-time LangGraph node-event stream.

    Authentication: pass ?api_key=<key> in the WS URL query string
    (WS headers are not reliably forwarded by all browser WS clients).

    Hot path (thread submitted via POST /run):
      Events are forwarded from the background task queue in real time
      with sub-millisecond latency relative to node completions.

    Poll path (thread submitted via POST /v3/claims):
      Server polls get_thread_state() at 400 ms intervals and emits
      node_complete events for any newly completed stages.

    In both paths the connection terminates automatically when the graph
    reaches END, hits a HITL interrupt, or errors.
    """
    # ── Auth via query param (browser WS clients can't set custom headers) ──
    api_key = websocket.query_params.get("api_key", "")
    try:
        _check_api_key(api_key)
    except Exception:
        await websocket.close(code=4001)  # 4001 = Unauthorized
        return

    await websocket.accept()
    log.info("WS /ws/%s accepted", thread_id)

    try:
        await websocket.send_json({
            "type":      "connected",
            "thread_id": thread_id,
            "ts":        _now(),
        })

        queue = _THREAD_QUEUES.get(thread_id)

        if queue is not None:
            # ── Hot path: background task running via POST /run ──────────────
            ping_task = asyncio.create_task(_ws_ping_loop(websocket))
            try:
                while True:
                    try:
                        evt = await asyncio.wait_for(
                            queue.get(),
                            timeout=_WS_PING_INTERVAL + 5,
                        )
                    except asyncio.TimeoutError:
                        # Keepalive already handled by ping_task; just loop
                        continue

                    if evt.get("type") == "__DONE__":
                        break

                    await websocket.send_json(evt)

                    # Update in-process meta
                    if evt.get("type") == "graph_complete":
                        if thread_id in _THREAD_META:
                            _THREAD_META[thread_id]["status"] = evt.get("final_status", "DONE")

            finally:
                ping_task.cancel()
                _THREAD_QUEUES.pop(thread_id, None)
                _THREAD_META.pop(thread_id, None)

        else:
            # ── Poll path: thread submitted via POST /v3/claims (sync) ───────
            if not getattr(lg, "LANGGRAPH_AVAILABLE", False):
                await websocket.send_json({
                    "type":    "error",
                    "message": "LangGraph not installed on this server.",
                    "ts":      _now(),
                })
                return

            prev_stages: set = set()
            prev_status: str = ""
            tick = 0

            while True:
                await asyncio.sleep(_WS_POLL_INTERVAL)
                tick += 1

                # Keepalive every ~15 s
                if tick * _WS_POLL_INTERVAL >= _WS_PING_INTERVAL:
                    await websocket.send_json({"type": "ping", "ts": _now()})
                    tick = 0

                try:
                    state = lg.get_thread_state(thread_id)
                except KeyError:
                    await websocket.send_json({
                        "type":    "error",
                        "message": f"Thread {thread_id} not found in checkpoint store.",
                        "ts":      _now(),
                    })
                    break
                except Exception as exc:
                    await websocket.send_json({
                        "type": "error", "message": str(exc), "ts": _now()
                    })
                    break

                # Emit newly completed stages
                cur_stages = set(state.get("stage_meta", {}).keys())
                new_stages  = cur_stages - prev_stages
                for sid in sorted(new_stages):
                    sm = state["stage_meta"].get(sid, {})
                    await websocket.send_json({
                        "type":       "node_complete",
                        "stage_id":   sid,
                        "node":       sm.get("agent", sid),
                        "status":     sm.get("status", "ok"),
                        "elapsed_ms": sm.get("elapsed_ms", 0),
                        "ts":         _now(),
                    })
                prev_stages = cur_stages

                cur_status = state.get("final_status", "")

                # Emit HITL event when pending list changes
                if state.get("pending_hitl") and cur_status != prev_status:
                    await websocket.send_json({
                        "type":      "hitl_required",
                        "thread_id": thread_id,
                        "pending":   state["pending_hitl"],
                        "ts":        _now(),
                    })

                # Terminal conditions
                if state.get("completed_at") or cur_status not in ("", "OPEN"):
                    await websocket.send_json({
                        "type":         "graph_complete",
                        "final_status": cur_status,
                        "thread_id":    thread_id,
                        "state":        state,
                        "ts":           _now(),
                    })
                    break

                prev_status = cur_status

    except WebSocketDisconnect:
        log.info("WS /ws/%s disconnected", thread_id)
    except Exception as exc:
        log.error("WS /ws/%s error: %s", thread_id, exc)
        try:
            await websocket.send_json({"type": "error", "message": str(exc), "ts": _now()})
        except Exception:
            pass
    finally:
        _THREAD_QUEUES.pop(thread_id, None)
        log.info("WS /ws/%s closed", thread_id)


@router.get("/hitl")
def list_hitl(limit: int = 50, _: str = Depends(require_roles(*SUPERVISOR_UP, Role.SIU_INVESTIGATOR))):
    """
    Return all threads currently suspended at a HITL interrupt gate.
    Used by the Live Pipeline UI to surface pending adjuster actions.
    """
    if not getattr(lg, "LANGGRAPH_AVAILABLE", False):
        return {"hitl_pending": [], "count": 0, "l3_enabled": False}
    try:
        threads = lg.list_threads(limit=min(limit, 200))
    except Exception as exc:
        raise server_error("list_threads failed", exc)
    pending = [t for t in threads if t.get("pending_hitl")]
    return {
        "hitl_pending": pending,
        "count":        len(pending),
        "l3_enabled":   True,
        "ts":           _now(),
    }


@router.put("/hitl/{thread_id}")
def resolve_hitl(
    thread_id: str,
    decision: HitlDecision,
    _: str = Depends(require_roles(*SUPERVISOR_UP)),
):
    """
    Submit a HITL decision to resume a suspended L3 graph thread.

    Typical payloads:
      Triage gate  — {"decision": "APPROVED", "adjuster_id": "ADJ-001"}
      SIU gate     — {"disposition": "CLEARED", "investigator_notes": "..."}
      BI gate      — {"decision": "ADJUST_OFFER", "approved_bi_usd": 45000}
    """
    _require_lg("PUT /hitl")
    decision_dict = decision.model_dump(exclude_none=True)
    try:
        result = lg.resume_thread(thread_id, decision_dict)
    except KeyError:
        raise client_error(f"Thread {thread_id} not found", 404)
    except Exception as exc:
        raise server_error("resume_thread failed", exc)
    return result
