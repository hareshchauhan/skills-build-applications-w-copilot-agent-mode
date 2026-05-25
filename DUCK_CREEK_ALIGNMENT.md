# Duck Creek Alignment

**FNOL Intelligence Platform v4.1.0 — How A9, A10, and A11 fit the Duck Creek L3 strategy**

This document explains why the platform is the natural agentic complement to Duck Creek Claims, and why **A11 — Total-Loss & Salvage Orchestrator** is a Marketplace-ready addition that Duck Creek does not solve natively today.

---

## 1 · Duck Creek's L3 roadmap

Duck Creek's published "L3 vision" anticipates a claims experience where:

* The carrier is contacted by a customer through any channel
* An AI agent captures, classifies, validates, and routes the claim conversationally
* The core platform (Duck Creek Claims) runs its decisioning rules, reserves, and assignments
* The adjuster operates with AI co-pilot assistance on top of the claim record
* High-volume specialised flows — total loss, salvage, subrogation, SIU — execute with minimal human routing

Duck Creek provides the **system of record**, **policy admin**, **rating**, and the **rule engine**. It does not natively provide the **conversational agent**, the **co-pilot intelligence**, or the **total-loss/salvage orchestration**. Those are the integration points.

---

## 2 · How the platform aligns

| Duck Creek layer | FNOL Intelligence Platform contribution |
|---|---|
| **Claim intake (FNOL form / portal / IVR)** | A10 Conversational FNOL Agent replaces or augments the form; emits a fully populated FNOL payload back to Duck Creek |
| **Coverage & reservation rules** | S2 pre-decisions coverage/limits before the claim hits Duck Creek; cuts rework |
| **Triage & assignment** | S3 produces complexity scores, recommended track, and adjuster pick; Duck Creek workflows consume them as routing inputs |
| **Fraud screening** | S4A fraud band feeds Duck Creek's SIU referral workflows |
| **Damage assessment** | S4B AI estimate populates the Duck Creek claim record before adjuster review |
| **Total loss & salvage** | **A11** drives the entire TL workflow end-to-end: TLT, ACV, branded title, settlement options, vendor assignment, customer letter — Duck Creek receives the settled outcome |
| **Adjuster workspace** | A9 Co-Pilot embeds in the Duck Creek adjuster UI via iframe or sidecar |
| **BI / liability / settlement / subrogation** | S5–S7 produce pre-computed recommendations; Duck Creek's authority/approval rules adjudicate |

The boundary is clean: **the platform is a thinking layer that emits Decision Records and structured outputs; Duck Creek remains the system of record**. Carriers don't replace Duck Creek; they augment it.

---

## 3 · Why A11 is the Marketplace whitespace

Total-loss settlement is one of the highest-friction, highest-NPS-impact moments in the claims lifecycle. It is also one of the most operationally fragmented:

* The **TLT calculation** is state-dependent (60% in OK, 80% in FL, formula-based in TX, etc.)
* The **ACV refinement** requires book sources, comparables, mileage/condition adjustments
* The **branded title decision** is regulatory and varies by damage cause
* The **salvage assignment** requires Copart or IAA partner-API integration
* The **settlement math** must include state sales tax and title fees
* The **customer letter** must satisfy state-specific disclosure rules

No major carrier solves all of this in Duck Creek directly. Most run it through a patchwork: spreadsheets for TLT, vendor portals for salvage assignments, manual letters, periodic state-rule updates pushed by compliance teams. A11 collapses the patchwork into a single orchestrated stage with full audit trails.

**This is the Marketplace conversation** — A11 as a packaged Duck Creek extension that:

* Pre-installs the 51-jurisdiction TLT and tax tables
* Ships with Copart and IAA adapter shells (carrier supplies credentials)
* Emits Decision Records compatible with Duck Creek's audit log
* Renders the customer letter using the carrier's LLM provider of choice

---

## 4 · Integration architecture

