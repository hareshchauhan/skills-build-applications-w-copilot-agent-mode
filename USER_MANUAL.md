# FNOL Intelligence Platform — User Manual

**Version 4.1.0 · A11 Total-Loss & Salvage**

This manual walks an operator through using the platform end-to-end: starting the server, working the UI, demoing the A11 Total-Loss & Salvage flow, and inspecting Decision Records. Pair this with `README.md` (architecture) and `DUCK_CREEK_ALIGNMENT.md` (positioning).

---

## 1 · Starting the platform

```bash
unzip fnol_app_v4.1.zip
cd fnol_app_v4.1
python fnol_launcher.py --install
```

The launcher installs `fastapi`, `uvicorn`, and `pydantic` if missing, then opens the SPA at `http://localhost:8000/app`.

If `--install` is not desired, install manually then run without it:

```bash
pip install -r requirements.txt
python fnol_launcher.py
```

Health check:

```bash
curl http://localhost:8000/api/v1/health | python -m json.tool
```

Expect `status: ok`, `version: 4.1.0-A11-total-loss`, and a populated `total_loss_agent` block.

---

## 2 · UI tour (7 tabs)

| # | Tab | What it shows |
|---|---|---|
| 01 | **Submit FNOL** | Manual claim submission form with 4 demo presets (rear-end, hail, **total loss**, weekend hit-and-run) |
| 02 | **Live Pipeline** | Visual pipeline timeline for any submitted claim — stage status, durations, decisions |
| 03 | **Claims** | Sortable claim list with summary fields (track, fraud band, settlement, **total-loss flag**) |
| 04 | **Adjuster Co-Pilot** | A9 Q&A against any specific claim plus proactive alert feed |
| 05 | **Conversational FNOL** | A10 — chat with the AI intake agent as if you were a customer |
| 06 | **Total Loss & Salvage** | A11 — evaluations list, ACV breakdown, settlement options, salvage assignment, customer letter |
| 07 | **System** | Health, configuration, thresholds, POC policies |

---

## 3 · Demo flow: triggering A11

The cleanest way to see A11 in action is via the **"Total loss"** preset on the Submit FNOL tab. It populates a Tesla Model 3 claim with damage estimate ≥ ACV, single-vehicle collision, undrivable, airbags deployed — every signal that S4B uses to flag total loss.

Sequence:

1. **Tab 01** — Click **Preset 3 · Total loss**. Confirm the form populates. Click **Submit FNOL**.
2. Toast banner appears: "Claim CLM-... submitted · STP/HITL routing decided." Click **View pipeline** in the banner.
3. **Tab 02** — Watch all 10 stages render (S0 → S4B, then **A11**, then S5 → S7). The A11 row shows `TOTAL_LOSS` and two decisions: TL determination + salvage assignment.
4. **Tab 06** — The new evaluation appears at the top of the evaluations list. Click it.

You'll see:

