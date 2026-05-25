# FNOL Intelligence Platform — User Manual
**v3.5.1 · Engine: BLUEPRINT_TAG=L100-V2 · May 2026**
**Accenture P&C Auto Claims · Duck Creek–Native**

---

## Quick-Start (30 seconds)

```bash
# 1. Navigate to your FNOL directory
cd C:\DEV1\FNOL\

# 2. Start the platform
python fnol_launcher.py --install

# 3. Open the app
# Browser opens automatically at: http://localhost:8000/app
# Replace fnol_app.html with fnol_intake_app.html for the new UI
```

**Default API Key:** `fnol-api-key-2026`

The new `fnol_intake_app.html` replaces `fnol_app.html` in the same directory — no other changes needed.

---

## Application Overview

The FNOL Intelligence Platform is an 8-agent AI pipeline for P&C Auto Claims intake:

| Stage | Agent | Purpose |
|---|---|---|
| S0 | Pre-FNOL / Telematics | Crash detection, IoT signal capture |
| S1 | FNOL Intake | Claim capture & validation |
| S2 | Coverage & Liability | Coverage verification, reservation |
| S3 | Triage & Assignment | Complexity scoring, adjuster routing |
| S4A | Fraud Detection | 40 fraud signals across 8 categories |
| S4B | Damage Assessment | AI damage estimate, total-loss flag |
| A11 | Total-Loss & Salvage | TLT determination, salvage, settlement |
| S5 | BI Evaluation | Bodily injury & liability determination |
| S6 | Settlement | Payment authorization |
| S7 | Subrogation | Recovery evaluation |
| A9 | Adjuster Co-Pilot | Post-pipeline Q&A + proactive alerts |
| A10 | Conversational FNOL | Customer-facing L3 intake agent |

---

## Tab-by-Tab Guide

---

### 01 · Submit FNOL

**Purpose:** The primary intake form. Submits a new First Notice of Loss and runs the full 8-agent pipeline.

**Steps:**
1. **Load a Demo** (optional) — Click one of the three demo buttons to pre-fill the form:
   - 🚗 **Rear-end (TX)** — Standard collision, POC-POL-00123, injury reported
   - ⚠️ **Fraud Signal (GA)** — Multiple fraud indicators, POC-POL-00456
   - 💥 **Total Loss (TX)** — Head-on collision, POC-POL-00789, triggers A11

2. **Fill Policy & Loss** — Select a policy from the dropdown (or enter a custom one). Fill in loss date/time, cause, location, and a description.

3. **Fill Reporter Info** — Name, phone, tenure (days since policy inception), prior claims count.

4. **Fill Vehicle & Damage** — Estimated loss, vehicle ACV (Actual Cash Value), vehicle class, photo count, and quality score. Check "Vehicle Drivable" if applicable.

5. **Fill Injury & Liability** — Check "Injury Reported" and select severity. Flag fatality, attorney representation, liability clarity, and third-party carrier information.

6. **Fill Telematics (S0)** — Pre-FNOL IoT signals. Check "Crash Alert Received," enter delta-V (mph), impact severity score (0–10), airbag deployment, and consent.

7. **Click "Run 8-Agent Pipeline"** — The pipeline runs all 8 agents and returns results in seconds.

**Result Card:**
- **Claim ID** — Unique identifier (CLM-XXXXX)
- **Final Status** — STP_COMPLETE, REFERRED_TO_ADJUSTER, FRAUD_REFERRED, TOTAL_LOSS_EVALUATED, etc.
- **Pipeline Duration** — End-to-end latency in ms
- **LLM Provider** — Which model was used (MOCK, ANTHROPIC, AZURE_OPENAI, etc.)
- **Pipeline Track** — Visual stage-by-stage status bar
- **Inspect Pipeline** → jumps to Tab 02
- **Open Co-Pilot** → jumps to Tab 04 with this claim loaded

**POC Test Policies:**
| Policy | State | Notes |
|---|---|---|
| POC-POL-00123 | TX | 2020 Honda Accord · Standard |
| POC-POL-00456 | GA | 2022 Toyota Camry · Liability + collision |
| POC-POL-00789 | TX | 2023 Tesla Model 3 · High ACV EV · Best for A11 |
| POC-POL-00999 | — | Expired · Coverage-denial demo |

---

### 02 · Pipeline Trace

**Purpose:** Deep-dive inspection of the 8-agent pipeline for any submitted claim.