```
                    ┌────────────────────────────────────────┐
                    │            DUCK CREEK CLAIMS           │
                    │  (system of record · rules · authority)│
                    └────────────┬───────────────────────────┘
                                 │  REST + EDI 906/810/820
                    ┌────────────▼───────────────────────────┐
                    │  FNOL Intelligence Platform v4.1       │
                    │                                        │
                    │  S0 → S4B → A11 → S5 → S7              │
                    │             │                          │
                    │             ├─ Salvage Adapter ──→ Copart │
                    │             ├─ Salvage Adapter ──→ IAA    │
                    │             └─ LLM Adapter ──→ Anthropic/Azure/Bedrock/...│
                    │                                        │
                    └────────────────────────────────────────┘
```

The Duck Creek custom-step pattern (Server Component → external HTTP call) is the wiring approach. A11's API surface is REST/JSON over HTTPS with an API key header — production-ready for Duck Creek custom-action integration.

---

## 5 · Net economic argument for A11

A typical mid-market US auto carrier processes ~10,000 total-loss claims per year. Industry benchmarks suggest:

| Metric | Status quo | With A11 |
|---|---|---|
| Time from TL declaration to vendor assignment | 24-72h | < 30s |
| ACV calculation cycle time | 2-4h adjuster effort | seconds |
| Salvage vendor net recovery | single-vendor lock-in | optimised via shadow quotes |
| Customer letter drafting | 30-45 min adjuster effort | seconds |
| Compliance evidence per TL claim | ad-hoc | full Decision Record + audit hash |

The shadow-quote vendor selection alone — choosing the higher of Copart vs IAA expected net return on every assignment — recovers materially more value when applied across 10,000+ claims. The customer-experience side (immediate, plain-English letter with two clear options) reduces complaint volume and DOI exposure.

---

## 6 · Guidewire compatibility

The architecture is platform-agnostic. The SOR adapter pattern (`fnol_sor_adapter.py`) supports both Duck Creek and Guidewire ClaimCenter; the salvage adapter pattern (`fnol_salvage_adapter.py`) supports Copart and IAA equally. A11 itself touches neither SOR nor salvage vendor directly — it operates through these adapters. The same A11 module ships unchanged for a Guidewire carrier.

This is intentional. Accenture P&C delivery teams move across Duck Creek and Guidewire engagements; the platform should not pre-commit either carrier to a single SOR.

---

## 7 · Sales motion for A11

The buyer audience for A11 is materially different from a general claims-automation pitch:

* **Claims COO / VP Claims** — operational efficiency, cycle time, recovery uplift
* **CRO / Chief Compliance Officer** — Decision Records, audit trail, state TLT/tax table governance
* **General Counsel / Head of Legal** — customer letter consistency, branded-title compliance, FCRA adverse-action alignment
* **Head of Salvage / Vehicle Disposition** — vendor optimisation, shadow-quote transparency

Accenture's asymmetric advantage here is the **combination** of (a) implementation speed via the prebuilt accelerator, (b) multi-carrier delivery experience, (c) integrated regulatory and risk-consulting depth, and (d) Duck Creek and Guidewire platform certifications. Most insurtech vendors can claim one or two of these; very few claim all four.

---

## 8 · Roadmap toward Marketplace listing

| Milestone | Status |
|---|---|
| A11 ships in v4.1.0 with full TLT/tax tables, Copart/IAA adapters, customer letter | ✓ |
| Stakeholder validation with Accenture P&C principal (CRO-relationship) | In progress |
| Pilot carrier validates A10 + A11 combo against their own claim flows | Pending |
| Duck Creek custom-step packaging (Server Component template) | Pending |
| Marketplace listing & co-sell motion with Duck Creek | Future |

A11 is the most compelling Marketplace candidate in the platform today because (a) its scope is contained, (b) the regulatory tables are heavy lift for carriers to maintain themselves, and (c) the salvage-vendor optimisation produces measurable recovery uplift on day one.
