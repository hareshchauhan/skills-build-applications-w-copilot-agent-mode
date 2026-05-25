#!/usr/bin/env python3
"""
FNOL Intelligence Platform — Full Stack Smoke Test
===================================================
Runs against a live server at localhost:8000 (or $FNOL_HOST:$FNOL_PORT).

Usage:
    python smoke_test.py                   # auto-detect key from fnol_settings.py
    python smoke_test.py --key <your-key>  # override key
    python smoke_test.py --fast            # skip LLM-backed routes (copilot, letter, convo)
    python smoke_test.py --stop-on-fail    # halt at first failure

Exit code: 0 = all pass, 1 = any failure.
"""

import argparse, json, os, sys, time, uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:
    print("httpx required: pip install httpx"); sys.exit(1)

# ── ANSI colours ─────────────────────────────────────────────────────────────
RESET="\033[0m"; GREEN="\033[92m"; RED="\033[91m"; YELLOW="\033[93m"
BOLD="\033[1m"; DIM="\033[2m"; CYAN="\033[96m"

# ── Key resolution ─────────────────────────────────────────────────────────────
def _detect_key() -> str:
    """Resolve the API key the smoke test will send. The key must come from
    `FNOL_API_KEY` (or be passed via --key). We deliberately do NOT ship a
    hardcoded fallback — a fallback in test code would 1) authenticate against
    any reachable server that happens to share the key, and 2) bake a usable
    production credential into the repo.

    The server-side `KNOWN_DEFAULT_API_KEYS` set blocks sentinel values from
    booting the server, so even an accidentally-shipped weak key cannot grant
    access via this harness.
    """
    env_key = os.getenv("FNOL_API_KEY", "").strip()
    if not env_key:
        print(f"{RED}FATAL:{RESET} FNOL_API_KEY is not set.\n"
              f"  Set the same key the server is running with:\n"
              f'      $env:FNOL_API_KEY = "<your-key>"     (PowerShell)\n'
              f'      export FNOL_API_KEY=<your-key>       (bash)\n'
              f"  Or pass it explicitly:  python smoke_test.py --key <your-key>",
              file=sys.stderr)
        sys.exit(2)
    return env_key

# ── CLI ────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--host", default=os.getenv("FNOL_HOST", "127.0.0.1"))
parser.add_argument("--port", default=int(os.getenv("FNOL_PORT","8000")), type=int)
parser.add_argument("--key",  default=None, help="Override API key (default: auto-detect from fnol_settings.py)")
parser.add_argument("--fast", action="store_true", help="Skip LLM-backed routes")
parser.add_argument("--stop-on-fail", action="store_true")
args = parser.parse_args()

API_KEY = args.key if args.key else _detect_key()
BASE    = f"http://{args.host}:{args.port}"
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
client  = httpx.Client(base_url=BASE, headers=HEADERS, timeout=90.0)

# ── State shared across tests ──────────────────────────────────────────────────
results: List[Tuple[str,str,bool,str]] = []
_claim_id:        Optional[str] = None
_fraud_claim_id:  Optional[str] = None
_tl_claim_id:     Optional[str] = None
_eval_id:         Optional[str] = None
_siu_id:          Optional[str] = None
_pay_id:          Optional[str] = None
_thread_id:       Optional[str] = None
_session_id:      Optional[str] = None
_l3_enabled:      bool          = False