* **State + TLT** (e.g. `TX · 75%`) and observed percentage (typically `78%` to `92%` for total-loss-shaped claims)
* **Final ACV** with the calculation breakdown (base ACV ± mileage / condition / options adjustments)
* **Settlement Option A — Carrier takes vehicle** with full subtotal → tax → title fee math
* **Settlement Option B — Owner keeps vehicle** showing the salvage credit deduction
* **Salvage assignment** with vendor, yard, lot ID, pickup ETA, expected sale date, expected net return, and confidence
* **Customer notification letter** (click "Generate letter draft" if it isn't already rendered)

---

## 4 · A11 operator actions

From the evaluation detail panel:

| Action | Effect |
|---|---|
| **Record owner decision → carrier_retains_salvage** | Marks the insured's choice on the evaluation record; audit-trail entry created |
| **Record owner decision → owner_retains_salvage** | Same, with owner-retention path |
| **Re-assign → Copart** | Re-runs the salvage assignment against the Copart adapter; updates lot ID, yard, expected net |
| **Re-assign → IAA** | Same, against IAA |
| **Auto (best net)** | Runs shadow quotes against every configured vendor and picks the highest expected net return |
| **Generate letter draft** | Calls the LLM (if configured) or the deterministic template fallback to produce a customer notification letter |

The salvage vendor card at the bottom of the tab shows whether real Copart/IAA partner-API credentials are configured. Without credentials, the adapters run in **shell mode** — vendor-shaped responses with realistic fees and yard networks for demos.

---

## 5 · A11 API reference (manual testing)

API key (POC default): `fnol-api-key-2026`

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/fnol/total-loss/evaluate` | Re-run A11 against an existing claim trace |
| `POST` | `/api/v1/fnol/total-loss/assign-salvage` | Assign or re-assign a salvage vendor |
| `POST` | `/api/v1/fnol/total-loss/owner-decision` | Record the insured's retention choice |
| `POST` | `/api/v1/fnol/total-loss/letter` | Generate the customer notification letter |
| `GET`  | `/api/v1/fnol/total-loss/{evaluation_id}` | Fetch a single evaluation |
| `GET`  | `/api/v1/fnol/total-loss/by-claim/{claim_id}` | Find the evaluation for a claim |
| `GET`  | `/api/v1/fnol/total-loss?limit=50` | List recent evaluations |

Example — round-trip after a total-loss claim has been submitted:

```bash
# 1. Find the most recent A11 evaluation
EVAL=$(curl -s http://localhost:8000/api/v1/fnol/total-loss \
  -H "X-API-Key: fnol-api-key-2026" \
  | python -c "import sys,json; print(json.load(sys.stdin)['evaluations'][0]['evaluation_id'])")
echo "Evaluation: $EVAL"

# 2. Re-assign to IAA
curl -s -X POST http://localhost:8000/api/v1/fnol/total-loss/assign-salvage \
  -H "X-API-Key: fnol-api-key-2026" -H "Content-Type: application/json" \
  -d "{\"evaluation_id\":\"$EVAL\",\"vendor\":\"iaa\"}" | python -m json.tool | head -40

# 3. Record owner decision
curl -s -X POST http://localhost:8000/api/v1/fnol/total-loss/owner-decision \
  -H "X-API-Key: fnol-api-key-2026" -H "Content-Type: application/json" \
  -d "{\"evaluation_id\":\"$EVAL\",\"choice\":\"owner_retains_salvage\"}" | python -m json.tool

# 4. Generate letter
curl -s -X POST http://localhost:8000/api/v1/fnol/total-loss/letter \
  -H "X-API-Key: fnol-api-key-2026" -H "Content-Type: application/json" \
  -d "{\"evaluation_id\":\"$EVAL\"}" | python -c "import sys,json; print(json.load(sys.stdin)['letter'])"
```

---

## 6 · Inspecting Decision Records

A11 emits two Decision Records per total-loss claim. To see them:

```bash
# Pipeline trace for a claim
curl http://localhost:8000/api/v1/fnol/claims/CLM-XXXXX/pipeline \
  -H "X-API-Key: fnol-api-key-2026" \
  | python -c "
import sys, json
data = json.load(sys.stdin)
for s in data['stages']:
    if s['stage_id'] == 'A11':
        for d in s['decisions']:
            print(f\"{d['stage_name']:30s}  {d['decision']:25s}  conf={d['confidence']:.2f}  hitl={d['hitl_required']}\")
            print(f\"  rationale: {d['rationale']}\")
            print()
"
```

Expected output (TL claim):

```
Total-Loss Determination       TOTAL_LOSS                conf=0.88  hitl=False
  rationale: State TX TLT=75%; observed (repair $28,000 + prior $0) / ACV $33,100 = 84.6% → TOTAL_LOSS. ...

Salvage Vendor Assignment      ASSIGN_MOCK               conf=0.90  hitl=False
  rationale: selected from 3 quotes — severity=SEVERE, area=FRONT, brand=SALVAGE, drivable=False, ...
```

`hitl_required=True` fires when the observed TLT percentage exceeds the state TLT by ≥10pp — borderline cases that warrant adjuster review even though the math says total loss.

---

## 7 · Switching LLM provider

A11 letter drafting uses whichever provider is selected by `FNOL_LLM_PROVIDER`. Defaults to `auto` (which falls back to mock when no real provider key is present).

```bash
FNOL_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... python fnol_launcher.py
FNOL_LLM_PROVIDER=azure_openai AZURE_OPENAI_API_KEY=... AZURE_OPENAI_ENDPOINT=https://... python fnol_launcher.py
FNOL_LLM_PROVIDER=bedrock AWS_REGION=us-east-1 ... python fnol_launcher.py
```

If the LLM returns template-shaped output (mock provider, partial fallback) the agent automatically substitutes a deterministic letter so demos never break.

---

## 8 · Connecting real salvage vendors

A11 ships with Copart and IAA adapters in **shell mode** — they simulate vendor responses without making network calls. To enable live integration:

```bash
SALVAGE_VENDOR=copart \
COPART_API_BASE_URL=https://api.copart.com/partner \
COPART_API_KEY=... \
python fnol_launcher.py
```

The shell adapters in `fnol_salvage_adapter.py` clearly mark the production path (`requests.post(...)` calls) so wiring real EDI 906 / REST endpoints is a contained change. The data contracts (`SalvageAssignmentRequest`, `SalvageAssignmentResponse`) are vendor-agnostic — A11 stays unchanged when adapters are swapped.

---

## 9 · State TLT and tax table

A11 ships with all 51 US jurisdictions (50 states + DC):

* **Total Loss Threshold** per state — industry-typical defaults from 60% (OK) to 80% (FL/MO/OR) with 75% as the most common value
* **Sales tax + title fees** per state — sourced from publicly available DMV/DOR schedules

Both tables are POC defaults. In production they should be loaded from a carrier-managed reference table with effective-date versioning. Override either by editing `STATE_TLT` / `STATE_TAX` in `fnol_total_loss_agent.py`.

---

## 10 · Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A11 tab shows "No evaluations yet" | No claim has triggered total loss yet | Use Preset 3 on Submit FNOL, or any claim where `estimated_loss_usd ≥ 0.75 × vehicle_acv_usd` |
| Pipeline trace shows no A11 row | S4B did not flag total loss | Check the S4B `total_loss` field in the trace; A11 is conditional |
| Letter says "Mock Coverage Analysis" or similar | LLM returned template/mock output; the agent's fallback caught it | Letter is replaced with the deterministic template — this is by design |
| `salvage_vendor: MOCK` always wins auto selection | No real Copart/IAA credentials configured | Set `COPART_API_BASE_URL` + `COPART_API_KEY` (and/or IAA equivalents) |
| Health endpoint shows `total_loss_agent: { ... evaluations_in_store: 0 }` after restart | A11 uses in-memory store (POC) | Submit a fresh total-loss claim; production replaces the store with Redis/DynamoDB |

---

## 11 · Stopping & resetting

```bash
# Stop server: Ctrl-C in the launcher terminal
# Reset all in-memory state: restart the server
```

POC data is in-memory. No filesystem state to clean up.