**Steps:**
1. Select a claim from the dropdown, or click "Inspect Pipeline" from a submitted claim.
2. The header shows claim ID, final status, total duration, and LLM provider.
3. The horizontal **Stage Track** shows each pipeline stage color-coded:
   - 🟢 Green = passed
   - 🟡 Amber = warning / conditional
   - 🔴 Red = error / fraud flag
   - ⚪ Gray = skipped (A11 skips unless S4B flags total_loss=True)
4. Click any **stage card** to expand its detail view:
   - **Outputs** — All key-value outputs from that stage (coverage_verified, fraud_score, damage_estimate, etc.)
   - **Decision Records** — Structured decision with confidence score, rationale, and HITL flag

**HITL Flag:** `hitl_required=True` fires when human review is mandatory (e.g., borderline TLT ≥10pp over threshold, fraud score ≥0.80).

---

### 03 · Claims Register

**Purpose:** Searchable list of all submitted claims with quick-access to details and actions.

**Steps:**
1. Claims auto-load on page open. Click **↻ Refresh** to sync.
2. Use the **search box** to filter by claim ID, status, cause, or any text field.
3. Click any **row** to expand a claim detail drawer showing:
   - All intake fields (policy, loss cause, reporter, vehicle info, injury, liability)
   - Mini pipeline track
   - Quick-action buttons: **Pipeline**, **Co-Pilot**

---

### 04 · Adjuster Co-Pilot (A9)

**Purpose:** AI assistant for adjusters working a specific claim. Ask natural-language questions and receive structured answers with suggested next actions.

**Steps:**
1. Select a claim from the left sidebar.
2. Proactive alerts auto-load (highlighted in amber) — these are AI-generated flags the adjuster should review immediately.
3. Type questions in the input box and press **Ctrl+Enter** (or click Send):
   - *"What's the fraud score and why?"*
   - *"Is there subrogation opportunity here?"*
   - *"Summarize the liability determination."*
   - *"What are the next recommended actions?"*
4. Suggested action buttons appear below Co-Pilot responses (display only; for demo).

**Context:** The Co-Pilot has full access to the claim record and all pipeline stage outputs.

---

### 05 · Conversational FNOL (A10 · L3)

**Purpose:** Customer-facing conversational intake. The agent collects FNOL information through natural dialogue, then runs the full pipeline automatically when intake is complete.

**Steps:**
1. Tab opens with the agent greeting automatically.
2. Respond to the agent's questions naturally — describe your accident, provide your policy number, describe the damage, etc.
3. Watch the **Captured Fields** panel (right sidebar) fill in as the agent extracts information.
4. When the agent has enough information, it completes intake and runs the pipeline automatically.
5. A **system message** confirms the claim ID and status when done.
6. Click **↺ New Session** to start a fresh intake conversation.

**Session Info panel** shows session ID, active status, and message count.

**Try saying:**
- *"I was in an accident this morning on I-10 in Houston."*
- *"My policy number is POC-POL-00123."*
- *"The other driver rear-ended me. My car is drivable but has rear bumper damage."*

---

### 06 · Total Loss & Salvage (A11)

**Purpose:** State-specific total-loss determination, ACV refinement, salvage vendor assignment, owner settlement options, and customer notification letter generation.

**Workflow:**
1. **Load Evaluations** — Tab auto-loads existing A11 evaluations. Click **↻ Refresh** to sync.

2. **Run A11 Evaluation** — Click "Run A11 Evaluation" to evaluate the most recent claim. For best results, submit the **Total Loss demo** from Tab 01 first.

3. **Click any evaluation row** to open its detail view:
   - **Loss-to-ACV Ratio bar** — Visual comparison of observed % vs. state TLT threshold (red vertical marker)
   - **ACV metrics** — Refined ACV, repair estimate, prior damage, branded title flag
   - **Salvage assignment** — Vendor, bid amount, net recovery, yard location

4. **Right sidebar — Actions:**
   - **Salvage Vendor** — Select AUTO (best quote), COPART, IAA, or MOCK
   - **Owner Decision** — Record whether Carrier or Owner retains salvage
   - **Generate Owner Letter** — AI-drafted customer notification letter per state regulations

5. **Letter Preview** — Appears below actions. Click **Copy** to copy to clipboard.

**TLT Thresholds:** State-specific Total Loss Thresholds are pre-configured for all 51 jurisdictions. HITL flag fires when observed TLT exceeds state threshold by ≥10pp.

---

### 07 · Governance (AI Act)

**Purpose:** Reference panel for the governance and AI compliance layer. Aligned to NAIC Model Bulletin on AI, Colorado Reg 10-1-1, NYDFS Circular Letter No. 7, and FCRA §615.