def ok(group, name, detail=""):
    results.append((group, name, True, detail))
    print(f"  {GREEN}✓{RESET} {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))

def fail(group, name, detail=""):
    results.append((group, name, False, detail))
    print(f"  {RED}✗{RESET} {name}  {detail}")
    if args.stop_on_fail:
        _summary(); sys.exit(1)

def warn(msg):
    print(f"  {YELLOW}⚠{RESET} {msg}")

def section(name):
    print(f"\n{BOLD}── {name}{RESET}")

def req(method, path, body=None, expected=200):
    """Fire one request, return (status_code, data_dict)."""
    try:
        if   method == "GET":    r = client.get(path)
        elif method == "POST":   r = client.post(path, content=json.dumps(body or {}))
        elif method == "DELETE": r = client.delete(path)
        else: return -1, {}
        try:    data = r.json()
        except: data = {}
        return r.status_code, data
    except httpx.ConnectError:
        return 0, {"error": "Connection refused"}
    except Exception as e:
        return -1, {"error": str(e)}

def check(group, name, method, path, body=None, expected=200, key=None):
    """Run one check. Returns response data dict on pass, None on fail."""
    sc, data = req(method, path, body, expected)
    if sc == 0:
        fail(group, name, f"Connection refused — server not running at {BASE}")
        return None
    if sc != expected:
        fail(group, name, f"HTTP {sc} (expected {expected}) — {str(data)[:120]}")
        return None
    detail = f"{key}={str(data.get(key,''))[:60]}" if key and isinstance(data, dict) else ""
    ok(group, name, detail)
    return data

# ── Demo payloads ──────────────────────────────────────────────────────────────
DEMO1 = {
    "policy_number":"POC-POL-00123","loss_date_time":"2026-05-10T14:25:00Z",
    "loss_location":"Houston, TX","loss_cause":"REAR_END_COLLISION",
    "loss_description":"Stopped at red light, struck from behind. Mild whiplash.",
    "reporter_name":"Aria Castillo","reporter_phone":"+1-713-555-0142",
    "estimated_loss_usd":4800,"vehicle_acv_usd":22500,
    "photo_count":6,"photo_quality_score":0.82,"drivable_indicator":True,
    "injury_reported":True,"injury_severity":"MINOR","liability_clear":True,
    "rear_ended_by_other":True,"third_party_carrier":"ACME Mutual",
    "third_party_policy_number":"ACM-7782-99","prior_claims_count":0,
    "telematics":{"crash_alert_received":True,"delta_v_mph":9.5,
                  "impact_severity_score":3.2,"airbag_deployed":False,"consent_given":True},
}
DEMO2 = {
    "policy_number":"POC-POL-00456","loss_date_time":"2026-05-11T02:15:00Z",
    "loss_location":"Atlanta, GA","loss_cause":"SINGLE_VEHICLE",
    "loss_description":"Hit a deer late at night. No witnesses. Severe spinal injury.",
    "reporter_name":"Jordan Mehta","reporter_phone":"+1-404-555-0188",
    "estimated_loss_usd":9200,"vehicle_acv_usd":12000,
    "photo_count":1,"photo_quality_score":0.41,"drivable_indicator":False,
    "injury_reported":False,"liability_clear":True,
    "attorney_represented":True,"prior_claims_count":3,"iso_match":True,
    "policy_tenure_days":45,"seed_fraud":True,
    "telematics":{"crash_alert_received":False,"delta_v_mph":0.0,
                  "impact_severity_score":0.0,"airbag_deployed":False,"consent_given":False},
}
DEMO3 = {
    "policy_number":"POC-POL-00789","loss_date_time":"2026-05-12T16:00:00Z",
    "loss_location":"Austin, TX","loss_cause":"HEAD_ON_COLLISION",
    "loss_description":"Severe head-on collision. Both airbags deployed. Vehicle inoperable.",
    "reporter_name":"Priya Donnelly","reporter_phone":"+1-512-555-0199",
    "estimated_loss_usd":62000,"vehicle_acv_usd":58000,"vehicle_class":"EV",
    "photo_count":12,"photo_quality_score":0.91,"drivable_indicator":False,
    "injury_reported":True,"injury_severity":"SERIOUS","liability_clear":True,
    "rear_ended_by_other":True,"third_party_carrier":"Statewide Auto",
    "third_party_policy_number":"SWA-101-22","prior_claims_count":0,
    "telematics":{"crash_alert_received":True,"delta_v_mph":28.4,
                  "impact_severity_score":8.7,"airbag_deployed":True,"consent_given":True},
}

# ════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}FNOL Intelligence Platform — Smoke Test{RESET}")
print(f"  Target : {CYAN}{BASE}{RESET}")
print(f"  API key: {DIM}…{API_KEY[-4:]} ({len(API_KEY)} chars){RESET}")
print(f"  Mode   : {'--fast (LLM routes skipped)' if args.fast else 'full'}")
print("─" * 60)

# ── 01 META ────────────────────────────────────────────────────────────────
section("01 · Meta — health, config, redirect")

# Root redirects to /app (307 is correct)
sc, _ = req("GET", "/")
if sc in (200, 301, 302, 307, 308):
    ok("meta", "GET / → redirect to /app", f"HTTP {sc}")
else:
    fail("meta", "GET / → redirect", f"HTTP {sc}")

check("meta", "GET /api/v1/health", "GET", "/api/v1/health")

cfg = check("meta", "GET /api/v1/config", "GET", "/api/v1/config")
if cfg:
    policies = cfg.get("policies", [])
    stages   = cfg.get("pipeline_stages", [])
    ok("meta", f"Config: {len(policies)} policies, {len(stages)} pipeline stages")
    if len(policies) < 3: fail("meta","Config policies", f"only {len(policies)}")

# ── 02 L2 PIPELINE — Demo 1 (STP) ─────────────────────────────────────────
section("02 · L2 Pipeline — Demo 1 (Rear-end TX → STP)")
r1 = check("l2","POST /fnol/claims Demo 1","POST","/api/v1/fnol/claims",DEMO1,expected=201)
if r1:
    _claim_id  = r1.get("claim_id")
    status1    = r1.get("final_status","")
    stages1    = r1.get("pipeline",{}).get("stages",[])
    ok("l2", f"claim_id={_claim_id}, final_status={status1}")
    ok("l2", f"Stage count: {len(stages1)}") if len(stages1) >= 8 else fail("l2","stage count",f"{len(stages1)}")
    s4a = next((s for s in stages1 if s.get("stage_id")=="S4A"), {})
    out = s4a.get("outputs",{})
    band    = out.get("fraud_band") or out.get("fraud_risk_band","?")
    iso_m   = out.get("iso_adapter_mode","?")
    trig    = out.get("triggered_categories",[])
    ok("l2", f"S4A: fraud_band={band}, iso_mode={iso_m}, triggered={trig}")
    s6  = next((s for s in stages1 if s.get("stage_id")=="S6"), {})
    pay = s6.get("outputs",{}).get("payment_id","")
    ok("l2", f"S6 payment_id={pay}") if pay else warn("S6 payment_id not set (mock SOR is fine)")

# ── 03 L2 PIPELINE — Demo 2 (Fraud → SIU) ────────────────────────────────
section("03 · L2 Pipeline — Demo 2 (Fraud signal → SIU hold)")
r2 = check("l2","POST /fnol/claims Demo 2","POST","/api/v1/fnol/claims",DEMO2,expected=201)
if r2:
    _fraud_claim_id = r2.get("claim_id")
    status2 = r2.get("final_status","")
    stages2 = r2.get("pipeline",{}).get("stages",[])
    s4a2 = next((s for s in stages2 if s.get("stage_id")=="S4A"),{})
    band2 = s4a2.get("outputs",{}).get("fraud_band") or s4a2.get("outputs",{}).get("fraud_risk_band","?")
    iso_m2 = s4a2.get("outputs",{}).get("iso_adapter_mode","?")
    trig2  = s4a2.get("outputs",{}).get("triggered_categories",[])
    ok("l2", f"fraud claim={_fraud_claim_id}, status={status2}, band={band2}, iso={iso_m2}")
    ok("l2", f"Triggered signals: {trig2}")
    if band2 in ("HIGH","CRITICAL"):
        ok("l2", "Fraud signals fired correctly")
    else:
        fail("l2","Expected HIGH/CRITICAL fraud band",f"got {band2}")

# ── 04 L2 PIPELINE — Demo 3 (Total Loss EV) ──────────────────────────────
section("04 · L2 Pipeline — Demo 3 (Total Loss EV)")
r3 = check("l2","POST /fnol/claims Demo 3","POST","/api/v1/fnol/claims",DEMO3,expected=201)
if r3:
    _tl_claim_id = r3.get("claim_id")
    stages3 = r3.get("pipeline",{}).get("stages",[])
    s4b3 = next((s for s in stages3 if s.get("stage_id")=="S4B"),{})
    tl   = s4b3.get("outputs",{}).get("total_loss",False)
    a11  = next((s for s in stages3 if s.get("stage_id")=="A11"),{})
    ok("l2", f"tl claim={_tl_claim_id}, S4B.total_loss={tl}, A11 ran={bool(a11)}")

# ── 05 CLAIMS CRUD ────────────────────────────────────────────────────────
section("05 · Claims CRUD")
d = check("claims","GET /fnol/claims","GET","/api/v1/fnol/claims")
if d: ok("claims",f"Claim list: {d.get('count',0)} records")
if _claim_id:
    check("claims","GET /fnol/claims/{id}","GET",f"/api/v1/fnol/claims/{_claim_id}")
    check("claims","GET /fnol/claims/{id}/pipeline","GET",f"/api/v1/fnol/claims/{_claim_id}/pipeline")
check("claims","POST /fnol/policy/lookup","POST","/api/v1/fnol/policy/lookup",
      body={"policy_number":"POC-POL-00123"})

# ── 06 GOVERNANCE ─────────────────────────────────────────────────────────
section("06 · Governance — 12 routes")
gh = check("gov","GET /governance/health","GET","/api/v1/fnol/governance/health")
if gh:
    ok("gov",f"Decisions: {gh.get('decision_log',{}).get('total_entries',0)}, chain_valid={gh.get('decision_log',{}).get('chain_valid','?')}")
    ok("gov",f"Model cards: {gh.get('model_cards',{}).get('total',0)}, bias_tested={gh.get('model_cards',{}).get('bias_tested',0)}")
    flags = gh.get("bias_monitor",{}).get("parity_flags",[])
    ok("gov",f"Bias parity flags: {len(flags)}")

check("gov","GET /governance/decisions","GET","/api/v1/fnol/governance/decisions")
if _claim_id:
    check("gov","GET /governance/decisions/{claim_id}","GET",f"/api/v1/fnol/governance/decisions/{_claim_id}")

check("gov","POST /governance/decisions","POST","/api/v1/fnol/governance/decisions",
      body={"claim_id":_claim_id or "CLM-SMOKE","stage_id":"SMOKE","rule_id":"SMOKE:TEST",
            "decision":"SMOKE_PASS","confidence":1.0,"rationale":"Automated smoke test."})

check("gov","GET /governance/bias","GET","/api/v1/fnol/governance/bias")
check("gov","POST /governance/bias","POST","/api/v1/fnol/governance/bias",
      body={"claim_id":_claim_id or "CLM-SMOKE","gender_code":"F","preferred_language":"en",
            "garaging_zip_prefix":"770","stp_authorized":True,"fraud_score":0.12})

ev = check("gov","GET /governance/bias/evaluation","GET","/api/v1/fnol/governance/bias/evaluation")
if ev: ok("gov",f"Bias eval overall: {ev.get('overall_determination','?')}, tests: {len(ev.get('tests',[]))}")

mc = check("gov","GET /governance/model-cards","GET","/api/v1/fnol/governance/model-cards")
if mc: ok("gov",f"Model cards loaded: {len(mc.get('model_cards',[]))}")

check("gov","GET /governance/model-cards/S4A","GET","/api/v1/fnol/governance/model-cards/S4A")
check("gov","GET /governance/model-cards/INVALID → 404","GET",
      "/api/v1/fnol/governance/model-cards/INVALID",expected=404)

sa = check("gov","GET /governance/state-addenda","GET","/api/v1/fnol/governance/state-addenda")
if sa: ok("gov",f"State addenda: {sa.get('total',0)} jurisdictions")

tx = check("gov","GET /governance/state-addenda/TX","GET","/api/v1/fnol/governance/state-addenda/TX")
if tx: ok("gov",f"TX TLT={tx.get('tlt_pct')}, ACK={tx.get('prompt_payment_days')}d, AA={tx.get('adverse_action_days')}d")

co = check("gov","GET /governance/state-addenda/CO (DOI required)","GET","/api/v1/fnol/governance/state-addenda/CO")
if co: ok("gov",f"CO doi_filing_required={co.get('doi_filing_required')}, provisions={len(co.get('special_provisions',[]))}")

check("gov","GET /governance/state-addenda/CA","GET","/api/v1/fnol/governance/state-addenda/CA")
check("gov","GET /governance/state-addenda/ZZ → 404","GET",
      "/api/v1/fnol/governance/state-addenda/ZZ",expected=404)

aa = check("gov","POST /governance/adverse-action","POST","/api/v1/fnol/governance/adverse-action",
           body={"claim_id":_claim_id or "CLM-SMOKE","template_key":"STP_DENIAL",
                 "basis":"Smoke test verification.","state":"TX"})
if aa: ok("gov",f"FCRA notice: {len(aa.get('notice',''))} chars")

# ── 07 SIU ────────────────────────────────────────────────────────────────
section("07 · SIU (A12) — 8 routes")
sl = check("siu","GET /siu (list)","GET","/api/v1/fnol/siu")
if sl: ok("siu",f"SIU cases in store: {len(sl.get('cases',[]))}")

if _fraud_claim_id:
    so = check("siu","POST /siu/open (fraud claim)","POST","/api/v1/fnol/siu/open",
               body={"claim_id":_fraud_claim_id},expected=201)
    if so:
        _siu_id = so.get("case_id")
        ok("siu",f"case_id={_siu_id}, band={so.get('fraud_band')}, hold={so.get('payment_hold_flag')}")
        ok("siu",f"Investigator: {so.get('investigator',{}).get('name','?')} | Team: {so.get('siu_team','?')}")
else:
    warn("SIU open skipped — no fraud claim (Demo 2 must succeed first)")

if _siu_id:
    check("siu","GET /siu/{case_id}","GET",f"/api/v1/fnol/siu/{_siu_id}")
    check("siu","GET /siu/by-claim/{id}","GET",f"/api/v1/fnol/siu/by-claim/{_fraud_claim_id}")
    check("siu","POST /siu/evidence","POST","/api/v1/fnol/siu/evidence",
          body={"case_id":_siu_id,"evidence_type":"STATEMENT",
                "description":"Recorded statement summary — smoke test.","source":"SMOKE_TEST"})
    check("siu","POST /siu/notes","POST","/api/v1/fnol/siu/notes",
          body={"case_id":_siu_id,"notes":"Adjuster notes — smoke test verification."})
    if not args.fast:
        check("siu","POST /siu/referral (LLM)","POST","/api/v1/fnol/siu/referral",body={"case_id":_siu_id})
    check("siu","POST /siu/close (CLEARED)","POST","/api/v1/fnol/siu/close",
          body={"case_id":_siu_id,"disposition":"CLEARED","investigator_notes":"Smoke test clear."})

# ── 08 TOTAL LOSS ─────────────────────────────────────────────────────────
section("08 · Total Loss (A11) — 7 routes")
tl_list = check("tl","GET /total-loss (list)","GET","/api/v1/fnol/total-loss")
if tl_list: ok("tl",f"TL evaluations: {len(tl_list.get('evaluations',[]))}")

if _tl_claim_id:
    tlr = check("tl","POST /total-loss/evaluate","POST","/api/v1/fnol/total-loss/evaluate",
                body={"claim_id":_tl_claim_id})
    if tlr:
        _eval_id = tlr.get("evaluation_id")
        ok("tl",f"eval_id={_eval_id}, is_tl={tlr.get('is_total_loss')}, state={tlr.get('state')}")

if _eval_id:
    ev2 = check("tl","GET /total-loss/{eval_id}","GET",f"/api/v1/fnol/total-loss/{_eval_id}")
    if ev2: ok("tl",f"TLT={ev2.get('tlt_pct')}, observed={ev2.get('tlt_percentage_observed'):.3f}")
    check("tl","GET /total-loss/by-claim/{id}","GET",f"/api/v1/fnol/total-loss/by-claim/{_tl_claim_id}")
    check("tl","POST /total-loss/assign-salvage","POST","/api/v1/fnol/total-loss/assign-salvage",
          body={"evaluation_id":_eval_id,"vendor":"auto"})
    check("tl","POST /total-loss/owner-decision","POST","/api/v1/fnol/total-loss/owner-decision",
          body={"evaluation_id":_eval_id,"choice":"carrier_retains_salvage"})
    if not args.fast:
        check("tl","POST /total-loss/letter (LLM)","POST","/api/v1/fnol/total-loss/letter",
              body={"evaluation_id":_eval_id})

# ── 09 CO-PILOT ───────────────────────────────────────────────────────────
section("09 · Co-Pilot (A9) — 2 routes")
if _claim_id and not args.fast:
    cp = check("cop","POST /copilot","POST","/api/v1/fnol/copilot",
               body={"claim_id":_claim_id,"question":"What is the next best action for this claim?"})
    if cp: ok("cop",f"Co-pilot response: {len(cp.get('text',''))} chars")
    check("cop","GET /copilot/alerts/{id}","GET",f"/api/v1/fnol/copilot/alerts/{_claim_id}")
else:
    warn("Co-Pilot skipped (--fast or no claim)")

# ── 10 CONVERSATIONAL FNOL ───────────────────────────────────────────────
section("10 · Conversational FNOL (A10) — 3 routes")
if not args.fast:
    cs = check("conv","POST /conversation/start","POST","/api/v1/fnol/conversation/start",
               body={"channel":"WEB"})
    if cs:
        _session_id = cs.get("session_id")
        ok("conv",f"session_id={_session_id}")
    if _session_id:
        ct = check("conv","POST /conversation/turn","POST","/api/v1/fnol/conversation/turn",
                   body={"session_id":_session_id,"user_message":"I was rear-ended on I-10 in Houston."})
        if ct: ok("conv",f"Agent reply: {len(ct.get('assistant_message',''))} chars")
        check("conv","GET /conversation/{session_id}","GET",f"/api/v1/fnol/conversation/{_session_id}")
else:
    warn("Conversational FNOL skipped (--fast)")

# ── 11 ISO CLAIMSEARCH ───────────────────────────────────────────────────
section("11 · ISO ClaimSearch (Verisk) — 4 routes")
ih = check("iso","GET /iso/health","GET","/api/v1/fnol/iso/health")
if ih: ok("iso",f"ISO adapter mode: {ih.get('mode','?')}, cache entries: {ih.get('cache',{}).get('total_entries',0)}")

iq = check("iso","POST /iso/query","POST","/api/v1/fnol/iso/query",
           body={"claim_id":_claim_id or "CLM-SMOKE","claimant_first_name":"Aria",
                 "claimant_last_name":"Castillo","vin":"1HGCM82633A123456",
                 "policy_number":"POC-POL-00123","loss_date":"2026-05-10"})
if iq:
    ok("iso",f"ISO query: hits={iq.get('hit_count',0)}, iso_match={iq.get('iso_match')}, weight={iq.get('fraud_signal_weight',0):.3f}, txn={iq.get('transaction_id','?')[:20]}")

c2 = check("iso","GET /iso/cache","GET","/api/v1/fnol/iso/cache")
if c2: ok("iso",f"Cache: {c2.get('total_entries',0)} entries, {c2.get('cached_claims',0)} claims")

if _claim_id:
    cd = check("iso","DELETE /iso/cache/{claim_id}","DELETE",f"/api/v1/fnol/iso/cache/{_claim_id}")
    if cd: ok("iso",f"Cache invalidated: removed={cd.get('removed')}")

# ── 12 PAYMENTS ──────────────────────────────────────────────────────────
# Skipped: no payments agent module exists yet. The smoke-test sections that
# exercised `/api/v1/fnol/payments/*` were against a roadmap feature that
# has not been built. Re-enable when fnol_payments_agent.py + router land.
section("12 · Payments — SKIPPED (agent module not yet implemented)")
warn("payments routes deferred — agent module not built; see roadmap.")

# ── 13 LANGGRAPH L3 ──────────────────────────────────────────────────────
section("13 · LangGraph L3 Orchestration — 5 routes")
lh = check("lg","GET /v3/health","GET","/api/v1/fnol/v3/health")
if lh:
    _l3_enabled = lh.get("l3_enabled", False)
    ok("lg",f"L3 enabled={_l3_enabled}, nodes={len(lh.get('graph_nodes',[]))}")
    if not _l3_enabled:
        warn(f"LangGraph not installed. Install: pip install langgraph langgraph-checkpoint-sqlite")

ll = check("lg","GET /v3/claims (list threads)","GET","/api/v1/fnol/v3/claims")
if ll: ok("lg",f"Threads: {len(ll.get('threads',[]))}")

if _l3_enabled:
    lg_r = check("lg","POST /v3/claims (Demo 1 via LangGraph)","POST",
                 "/api/v1/fnol/v3/claims",DEMO1,expected=201)
    if lg_r:
        _thread_id = lg_r.get("thread_id")
        lg_status  = lg_r.get("status") or lg_r.get("final_status","")
        orch       = lg_r.get("orchestrator","?")
        ok("lg",f"thread_id={_thread_id}, status={lg_status}, orchestrator={orch}")
    if _thread_id:
        ts = check("lg","GET /v3/claims/{thread_id}","GET",f"/api/v1/fnol/v3/claims/{_thread_id}")
        if ts: ok("lg",f"Thread state: final={ts.get('final_status')}, fraud_band={ts.get('fraud_band')}")
else:
    # LangGraph not installed — expect 503 on submit
    sc_l3, _ = req("POST","/api/v1/fnol/v3/claims",DEMO1)
    if sc_l3 == 503:
        ok("lg","POST /v3/claims → 503 (LangGraph not installed, expected)")
    else:
        warn(f"L3 not installed but got {sc_l3} instead of 503")

# ── 14 ERROR HANDLING ─────────────────────────────────────────────────────
section("14 · Error handling — 401, 404")
no_auth = httpx.Client(base_url=BASE, timeout=10.0)
sc_401 = no_auth.get("/api/v1/fnol/claims").status_code
ok("err","GET /fnol/claims without key → 401") if sc_401==401 else fail("err","401 auth guard",f"got {sc_401}")

check("err","GET /siu/BOGUS → 404","GET","/api/v1/fnol/siu/SIU-DOESNOTEXIST",expected=404)
check("err","POST /siu/open MISSING claim → 404","POST","/api/v1/fnol/siu/open",
      body={"claim_id":"CLM-DOESNOTEXIST"},expected=404)
check("err","GET /governance/model-cards/BOGUS → 404","GET",
      "/api/v1/fnol/governance/model-cards/BOGUS",expected=404)
check("err","GET /governance/state-addenda/ZZ → 404","GET",
      "/api/v1/fnol/governance/state-addenda/ZZ",expected=404)

# ════════════════════════════════════════════════════════════════════════════
# SUMMARY
def _summary():
    passed = [r for r in results if r[2]]
    failed = [r for r in results if not r[2]]
    total  = len(results)
    print(f"\n{'─'*60}")
    print(f"{BOLD}SMOKE TEST RESULT{RESET}")
    print(f"  Checks : {total}  {GREEN}Passed: {len(passed)}{RESET}  {RED}Failed: {len(failed)}{RESET}")
    if failed:
        print(f"\n{RED}{BOLD}FAILURES:{RESET}")
        for grp,name,_,detail in failed:
            print(f"  [{grp}] {name}")
            if detail: print(f"         → {detail}")
    rate = len(passed)/total*100 if total else 0
    colour = GREEN if rate==100 else (YELLOW if rate>=80 else RED)
    print(f"\n{colour}{BOLD}Pass rate: {rate:.0f}%  ({len(passed)}/{total}){RESET}\n")

_summary()
sys.exit(0 if all(r[2] for r in results) else 1)
