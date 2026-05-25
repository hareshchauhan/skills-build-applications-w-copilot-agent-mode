# FNOL Intelligence Platform v5 — User Manual

**Accenture Insurance Claims Intelligence Practice**  
*Auto Claims · Duck Creek-Native · AI-Powered*  
Version 5.0 · May 2026

---

## Table of Contents

1. [Overview](#overview)
2. [Getting Started — Server Setup](#getting-started)
3. [API Key Configuration](#api-key)
4. [Tab 01 — Submit FNOL](#tab01)
5. [Tab 02 — Live Pipeline Trace](#tab02)
6. [Tab 03 — Claims Dashboard](#tab03)
7. [Tab 04 — Adjuster Co-Pilot (A9)](#tab04)
8. [Tab 05 — Conversational FNOL (A10)](#tab05)
9. [Tab 06 — Total Loss & Salvage (A11)](#tab06)
10. [Tab 07 — SIU Case Builder (A12)](#tab07)
11. [Tab 08 — System Health](#tab08)
12. [POC Test Policies](#policies)
13. [Demo Scenarios](#demos)
14. [LLM Provider Configuration](#llm)
15. [Regulatory Compliance](#compliance)
16. [Troubleshooting](#troubleshooting)

---

## 1. Overview {#overview}

The **FNOL Intelligence Platform** is an AI-native auto claims workbench that orchestrates **9 specialized AI agents** from First Notice of Loss through settlement decision. It is positioned as internal Accenture IP and delivery accelerator for P&C auto claims processing.

### Agent Architecture

| Agent | Stage | Role |
|---|---|---|
| FNOL Intake | S0–S1 | Pre-FNOL crash detection, capture & validation |
| Coverage & Liability | S2 | Coverage verification & reservation |
| Triage & Assignment | S3 | Complexity scoring, adjuster routing |
| Fraud Detection | S4A | 40 fraud signals across 8 categories (parallel) |
| Damage Estimation | S4B | AI damage assessment + total-loss flag (parallel) |
| **Total-Loss & Salvage** | **A11** | State TLT, ACV, salvage assignment, settlement options |
| Coverage & Liability | S5 | BI evaluation & liability determination |
| Settlement | S6 | Settlement & payment authorization |
| Subrogation | S7 | Subrogation & recovery evaluation |
| **Adjuster Co-Pilot** | **A9** | Post-pipeline adjuster Q&A + proactive alerts |
| **Conversational FNOL** | **A10** | Customer-facing L3 conversational intake |
| **SIU Case Builder** | **A12** | Suspect claim packaging, evidence dossier, referral |

### Key Performance Targets

- **85%** FNOL → STP (Straight-Through Processing) automation rate
- **≤ 2 minutes** intake to coverage decision
- **≥ 80%** subrogation opportunities identified at FNOL
- **Zero hallucination** policy: all financial figures are deterministic; LLM is used only for letter drafting and adjuster Q&A

---

## 2. Getting Started — Server Setup {#getting-started}

### Prerequisites

```
Python 3.10+
pip install -r requirements.txt
```

### Start the Platform

```bash
# Windows
cd C:\DEV1\FNOL
python fnol_launcher.py

# macOS / Linux
cd /path/to/fnol
python fnol_launcher.py
```

The server starts at **http://localhost:8000**. The browser opens automatically to `/app`.

### Environment Variables (optional)

| Variable | Default | Description |
|---|---|---|
| `FNOL_API_KEY` | `fnol-api-key-2026` | API authentication key |
| `FNOL_PORT` | `8000` | Server port |
| `FNOL_LLM_PROVIDER` | `auto` | LLM: `anthropic` · `azure_openai` · `bedrock` · `mock` |
| `ANTHROPIC_API_KEY` | — | Required if `FNOL_LLM_PROVIDER=anthropic` |
| `SALVAGE_VENDOR` | `auto` | Salvage vendor: `copart` · `iaa` · `auto` |

### Install dependencies and launch

```bash
python fnol_launcher.py --install
```

---

## 3. API Key Configuration {#api-key}

On first use, the platform will prompt for the API key. The default for POC is:

```
fnol-api-key-2026
```

To set or change it:
1. Click the **API Key** button in the top-right header strip
2. Paste the key (must match `FNOL_API_KEY` on the server)
3. The dot turns **green** when accepted

The key is stored in browser `localStorage` and sent as the `X-API-Key` header on every API call.

---

## 4. Tab 01 — Submit FNOL {#tab01}

The primary intake form. Collects all fields needed to run the full 9-agent pipeline.

### Hero KPIs

The banner at the top shows platform performance targets. These are POC design goals, not live metrics.

### Demo Presets

Use the three demo buttons to quickly populate the form with realistic scenarios:

| Button | Scenario | Pipeline Outcome |
|---|---|---|
| **Demo · Rear-end (TX)** | Standard rear-end collision, TX jurisdiction, minor injury, third-party | STP_AUTHORIZED — fast-track |
| **Demo · Fraud Signals** | Late-tenure GA policy, low-quality photos, ISO match, attorney rep | ADJUSTER_REVIEW — fraud flag |
| **Demo · Total Loss (EV)** | Severe head-on collision, Tesla Model 3, high Delta-V, airbags deployed | ADJUSTER_REVIEW — triggers A11 |

### Form Sections

**Policy & Loss**
- *Policy Number*: Select from dropdown or choose "custom" to enter manually. POC policies are pre-seeded (see [POC Test Policies](#policies)).
- *Date & Time of Loss*: Defaults to current time. Adjust for historical claims.
- *Loss Cause*: Select the primary cause code (maps to Duck Creek loss codes).
- *Loss Location*: Free-text. Drives state jurisdiction lookup.
- *Loss Description*: Narrative fed to fraud and damage agents.

**Reporter**
- *Reporter Name / Phone*: Intake contact information.
- *Policy Tenure (days)*: Days since policy inception. Short tenure (< 90 days) is a fraud signal.

**Damage & Vehicle**
- *Estimated Loss (USD)*: Adjuster's initial estimate. Compared against ACV for total-loss threshold.
- *Vehicle ACV (USD)*: Actual Cash Value used in TLT calculation.
- *Vehicle Class*: Standard / Luxury / EV / Heavy Truck — affects damage agent scoring.
- *Photo Count / Photo Quality*: Low count or quality triggers fraud signals.
- *Driveable indicator*: Non-driveable vehicles trigger salvage logistics.
- *NHTSA Recall / ISO ClaimSearch*: Known fraud and liability signals.

**Injury & Liability**
- *Injury Reported / Severity*: Drives BI (Bodily Injury) evaluation in S5.
- *Fatality*: Activates critical claims handling workflow.
- *Liability Clear / Rear-ended by / of other*: Liability determination signals.
- *Attorney Represented*: Litigation flag — affects settlement and SIU scoring.
- *Third-Party Carrier/Policy*: Enables subrogation identification.

**Telematics — Pre-FNOL Crash Detection**
- *Crash Alert*: Whether telematics generated an alert.
- *Delta-V (mph)*: Velocity change at impact. High values (> 15 mph) escalate severity.
- *Impact Severity Score (0–10)*: Combined IoT signal.
- *Airbag Deployed*: Confirms high-severity event.
- *Consent Given*: Whether telematics data sharing is consented (required for FCRA compliance).
- *Seed Fraud Signal*: POC demo toggle — artificially elevates fraud score for demonstration.

### Submitting

Click **⟶ Run Full 9-Agent Pipeline**. The button shows a spinner while the pipeline runs (typically 1–4 seconds in mock mode).

### Pipeline Result

After submission, a result panel appears showing:
- **Claim ID**: System-assigned identifier (e.g., `CLM-XXXXX`)
- **Final Status**: `STP_AUTHORIZED` / `ADJUSTER_REVIEW` / `ON_HOLD` / `COVERAGE_DISPUTE`
- **Pipeline Duration**: Wall-clock time for all stages
- **LLM Provider**: Which LLM processed the claim

The **visual pipeline track** shows all stages color-coded:
- 🟢 Green = PASS
- 🟡 Amber = HITL (Human-In-The-Loop required)
- 🔴 Red = HOLD / FAIL
- ⬜ Grey = SKIPPED (conditional stages)

**Quick actions after submit:**
- *View Detailed Pipeline →* — jumps to Tab 02 with this claim selected
- *Ask Co-Pilot →* — jumps to Tab 04 with this claim loaded
- *Open SIU Dossier →* — appears only when fraud band is HIGH or CRITICAL

---

## 5. Tab 02 — Live Pipeline Trace {#tab02}

Deep-dive into the stage-by-stage decisions for any submitted claim.

### Selecting a Claim

Use the dropdown at the top to pick a claim by ID + status. The most recently submitted claim is auto-selected when navigating from Tab 01.

### Stage Cards

Each pipeline stage renders as a collapsible card. The first two stages expand by default.

Each card shows:
- **Stage ID** (S0, S1, S2, S3, S4A, S4B, A11, S5, S6, S7)
- **Stage Name** and **Agent**
- **Elapsed time** (milliseconds per stage)
- **Status chip** (PASS / HITL / HOLD / FAIL)

Expand a card to see:
- **Stage Outputs**: Key-value pairs of the agent's output fields
- **Decision Records**: Individual rule decisions with:
  - Rule ID and decision label
  - Confidence score (0.00–1.00)
  - HITL flag (amber badge when human review required)
  - Rationale narrative

### Raw JSON Trace

Click "▸ Raw JSON Trace" at the bottom to see the complete pipeline trace as formatted JSON. Useful for debugging or copying data for downstream integration.

---

## 6. Tab 03 — Claims Dashboard {#tab03}

Table view of all submitted claims with key summary fields.

### Columns

| Column | Source | Notes |
|---|---|---|
| Claim ID | System-generated | Click row to open pipeline trace |
| Policy | Intake form | |
| Cause | Intake form | Loss cause code |
| Location | Intake form | |
| Status | SOR | STP_AUTHORIZED · ADJUSTER_REVIEW · ON_HOLD |
| Track | S3 Triage output | FAST_TRACK · STANDARD · COMPLEX |
| Fraud Band | S4A output | LOW · MEDIUM · HIGH · CRITICAL (red for HIGH+) |
| AI Damage Est. | S4B output | AI-estimated repair cost |
| Settlement | S6 output | Authorized settlement amount |

### Search

Type in the search box to filter claims by Claim ID, policy number, or loss cause.

### Clicking a Row

Clicking any claim row navigates to Tab 02 (Pipeline Trace) with that claim selected.

---

## 7. Tab 04 — Adjuster Co-Pilot (A9) {#tab04}

AI-powered assistant grounded in the pipeline trace and claim record. Answers adjuster questions without hallucination — all responses are anchored to actual claim data.

### Selecting a Claim

Click a claim in the left sidebar. The Co-Pilot loads proactive alerts automatically for any newly selected claim.

### Proactive Alerts

When you first open a claim in the Co-Pilot, the system automatically generates alerts for:
- Fraud signals that warrant attention
- Coverage gaps or reservation of rights triggers
- Subrogation opportunities
- Missing documentation
- Compliance deadlines

These appear as amber-highlighted messages at the top of the chat.

### Quick Prompts (Left Sidebar)

Pre-built prompts to get started:

| Prompt | Purpose |
|---|---|
| **Next best action** | What should I do right now on this claim? |
| **Executive summary** | Brief summary for supervisor handoff |
| **Draft ROR letter** | Reservation of rights letter draft |
| **Subrogation check** | Is there recovery potential? Against whom? |
| **Fraud analysis** | Explain the fraud score and specific signals |
| **Compliance check** | What compliance items are outstanding? |

### Custom Questions

Type any question in the text area. The Co-Pilot has full context of:
- All intake fields
- Every pipeline stage output
- Every decision record and rationale
- Coverage details from the SOR
- Settlement authorization details

**Send:** Click **Send** button or press **Ctrl+Enter** (Cmd+Enter on Mac)

### Example Questions

```
What is the fraud score and what are the top 3 reasons for it?
Draft a diary note for this claim suitable for Duck Creek.
Is there subrogation potential here? Explain your reasoning.
What documents should I request from the claimant?
Has the coverage reservation been triggered? What is the basis?
Summarize this claim for handoff to the complex claims team.
```

---

## 8. Tab 05 — Conversational FNOL (A10) {#tab05}

The **L3 Vision** endpoint — a customer-facing AI agent that conducts the entire FNOL intake conversation. The 9-agent pipeline runs invisibly beneath the surface.

### Starting a Conversation

The agent greets automatically on first load. If the API is offline, an offline message appears.

### How It Works

1. Customer describes their loss in natural language
2. The agent asks follow-up questions to capture all required FNOL fields
3. When all required fields are captured, the pipeline runs automatically
4. A completion message shows Claim ID and final status
5. The claim appears in Tab 03 and Tab 02

### Captured Fields Panel (Left Sidebar)

Shows all FNOL fields extracted from the conversation in real time. This is what gets submitted to the pipeline.

### Reset

Click **⟲ New Conversation** to start fresh with a new session.

### Sample Opening Phrases

- "I was just rear-ended on the highway"
- "Someone broke into my car last night and stole my belongings"
- "My car was totalled in a collision this morning"
- "I hit a deer on the interstate and the damage is severe"

---

## 9. Tab 06 — Total Loss & Salvage (A11) {#tab06}

Manages total-loss evaluations triggered by Stage S4B. A11 runs **automatically** when S4B determines the repair estimate exceeds the state Total-Loss Threshold (TLT).

### Evaluations List (Left Panel)

Shows all total-loss evaluations with:
- Evaluation ID
- Claim ID
- State / TLT percentage
- Observed TLT percentage
- ACV and repair estimate

Click an evaluation to load the detail panel.

### Evaluation Detail (Right Panel)

**Summary Cards:**
- State and applicable TLT (e.g., TX = 75%)
- Observed percentage (repair + prior damage / ACV)
- Repair estimate
- Final ACV (after adjustments)

**ACV Calculation Breakdown** (expandable):
- Base ACV
- Mileage adjustment
- Condition adjustment
- Options/features adjustment
- Final ACV and confidence score
- Valuation basis (comparable sales, book value, market)

**Settlement Options:**

*Option A — Carrier retains salvage:* Carrier pays full ACV minus deductible. Carrier keeps and sells the vehicle.

*Option B — Owner retains salvage:* Carrier pays ACV minus deductible minus salvage value. Owner keeps the vehicle (branded title).

Click **Record owner decision** to log the insured's choice.

**Salvage Assignment:**
- Shows assigned vendor (Copart / IAA / Mock)
- Lot ID, yard location, pickup ETA
- Expected net return and salvage recovery %
- Re-assign to different vendor using the buttons

**Customer Notification Letter:**
Click **Generate letter draft** to produce an AI-drafted customer notification letter. The letter is populated with actual claim figures and state-specific legal language.

### Salvage Vendor Network

A11 uses shadow-quoting to select the best vendor:
- **Copart**: Real partner API (configure with `COPART_API_KEY`). Runs in shell mode without credentials.
- **IAA**: Real partner API (configure with `IAA_API_KEY`). Runs in shell mode without credentials.
- **Auto**: Shadow-quotes all vendors, selects highest expected net return.

---

## 10. Tab 07 — SIU Case Builder (A12) {#tab07}

**New in v5.** Automatically identifies suspect claims and assembles a referral package for the Special Investigations Unit.

### How A12 Works

A12 monitors claims processed by the fraud detection agent (S4A). When a claim returns a fraud band of **HIGH** or **CRITICAL**, it appears in the SIU suspect list automatically.

### Suspect Claims List (Left Panel)

Each suspect claim shows:
- Claim ID and fraud band badge
- Policy number and loss cause
- Fraud Score (0–100%) with visual risk bar
- Triggered fraud categories

**Risk bar colors:**
- 🟢 Green = LOW (< 40%)
- 🟡 Amber = MEDIUM (40–60%)
- 🔴 Orange = HIGH (60–80%)
- 🔴 Dark = CRITICAL (> 80%)

### SIU Dossier Builder (Right Panel)

Click a suspect claim to open the dossier builder.

**SIU Risk Score**
Visual meter showing composite fraud score with triggered categories listed.

**Fraud Signals Grid**
8 key fraud indicators shown as cards. Cards highlight red when a signal is active:
- Prior claims frequency (last 36 months)
- ISO ClaimSearch match
- Policy tenure at time of loss
- Photo quality and count
- Attorney representation at FNOL
- Telematics event correlation
- Loss time anomaly

**Evidence Dossier**
Pre-populated evidence items based on flagged signals. Each item shows:
- Evidence type (DATABASE / PHOTO / STATEMENT / ANALYSIS)
- Description
- FLAGGED badge for active signals

Click **+ Add Evidence Item** to manually add items (prompts for type and description).

**Adjuster Notes**
Free-text field for investigator observations. Saved with the dossier and included in the referral package.

**AI Decision Records**
All fraud detection decisions from S4A, with rule IDs, confidence scores, and rationale.

### Generating the Referral Package

Click **Generate SIU Referral Package**. The system produces:
- Reference number (`SIU-REF-CLMXXXXX-TIMESTAMP`)
- Full claim summary
- Fraud risk assessment
- Numbered red flags
- Recommended next steps (field investigation, recorded statement, IVI, etc.)
- Compliance note (NAIC Model Bulletin §IV, FCRA §615)

### Exporting

Click **Export Dossier** to download the referral package as a `.txt` file suitable for email or case management upload.

### SIU Analytics Panel

Bottom panel shows aggregate statistics:
- Total suspects flagged
- Average fraud score across suspects
- Number of referrals generated
- Most common fraud category

---

## 11. Tab 08 — System Health {#tab08}

### Health & Configuration

Live status of the platform components:
- Service name and version
- Status (ok / degraded)
- LLM provider and health
- SOR type and connection status
- Number of policies and pipeline stages

Click **⟳ Refresh** to re-query the server.

### Pipeline Thresholds

POC default thresholds used by the pipeline agents. Production calibration is required before go-live.

Key thresholds:
- `stp_fraud_max`: Maximum fraud score to allow STP (default 0.30)
- `fast_track_complexity_max`: Maximum complexity score for fast-track routing
- `total_loss_default_pct`: Default TLT when state-specific not found (typically 0.75)
- `subro_threshold_min`: Minimum subrogation probability to flag

### Policy Registry

All POC policies available for testing, with named insured, jurisdiction state, and in-force dates.

---

## 12. POC Test Policies {#policies}

| Policy Number | Named Insured | State | Vehicle | Notes |
|---|---|---|---|---|
| `POC-POL-00123` | (standard insured) | TX | 2020 Honda Accord | Standard coverage — use for normal claims |
| `POC-POL-00456` | (standard insured) | GA | 2022 Toyota Camry | Liability + collision — use for fraud scenarios |
| `POC-POL-00789` | (standard insured) | TX | 2023 Tesla Model 3 | High ACV — best for triggering A11 total loss |
| `POC-POL-00999` | (expired) | — | — | Expired policy — triggers coverage denial path |

---

## 13. Demo Scenarios {#demos}

### Scenario 1: Standard Rear-End → STP

1. Click **Demo · Rear-end (TX)**
2. Review pre-filled fields (feel free to modify)
3. Click **⟶ Run Full 9-Agent Pipeline**
4. Expected result: `STP_AUTHORIZED` — all green stages
5. Navigate to Tab 02 to see the full decision trace
6. Navigate to Tab 04 and ask: *"What is the next best action?"*

### Scenario 2: Fraud-Flagged Claim → SIU

1. Click **Demo · Fraud Signals**
2. Note: ISO match checked, short tenure, attorney rep, low photo quality
3. Submit the pipeline
4. Expected result: `ADJUSTER_REVIEW` — S4A shows HIGH or CRITICAL fraud band
5. Click **Open SIU Dossier →** (appears in result panel)
6. Or navigate to Tab 07 — claim appears in suspect list
7. Click the claim → review evidence → click **Generate SIU Referral Package**

### Scenario 3: Total Loss → A11

1. Click **Demo · Total Loss (EV)**
2. Note: `$62,000` estimated loss vs `$58,000` ACV — exceeds TX TLT (75%)
3. Submit
4. Expected result: `ADJUSTER_REVIEW` — A11 stage appears in pipeline track
5. Navigate to Tab 06 — evaluation should appear in the list
6. Click the evaluation → review ACV breakdown → view settlement options
7. Click **Assign salvage (auto)** → observe vendor selection and net return
8. Click **Generate letter draft** to see the customer notification

### Scenario 4: Conversational Intake

1. Navigate to Tab 05
2. Agent greets you automatically
3. Say: *"I was rear-ended at a red light in Austin this morning"*
4. Follow the agent's questions for all required fields
5. When complete, pipeline runs and claim appears in Tab 03

---

## 14. LLM Provider Configuration {#llm}

The platform uses a multi-provider LLM abstraction. Select provider via `FNOL_LLM_PROVIDER`:

### Anthropic (Claude)

```bash
FNOL_LLM_PROVIDER=anthropic \
ANTHROPIC_API_KEY=sk-ant-... \
python fnol_launcher.py
```

### Azure OpenAI

```bash
FNOL_LLM_PROVIDER=azure_openai \
AZURE_OPENAI_API_KEY=... \
AZURE_OPENAI_ENDPOINT=https://your-instance.openai.azure.com/ \
AZURE_OPENAI_DEPLOYMENT=gpt-4o \
python fnol_launcher.py
```

### AWS Bedrock

```bash
FNOL_LLM_PROVIDER=bedrock \
AWS_REGION=us-east-1 \
AWS_ACCESS_KEY_ID=... \
AWS_SECRET_ACCESS_KEY=... \
python fnol_launcher.py
```

### Mock (No API key required)

```bash
FNOL_LLM_PROVIDER=mock python fnol_launcher.py
```

Mock mode uses deterministic rule-based logic for all agents. All pipeline stages function correctly; LLM-drafted content (letters, Co-Pilot Q&A) uses realistic template output. **Recommended for demos without live LLM credentials.**

---

## 15. Regulatory Compliance {#compliance}

The platform is designed with the following regulatory frameworks in scope:

| Framework | Scope | Implementation |
|---|---|---|
| **NAIC Model Bulletin on AI** | AI usage disclosure, human oversight | HITL flags on decisions above confidence thresholds, governance layer |
| **Colorado Reg 10-1-1** | Algorithmic bias prohibition | Demographic proxy monitoring (monitoring-only, never input features) |
| **NYDFS Circular Letter No. 7** | Explainability of AI decisions | Full rationale narrative on every decision record |
| **FCRA §615** | Adverse action notices | FCRA adverse-action template auto-triggered on coverage denial |
| **State TLT Laws** | 50-state total-loss thresholds | A11 applies state-specific TLT from all 50 state addenda |

### Key compliance principles

1. **Every financial figure is deterministic.** LLMs draft letters and answer questions — they do not calculate amounts.
2. **Full audit trail.** Every decision record carries rule ID, confidence, rationale, and timestamp. SQLite Decision Log backend by default.
3. **Demographic proxies are monitoring-only.** The bias monitor tracks DOB, gender, preferred language, and garaging ZIP — none are used as input features.
4. **HITL flags are binding.** Any decision with `hitl_required=True` must be reviewed by a licensed adjuster before adverse action.
5. **FCRA adverse-action templates** fire automatically on coverage denial or STP rejection.

---

## 16. Troubleshooting {#troubleshooting}

### API is offline / red dot in header

1. Ensure the server is running: `python fnol_launcher.py`
2. Default port is `8000` — check that nothing else is using it
3. Check console for Python errors
4. Verify `requirements.txt` dependencies: `pip install -r requirements.txt --break-system-packages`

### "Invalid or missing X-API-Key"

1. Click **API Key** button in top-right
2. Enter `fnol-api-key-2026` (default) or the value of `$FNOL_API_KEY` on the server

### Pipeline returns error

1. Check that the policy number exists in the POC registry
2. Verify all required fields are filled (Policy Number, Loss Cause, Loss Description)
3. Check the server console for traceback

### Co-Pilot returns "No claim/pipeline"

1. The Co-Pilot requires a submitted claim with a pipeline trace in memory
2. Submit a claim first, then select it in the Co-Pilot sidebar
3. In-memory traces reset when the server restarts; re-submit claims after restart

### Conversational agent doesn't start

1. Check API health — server must be running
2. Click **⟲ New Conversation** to reset the session
3. With `mock` provider, the agent uses rule-based responses — they are shorter than Claude responses

### Total loss evaluation not appearing

1. The claim must have been submitted with `estimated_loss_usd` near or above the TLT % of `vehicle_acv_usd`
2. Use Demo 3 (Total Loss) which pre-fills `$62,000` loss vs `$58,000` ACV for TX (TLT=75%)
3. A11 runs automatically when S4B flags total loss — confirm S4B is not showing `SKIPPED`

### SIU tab shows no suspect claims

1. Claims need a pipeline trace loaded with an S4A fraud band of HIGH or CRITICAL
2. Use Demo 2 (Fraud Signals) — it seeds fraud signals that push to HIGH/CRITICAL
3. Click **⟳ Refresh Claims** on the SIU tab to re-check loaded pipelines

### CORS error in browser console

The server uses `allow_origins=["*"]` + `allow_credentials=False` to avoid the Starlette CORS incompatibility. If you see CORS errors, check that:
- The server is running on the same port as the URL in the browser
- No proxy is rewriting Origin headers
- `allow_credentials` is `False` (not `True`) in `fnol_api_server.py`

---

*FNOL Intelligence Platform — Internal Accenture IP*  
*Insurance Claims Intelligence Practice · Auto Claims Center of Excellence*  
*Not for distribution outside Accenture without client engagement authorization.*