**Governance Cards:**
- **Model Cards** — 10 registered model cards documenting training data, bias assessments
- **Decision Log** — Immutable SQLite audit trail with SHA-256 hash chain
- **Regulatory Frameworks** — All 51 state addenda implemented
- **Bias Monitor** — 4 demographic proxy attributes tracked (monitoring-only, never input features)
- **FCRA Adverse Action** — Auto-generated notices per FCRA §615
- **HITL Escalation** — Automatic escalation rules configured
- **Explainability (XAI)** — Structured rationale + confidence on every decision
- **PII Handling** — TTL-bounded trace store, masked logs

**Regulatory Coverage Matrix** lists all frameworks with implementation status.

**Architecture notes** explain the governance/ package structure (8 modules, 16 API endpoints).

---

### 08 · System

**Purpose:** Live health status, configuration, pipeline stage registry, and POC policy reference.

**Shows:**
- LLM Provider, SOR Backend, Pipeline Version, Policies Seeded
- Full configuration table (API host, rate limits, thresholds)
- Pipeline stages table with IDs, names, and types
- POC test policies with recommended use cases

Click **↻ Refresh** to re-query live system status.

---

## API Key Setup

The platform requires `X-API-Key: fnol-api-key-2026` (default for local POC).

1. Click **🔑 API Key** in the top-right corner.
2. Enter your API key (default: `fnol-api-key-2026`).
3. Click **Save & Retry**.

The key is persisted in `localStorage` and sent with every request.

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `FNOL_API_KEY` | `fnol-api-key-2026` | API authentication key |
| `FNOL_LLM_PROVIDER` | `auto` | `anthropic` · `azure_openai` · `bedrock` · `mock` |
| `FNOL_SOR_TYPE` | `mock` | `duck_creek` · `guidewire` · `mock` |
| `SALVAGE_VENDOR` | `auto` | `copart` · `iaa` · `mock` · `auto` |
| `FNOL_HOST` | `0.0.0.0` | Server bind address |
| `FNOL_PORT` | `8000` | Server port |

**Switching LLM providers:**
```bash
# Anthropic
FNOL_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... python fnol_launcher.py

# Azure OpenAI
FNOL_LLM_PROVIDER=azure_openai AZURE_OPENAI_API_KEY=... AZURE_OPENAI_ENDPOINT=https://... python fnol_launcher.py

# AWS Bedrock
FNOL_LLM_PROVIDER=bedrock AWS_REGION=us-east-1 python fnol_launcher.py

# Google Gemini
FNOL_LLM_PROVIDER=gemini GOOGLE_API_KEY=... python fnol_launcher.py
```

---

## Common Issues

| Problem | Solution |
|---|---|
| "API offline" banner | Run `python fnol_launcher.py` in the FNOL directory |
| 401 API key error | Click 🔑 and enter `fnol-api-key-2026` |
| Coverage denied | Use a valid POC policy number (see Tab 08) |
| A11 not triggering | Use the Total Loss demo (POC-POL-00789, est. $62k vs ACV $58k) |
| LLM returns template output | Normal in mock mode — deterministic fallback is working correctly |
| Conversational agent not starting | Restart server; check `FNOL_LLM_PROVIDER` is set |

---

## Architecture Notes

- **Backend:** FastAPI at `localhost:8000`, 46 API routes
- **LLM Abstraction:** Multi-provider adapter (Anthropic, Azure OpenAI, Bedrock, NVIDIA NIM, Gemini/Vertex)
- **PDF Generation:** reportlab (hash-anchored audit footers)
- **Decision Log:** SQLite at `governance/decision_log_data/decisions.db`
- **CORS:** `allow_origins=["*"]` + `allow_credentials=False` (required for wildcard origins)
- **Rate Limiting:** Sliding-window per API key on LLM-backed endpoints
- **Idempotency:** `Idempotency-Key` header prevents duplicate pipeline runs

---

## Regulatory Alignment

| Framework | Scope | Implementation |
|---|---|---|
| NAIC Model Bulletin on AI | All states · AI use in insurance | ✅ Full |
| Colorado Reg 10-1-1 | Algorithmic bias · Auto claims | ✅ Full |
| NYDFS Circular Letter No. 7 | AI model governance · NY | ✅ Full |
| FCRA §615 | Adverse action notices | ✅ Full |
| 51 State Addenda | State TLT thresholds | ✅ All 51 |

---

*FNOL Intelligence Platform · Internal Accenture IP · © Accenture 2026*
