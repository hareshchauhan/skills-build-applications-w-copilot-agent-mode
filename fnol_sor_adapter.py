"""
FNOL Intelligence Platform — SOR Adapter
========================================
Platform-agnostic adapter layer for System-of-Record integration.

Adapters (one canonical interface, three implementations):
  - DuckCreekAdapter   — primary SOR (REST/OnDemand-style)
  - GuidewireAdapter   — secondary SOR (ClaimCenter REST stubs)
  - MockAdapter        — in-memory POC store (default)

Selection: SOR_TYPE env var ∈ {'mock','duckcreek','guidewire'}; default 'mock'.

L1/L2/L3 stability principle (per Blueprint V2):
  The CANONICAL schema and adapter pattern remain stable across maturity levels.
  Only the call frequency and UX surface change. SOR adapters never become
  obsolete — they migrate from "primary UI source" (L1) to "back-office only" (L3).

Production hardening required before client deployment:
  - OAuth2/SAML token management with refresh
  - Idempotency keys on every write
  - Circuit breaker + retry-with-jitter
  - PII-aware request/response logging
  - State machine reconciliation on partial failure
"""

from __future__ import annotations
import datetime as _dt
import os
import threading
import time as _time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Protocol


# ───────────────────────────────────────────────────────────────────────────
# Canonical policy & claim shapes (subset of Blueprint FNOL Data Dictionary)
# ───────────────────────────────────────────────────────────────────────────

CANONICAL_POLICIES: Dict[str, Dict[str, Any]] = {
    "POC-POL-00123": {
        "policy_number": "POC-POL-00123",
        "policy_system": "DUCKCREEK",
        "in_force_from": "2025-01-01T00:00:00Z",
        "in_force_to":   "2026-12-31T23:59:59Z",
        "named_insured": "Aria Castillo",
        "jurisdiction_state": "TX",
        "vehicles": [
            {"vin": "1HGCM82633A123456", "year": 2021, "make": "Honda", "model": "Accord"},
        ],
        "coverages": {
            "collision": {"limit": 50000, "deductible": 500},
            "comprehensive": {"limit": 50000, "deductible": 250},
            "bi_per_person": 100000,
            "bi_per_occurrence": 300000,
            "um_uim": 100000,
            "rental_daily": 50,
            "medpay": 5000,
        },
        "telematics_consent": True,
        "exclusions": [],
    },
    "POC-POL-00456": {
        "policy_number": "POC-POL-00456",
        "policy_system": "DUCKCREEK",
        "in_force_from": "2025-06-01T00:00:00Z",
        "in_force_to":   "2026-05-31T23:59:59Z",
        "named_insured": "Jordan Mehta",
        "jurisdiction_state": "GA",
        "vehicles": [
            {"vin": "4T1BF1FK5GU123456", "year": 2019, "make": "Toyota", "model": "Camry"},
        ],
        "coverages": {
            "collision": {"limit": 35000, "deductible": 1000},
            "comprehensive": {"limit": 35000, "deductible": 500},
            "bi_per_person": 50000,
            "bi_per_occurrence": 100000,
            "um_uim": 50000,
            "rental_daily": 35,
            "medpay": 2500,
        },
        "telematics_consent": False,
        "exclusions": [],
    },
    "POC-POL-00789": {
        "policy_number": "POC-POL-00789",
        "policy_system": "DUCKCREEK",
        "in_force_from": "2025-03-15T00:00:00Z",
        "in_force_to":   "2026-03-14T23:59:59Z",
        "named_insured": "Priya Donnelly",
        "jurisdiction_state": "TX",
        "vehicles": [
            {"vin": "5YJ3E1EA7KF123456", "year": 2023, "make": "Tesla", "model": "Model 3"},
        ],
        "coverages": {
            "collision": {"limit": 80000, "deductible": 1000},
            "comprehensive": {"limit": 80000, "deductible": 500},
            "bi_per_person": 250000,
            "bi_per_occurrence": 500000,
            "um_uim": 250000,
            "rental_daily": 75,
            "medpay": 10000,
        },
        "telematics_consent": True,
        "exclusions": [],
    },
    "POC-POL-00999": {
        "policy_number": "POC-POL-00999",
        "policy_system": "DUCKCREEK",
        "in_force_from": "2023-01-01T00:00:00Z",
        "in_force_to":   "2024-12-31T23:59:59Z",   # expired
        "named_insured": "Sam Whittaker",
        "jurisdiction_state": "CA",
        "vehicles": [
            {"vin": "JM1BL1L78C1234567", "year": 2017, "make": "Mazda", "model": "3"},
        ],
        "coverages": {
            "collision": {"limit": 25000, "deductible": 1000},
            "comprehensive": {"limit": 25000, "deductible": 500},
            "bi_per_person": 25000,
            "bi_per_occurrence": 50000,
            "um_uim": 25000,
            "rental_daily": 0,
            "medpay": 0,
        },
        "telematics_consent": False,
        "exclusions": ["lapse_gap"],
    },
}


# ───────────────────────────────────────────────────────────────────────────
# Payment data contracts
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class PaymentRequest:
    """One payment authorization + disbursement request.

    Mirrors Duck Creek OnDemand ClaimPayment schema.
    All dollar amounts in USD. payment_type drives the disbursement channel.
    """
    claim_id:           str
    payment_type:       str          # PD | BI | RENTAL | MEDPAY | STP
    payment_method:     str          # ACH | CHECK | ZELLE | WIRE | HOLD
    amount_usd:         float
    payee_name:         str
    payee_account:      Optional[str] = None   # masked account ref, carrier-vaulted
    payee_routing:      Optional[str] = None   # ABA routing, carrier-vaulted
    payee_address:      Optional[str] = None   # for CHECK disbursements
    memo:               str = ""
    authority_tier:     str = "AUTO"           # AUTO | ADJUSTER | SUPERVISOR | DIRECTOR
    authorized_by:      Optional[str] = None   # adjuster employee ID
    idempotency_key:    str = field(default_factory=lambda: uuid.uuid4().hex)
    coverage_part:      Optional[str] = None   # collision | comprehensive | bi | etc.
    deductible_applied: float = 0.0
    release_obtained:   bool = False


@dataclass
class PaymentResponse:
    """Normalized payment response from the SOR.

    Contains both the authorization result and, when immediately processed,
    the disbursement status. Duck Creek separates Authorize → Disburse into
    two API calls; the adapter surfaces them as one logical operation for
    STP but exposes both IDs for the audit trail.
    """
    payment_id:         str            # Platform payment reference
    sor_payment_id:     Optional[str]  # DC ClaimPaymentId (None in shell/mock)
    claim_id:           str
    status:             str            # AUTHORIZED | PENDING_DISBURSE | DISBURSED |
                                       # FAILED | VOIDED | ON_HOLD
    payment_type:       str
    payment_method:     str
    amount_usd:         float
    payee_name:         str
    authority_tier:     str
    authorized_at:      str            # ISO 8601 UTC
    disbursed_at:       Optional[str] = None
    expected_settle_date: Optional[str] = None  # ACH T+1, CHECK T+5
    sor_transaction_ref: Optional[str] = None
    failure_reason:     Optional[str] = None
    adapter_mode:       str = "mock"   # live | shell | mock


@dataclass
class PaymentStatusResponse:
    """Payment status query response."""
    payment_id:      str
    sor_payment_id:  Optional[str]
    claim_id:        str
    status:          str
    amount_usd:      float
    payment_method:  str
    authorized_at:   str
    disbursed_at:    Optional[str]
    cleared_at:      Optional[str]
    adapter_mode:    str


def _now_utc_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ───────────────────────────────────────────────────────────────────────────
# ACORD Gap 6 — SOR field mapping functions
#
# Canonical claim payload (from Claim.to_sor_payload()) → carrier SOR shape.
# Both functions are pure (no side effects, no I/O) so they can be unit-tested
# independently. Called by DuckCreekAdapter and GuidewireAdapter create_claim /
# update_claim to produce the SOR-specific request body.
#
# Field mapping authority:
#   Duck Creek OnDemand v23.x field names from DC Technical Reference Guide
#   Guidewire ClaimCenter 10.x REST API from GW Dev Hub documentation
#
# Gap coverage per adapter:
#   Gap 1  SourceChannelCd    → DC: SourceChannel        GW: intakeChannel
#   Gap 2  Party/Role arrays  → DC: Participants[]        GW: contacts[]
#   Gap 3  LossCauseCd        → DC: CauseOfLoss           GW: lossCause
#          LossTypeCd         → DC: ClaimType             GW: lossType
#          PoliceReport       → DC: PoliceReportNumber     GW: policeReport.*
#   Gap 4  DamageAreaCd       → DC: DamageArea            GW: impactType
#          LicensePlate       → DC: LicensePlateNumber     GW: licensePlateNumber
#   Gap 5  CoverageCd[]       → DC: CoverageLines[]        GW: coverageLines[]
#          AcvSourceCd        → DC: ACVSource             GW: acvSource
#          RorTriggerCd[]     → DC: RORTriggers[]          GW: rorTriggers[]
#   Gap 6  TelematicsInfo     → DC: TelematicsInfo{}       GW: telematicsInfo{}
#          CrashNotifSource   → DC: CrashNotificationSource GW: crashNotificationSource
#          TelematicsScope    → DC: TelematicsConsentScope  GW: telematicsDataScope
# ───────────────────────────────────────────────────────────────────────────

def _sv(val: Any) -> Optional[str]:
    """Return enum .value as string, or str(val), or None."""
    if val is None:
        return None
    return val.value if hasattr(val, "value") else str(val)


def _to_dc_claim_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Map canonical claim payload → Duck Creek OnDemand claim request body.

    Canonical payload comes from Claim.to_sor_payload() — a flat dict with
    enum values already serialised to strings. Nested objects (telematics,
    parties, passengers) are nested dicts.

    Returns a DC-shaped dict. Keys use DC PascalCase field names.
    Only non-None values are included; absent fields → DC uses policy defaults.

    Production note: carrier may have custom fields in their DC tenant — extend
    this mapping in the carrier-specific subclass rather than editing here.
    """
    tel = payload.get("telematics") or {}
    tel_source = tel.get("crash_notification_source_cd") or "UNKNOWN"
    tel_scope  = tel.get("telematics_data_scope") or "NONE"
    tel_consent = bool(
        tel_scope in ("FULL", "IMPACT_ONLY") or tel.get("consent_given", False)
    )

    body: Dict[str, Any] = {}

    # ── Core identity ─────────────────────────────────────────────────────
    if payload.get("claim_id"):
        body["ClaimNumber"]        = payload["claim_id"]
    if payload.get("policy_number"):
        body["PolicyNumber"]       = payload["policy_number"]
    if payload.get("source_channel_cd"):                      # Gap 1
        body["SourceChannel"]      = payload["source_channel_cd"]
    if payload.get("intake_quality_score") is not None:       # Gap 1
        body["IntakeQualityScore"] = payload["intake_quality_score"]
    if payload.get("status"):
        body["ClaimStatus"]        = payload["status"]

    # ── Reporter / Named Insured (flat fields — backward compat) ──────────
    if payload.get("reporter_name"):
        body["InsuredName"]        = payload["reporter_name"]
    if payload.get("reporter_phone"):
        body["InsuredPhone"]       = payload["reporter_phone"]
    if payload.get("reporter_email"):
        body["InsuredEmail"]       = payload["reporter_email"]

    # ── Loss details ──────────────────────────────────────────────────────
    if payload.get("loss_date_time"):
        body["LossDate"]           = payload["loss_date_time"]
    if payload.get("loss_location"):
        body["LossLocation"]       = payload["loss_location"]
    if payload.get("loss_location_zip"):
        body["LossLocationZip"]    = payload["loss_location_zip"]
    if payload.get("loss_cause"):
        body["CauseOfLossDesc"]    = payload["loss_cause"]
    if payload.get("loss_cause_cd"):                          # Gap 3
        body["CauseOfLoss"]        = payload["loss_cause_cd"]
    if payload.get("loss_type_cd"):                           # Gap 3
        body["ClaimType"]          = payload["loss_type_cd"]
    if payload.get("loss_description"):
        body["LossDescription"]    = payload["loss_description"]
    if payload.get("fatality_indicator"):
        body["FatalityIndicator"]  = "Y"

    # ── Police report (Gap 3) ─────────────────────────────────────────────
    if payload.get("police_report_filed"):
        body["PoliceReportFiled"]  = True
    if payload.get("police_report_number"):
        body["PoliceReportNumber"] = payload["police_report_number"]
    if payload.get("police_report_agency"):
        body["PoliceAgencyName"]   = payload["police_report_agency"]
    if payload.get("police_report_agency_phone"):
        body["PoliceAgencyPhone"]  = payload["police_report_agency_phone"]
    if payload.get("weather_condition_cd"):
        body["WeatherCondition"]   = payload["weather_condition_cd"]
    if payload.get("road_condition_cd"):
        body["RoadCondition"]      = payload["road_condition_cd"]

    # ── Vehicle (Gaps 4+) ─────────────────────────────────────────────────
    if payload.get("vin"):
        body["VIN"]                = payload["vin"]
    if payload.get("vehicle_year"):
        body["VehicleYear"]        = payload["vehicle_year"]
    if payload.get("vehicle_make"):
        body["VehicleMake"]        = payload["vehicle_make"]
    if payload.get("vehicle_model"):
        body["VehicleModel"]       = payload["vehicle_model"]
    if payload.get("vehicle_mileage"):
        body["Odometer"]           = payload["vehicle_mileage"]
    if payload.get("vehicle_state"):
        body["VehicleState"]       = payload["vehicle_state"]
    if payload.get("damage_area_cd"):                         # Gap 4
        body["DamageArea"]         = payload["damage_area_cd"]
    if payload.get("primary_damage_area"):
        body["DamageAreaDesc"]     = payload["primary_damage_area"]
    if payload.get("license_plate"):                          # Gap 4
        body["LicensePlateNumber"] = payload["license_plate"]
    if payload.get("registration_state"):                     # Gap 4
        body["LicensePlateState"]  = payload["registration_state"]
    if payload.get("drivable_indicator") is not None:
        body["DrivableIndicator"]  = "Y" if payload["drivable_indicator"] else "N"
    if payload.get("vehicle_recall_indicator"):
        body["RecallIndicator"]    = "Y"
    if payload.get("vehicle_acv_usd") is not None:
        body["VehicleACV"]         = {
            "Value": payload["vehicle_acv_usd"],
            "Currency": "USD",
            "Source": payload.get("acv_source_cd", "MISSING"),  # Gap 5
        }
    if payload.get("deductible_usd"):
        body["Deductible"]         = payload["deductible_usd"]

    # ── Financial / Coverage (Gap 5) ──────────────────────────────────────
    _cov_lines = payload.get("claimant_asserted_coverages") or []
    if _cov_lines:
        body["CoverageLines"] = [
            {
                "CoverageCode":   c.get("coverage_cd", ""),
                "Deductible":     c.get("deductible_usd"),
                "Limit":          c.get("limit_usd"),
                "ClaimantAsserts": c.get("applies"),
            }
            for c in _cov_lines
        ]
    _ror = payload.get("ror_trigger_cds") or []
    if _ror:
        body["RORTriggers"]        = _ror                     # Gap 5

    # ── Telematics (Gap 6) ────────────────────────────────────────────────
    if tel:
        body["TelematicsInfo"] = {
            "CrashAlertReceived":        tel.get("crash_alert_received", False),
            "DeltaVMph":                 tel.get("delta_v_mph", 0),
            "ImpactSeverityScore":       tel.get("impact_severity_score", 0),
            "AirbagDeployed":            tel.get("airbag_deployed", False),
            "ConsentGiven":              tel_consent,
            "CrashNotificationSource":   tel_source,           # Gap 6
            "TelematicsConsentScope":    tel_scope,            # Gap 6
            "OemEventId":                tel.get("oem_event_id"),
            "VehicleSpeedMph":           tel.get("vehicle_speed_mph"),
            "SeatbeltDeployed":          tel.get("seatbelt_deployed"),
            "TelematicsUsedInAI":        tel_consent,
        }
        # Location only when scope permits
        if tel_scope in ("FULL", "LOCATION_ONLY"):
            if tel.get("location_lat") is not None:
                body["TelematicsInfo"]["LocationLat"] = tel["location_lat"]
                body["TelematicsInfo"]["LocationLon"] = tel.get("location_lon")

    # ── Jurisdiction ──────────────────────────────────────────────────────
    if payload.get("state") or payload.get("vehicle_state"):
        body["LossState"] = (payload.get("state") or payload.get("vehicle_state", "")).upper()

    # Strip None values
    body["TelematicsInfo"] = {k: v for k, v in body.get("TelematicsInfo", {}).items() if v is not None}
    return {k: v for k, v in body.items() if v is not None and v != {} and v != []}


def _to_gw_claim_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Map canonical claim payload → Guidewire ClaimCenter REST claim body.

    Guidewire ClaimCenter 10.x uses camelCase JSON. Field names follow the
    GW REST API Developer Hub (cc/v1/claims POST endpoint).

    Same canonical input as _to_dc_claim_body(); only the key names and
    nested structure differ. Guidewire uses 'contacts[]' (not 'Participants[]')
    and 'coverageLines[]' with GW typelist-coded 'type' values.

    Carrier note: GW typelist values (lossType, lossCause, intakeChannel) are
    carrier-configured. The values here are GW defaults — override in the
    carrier-specific GW subclass for custom typelists.
    """
    tel = payload.get("telematics") or {}
    tel_scope  = tel.get("telematics_data_scope") or "NONE"
    tel_consent = bool(
        tel_scope in ("FULL", "IMPACT_ONLY") or tel.get("consent_given", False)
    )

    body: Dict[str, Any] = {}

    # ── Core identity ─────────────────────────────────────────────────────
    if payload.get("claim_id"):
        body["claimNumber"]          = payload["claim_id"]
    if payload.get("policy_number"):
        body["policy"]               = {"policyNumber": payload["policy_number"]}
    if payload.get("source_channel_cd"):                      # Gap 1
        body["intakeChannel"]        = payload["source_channel_cd"].lower()
    if payload.get("status"):
        body["state"]                = payload["status"]

    # ── Loss details ──────────────────────────────────────────────────────
    if payload.get("loss_date_time"):
        body["lossDate"]             = payload["loss_date_time"]
    if payload.get("loss_cause_cd"):                          # Gap 3
        body["lossCause"]            = payload["loss_cause_cd"].lower()
    if payload.get("loss_type_cd"):                           # Gap 3
        body["lossType"]             = payload["loss_type_cd"].lower()
    if payload.get("loss_description"):
        body["description"]          = payload["loss_description"]
    if payload.get("loss_location"):
        body["lossLocation"]         = {
            "addressLine1": payload["loss_location"],
            "postalCode":   payload.get("loss_location_zip", ""),
            "state":        (payload.get("state") or payload.get("vehicle_state", "")).upper(),
        }
    if payload.get("fatality_indicator"):
        body["fatalityIndicator"]    = True
    if payload.get("weather_condition_cd"):
        body["weatherCondition"]     = payload["weather_condition_cd"]
    if payload.get("road_condition_cd"):
        body["roadCondition"]        = payload["road_condition_cd"]

    # ── Police report (Gap 3) ─────────────────────────────────────────────
    if payload.get("police_report_filed") or payload.get("police_report_number"):
        body["policeReport"] = {
            "reportFiled":  payload.get("police_report_filed", False),
            "reportNumber": payload.get("police_report_number"),
            "agencyName":   payload.get("police_report_agency"),
            "agencyPhone":  payload.get("police_report_agency_phone"),
        }

    # ── Vehicle (Gaps 4+) ─────────────────────────────────────────────────
    _veh: Dict[str, Any] = {}
    if payload.get("vin"):
        _veh["vin"]               = payload["vin"]
    if payload.get("vehicle_year"):
        _veh["year"]              = payload["vehicle_year"]
    if payload.get("vehicle_make"):
        _veh["make"]              = payload["vehicle_make"]
    if payload.get("vehicle_model"):
        _veh["model"]             = payload["vehicle_model"]
    if payload.get("vehicle_mileage"):
        _veh["odometer"]          = payload["vehicle_mileage"]
    if payload.get("damage_area_cd"):                         # Gap 4
        _veh["impactType"]        = payload["damage_area_cd"]
    if payload.get("primary_damage_area"):
        _veh["impactTypeDesc"]    = payload["primary_damage_area"]
    if payload.get("license_plate"):                          # Gap 4
        _veh["licensePlateNumber"] = payload["license_plate"]
    if payload.get("registration_state"):
        _veh["licensePlateState"]  = payload["registration_state"]
    if payload.get("drivable_indicator") is not None:
        _veh["drivable"]          = bool(payload["drivable_indicator"])
    if payload.get("vehicle_acv_usd") is not None:
        _veh["acv"]               = {
            "amount": payload["vehicle_acv_usd"],
            "currency": "USD",
            "source": payload.get("acv_source_cd", "MISSING"),  # Gap 5
        }
    if _veh:
        body["vehicleIncident"]   = _veh

    # ── Financial / Coverage (Gap 5) ──────────────────────────────────────
    _cov_lines = payload.get("claimant_asserted_coverages") or []
    if _cov_lines:
        body["coverageLines"] = [
            {
                "type":         c.get("coverage_cd", "").lower(),
                "deductible":   c.get("deductible_usd"),
                "limit":        c.get("limit_usd"),
                "claimantAsserts": c.get("applies"),
            }
            for c in _cov_lines
        ]
    _ror = payload.get("ror_trigger_cds") or []
    if _ror:
        body["rorTriggers"]        = [r.lower() for r in _ror]  # Gap 5
    if payload.get("acv_source_cd"):
        body["acvSource"]          = payload["acv_source_cd"]   # Gap 5

    # ── Reporter contact ──────────────────────────────────────────────────
    if payload.get("reporter_name") or payload.get("reporter_phone"):
        body["contacts"] = [{
            "contactRole":   "insured",
            "displayName":   payload.get("reporter_name", ""),
            "workPhone":     payload.get("reporter_phone"),
            "emailAddress1": payload.get("reporter_email"),
        }]

    # ── Telematics (Gap 6) ────────────────────────────────────────────────
    if tel:
        _tel_body: Dict[str, Any] = {
            "crashAlertReceived":        tel.get("crash_alert_received", False),
            "deltaVMph":                 tel.get("delta_v_mph", 0),
            "impactSeverityScore":       tel.get("impact_severity_score", 0),
            "airbagDeployed":            tel.get("airbag_deployed", False),
            "consentGiven":              tel_consent,
            "crashNotificationSource":   tel.get("crash_notification_source_cd", "UNKNOWN"),  # Gap 6
            "telematicsDataScope":       tel_scope,             # Gap 6
            "oemEventId":                tel.get("oem_event_id"),
            "vehicleSpeedMph":           tel.get("vehicle_speed_mph"),
            "seatbeltDeployed":          tel.get("seatbelt_deployed"),
            "telematicsUsedInAi":        tel_consent,
        }
        if tel_scope in ("FULL", "LOCATION_ONLY"):
            if tel.get("location_lat") is not None:
                _tel_body["locationLat"] = tel["location_lat"]
                _tel_body["locationLon"] = tel.get("location_lon")
        body["telematicsInfo"] = {k: v for k, v in _tel_body.items() if v is not None}

    return {k: v for k, v in body.items() if v is not None and v != {} and v != []}


def _settle_date(method: str) -> str:
    """Expected settlement date per payment method.
    ACH: T+1 business day. CHECK: T+5 business days. WIRE: same-day if < 3pm ET.
    """
    days = {"ACH": 1, "ZELLE": 0, "WIRE": 1, "CHECK": 5}.get(method, 2)
    settle = _dt.date.today() + _dt.timedelta(days=days)
    return settle.isoformat()

class SORAdapter(Protocol):
    """Structural typing protocol — what every SOR adapter must expose."""
    name: str
    def lookup_policy(self, policy_number: str) -> Optional[Dict[str, Any]]: ...
    def create_claim(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...
    def get_claim(self, claim_id: str) -> Optional[Dict[str, Any]]: ...
    def update_claim(self, claim_id: str, patch: Dict[str, Any]) -> Dict[str, Any]: ...
    def list_claims(self) -> List[Dict[str, Any]]: ...
    def health(self) -> Dict[str, Any]: ...
    def authorize_payment(self, req: "PaymentRequest") -> "PaymentResponse": ...
    def disburse_payment(self, payment_id: str) -> "PaymentResponse": ...
    def get_payment_status(self, payment_id: str) -> "PaymentStatusResponse": ...
    def void_payment(self, payment_id: str, reason: str) -> "PaymentResponse": ...
    def list_payments(self, claim_id: str) -> List[Dict[str, Any]]: ...


# ───────────────────────────────────────────────────────────────────────────
# Abstract base
# ───────────────────────────────────────────────────────────────────────────

class BaseSORAdapter(ABC):
    """Abstract base for SOR adapters. Concrete subclasses MUST implement
    every method, OR explicitly opt into in-memory fallback by also mixing
    in `InMemoryFallbackMixin`. There is no silent fallback path."""

    name: str = "abstract"

    @abstractmethod
    def lookup_policy(self, policy_number: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def create_claim(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...

    @abstractmethod
    def get_claim(self, claim_id: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def update_claim(self, claim_id: str, patch: Dict[str, Any]) -> Dict[str, Any]: ...

    @abstractmethod
    def list_claims(self) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def health(self) -> Dict[str, Any]: ...

    @abstractmethod
    def authorize_payment(self, req: "PaymentRequest") -> "PaymentResponse": ...

    @abstractmethod
    def disburse_payment(self, payment_id: str) -> "PaymentResponse": ...

    @abstractmethod
    def get_payment_status(self, payment_id: str) -> "PaymentStatusResponse": ...

    @abstractmethod
    def void_payment(self, payment_id: str, reason: str) -> "PaymentResponse": ...

    @abstractmethod
    def list_payments(self, claim_id: str) -> List[Dict[str, Any]]: ...


# ───────────────────────────────────────────────────────────────────────────
# Mixin: explicit opt-in to in-memory fallback behaviour. Carrier adapters
# that haven't wired their real SOR yet inherit this to keep the POC working
# while making the fallback intentional and easy to grep for.
# ───────────────────────────────────────────────────────────────────────────

class InMemoryFallbackMixin:
    """In-memory CRUD + canonical-policy lookup. Stand-alone POC adapter
    behaviour, factored out so production adapters can opt in by inheritance
    rather than silently inherit from a fully-functional sibling."""

    def _init_inmemory(self) -> None:
        # Subclasses must call this from __init__.
        self._claims: Dict[str, Dict[str, Any]] = {}
        self._payments: Dict[str, PaymentResponse] = {}
        self._lock = threading.RLock()

    def lookup_policy(self, policy_number: str) -> Optional[Dict[str, Any]]:
        return CANONICAL_POLICIES.get(policy_number)

    def create_claim(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            cid = payload.get("claim_id") or f"CLM-{uuid.uuid4().hex.upper()}"
            payload = {**payload, "claim_id": cid,
                       "sor_system": getattr(self, "name", "unknown"),
                       "sor_record_id": f"{getattr(self, 'name', 'sor').upper()}-{cid}"}
            # Normalise ACORD-coded enum fields to string values at SOR boundary
            # so all downstream consumers read plain strings, not Enum instances.
            for _f in ("source_channel_cd", "loss_cause_cd", "loss_type_cd",
                       "damage_area_cd", "acv_source_cd"):
                _v = payload.get(_f)
                if _v is not None:
                    payload[_f] = _v.value if hasattr(_v, "value") else _v
            self._claims[cid] = payload
            return payload

    def get_claim(self, claim_id: str) -> Optional[Dict[str, Any]]:
        return self._claims.get(claim_id)

    def update_claim(self, claim_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            current = self._claims.get(claim_id) or {}
            merged = {**current, **patch, "claim_id": claim_id}
            self._claims[claim_id] = merged
            return merged

    def list_claims(self) -> List[Dict[str, Any]]:
        return list(self._claims.values())

    # ── Payment methods (in-memory / mock implementation) ─────────────────

    def authorize_payment(self, req: PaymentRequest) -> PaymentResponse:
        """Authorize a payment against the in-memory store.

        Deterministic mock that mirrors the DC OnDemand payment authorization
        response shape. Authority tier validation enforces Blueprint §S6 rules:
          AUTO:     amount ≤ $15,000 PD STP cap
          ADJUSTER: amount ≤ $50,000
          SUPERVISOR: amount ≤ $100,000
          DIRECTOR: unlimited (carrier-specific; POC allows all)
        """
        with self._lock:
            # Authority limit enforcement (POC thresholds — carrier must calibrate)
            limits = {"AUTO": 15_000, "ADJUSTER": 50_000, "SUPERVISOR": 100_000, "DIRECTOR": float("inf")}
            limit = limits.get(req.authority_tier, 15_000)
            if req.amount_usd > limit:
                resp = PaymentResponse(
                    payment_id=f"PAY-{uuid.uuid4().hex[:10].upper()}",
                    sor_payment_id=None,
                    claim_id=req.claim_id,
                    status="FAILED",
                    payment_type=req.payment_type,
                    payment_method=req.payment_method,
                    amount_usd=req.amount_usd,
                    payee_name=req.payee_name,
                    authority_tier=req.authority_tier,
                    authorized_at=_now_utc_iso(),
                    failure_reason=f"Amount ${req.amount_usd:,.2f} exceeds {req.authority_tier} limit ${limit:,.0f}",
                    adapter_mode=getattr(self, "_adapter_mode", "mock"),
                )
                self._payments[resp.payment_id] = resp
                return resp

            pid = f"PAY-{uuid.uuid4().hex[:10].upper()}"
            resp = PaymentResponse(
                payment_id=pid,
                sor_payment_id=f"{getattr(self,'name','MOCK').upper()}-PMT-{pid[-6:]}",
                claim_id=req.claim_id,
                status="AUTHORIZED",
                payment_type=req.payment_type,
                payment_method=req.payment_method,
                amount_usd=req.amount_usd,
                payee_name=req.payee_name,
                authority_tier=req.authority_tier,
                authorized_at=_now_utc_iso(),
                expected_settle_date=_settle_date(req.payment_method),
                sor_transaction_ref=f"TXN-{uuid.uuid4().hex[:8].upper()}",
                adapter_mode=getattr(self, "_adapter_mode", "mock"),
            )
            self._payments[pid] = resp
            # Patch the claim record with payment reference
            if req.claim_id in self._claims:
                existing = self._claims[req.claim_id].get("payment_references", [])
                existing.append(pid)
                self._claims[req.claim_id]["payment_references"] = existing
                self._claims[req.claim_id]["payment_status"] = "AUTHORIZED"
            return resp

    def disburse_payment(self, payment_id: str) -> PaymentResponse:
        """Trigger disbursement of an authorized payment.

        In mock mode: immediately marks as DISBURSED and stamps disbursed_at.
        Production DC: POST /api/claims/{claimId}/payments/{paymentId}/disburse
        """
        with self._lock:
            resp = self._payments.get(payment_id)
            if resp is None:
                raise KeyError(f"Payment {payment_id} not found")
            if resp.status not in ("AUTHORIZED", "PENDING_DISBURSE"):
                raise ValueError(f"Cannot disburse payment in status {resp.status}")
            resp.status = "DISBURSED"
            resp.disbursed_at = _now_utc_iso()
            if resp.claim_id in self._claims:
                self._claims[resp.claim_id]["payment_status"] = "DISBURSED"
                self._claims[resp.claim_id]["disbursed_at"] = resp.disbursed_at
            return resp

    def get_payment_status(self, payment_id: str) -> PaymentStatusResponse:
        resp = self._payments.get(payment_id)
        if resp is None:
            raise KeyError(f"Payment {payment_id} not found")
        return PaymentStatusResponse(
            payment_id=resp.payment_id,
            sor_payment_id=resp.sor_payment_id,
            claim_id=resp.claim_id,
            status=resp.status,
            amount_usd=resp.amount_usd,
            payment_method=resp.payment_method,
            authorized_at=resp.authorized_at,
            disbursed_at=resp.disbursed_at,
            cleared_at=None,   # Mock: no clearing confirmation
            adapter_mode=resp.adapter_mode,
        )

    def void_payment(self, payment_id: str, reason: str) -> PaymentResponse:
        with self._lock:
            resp = self._payments.get(payment_id)
            if resp is None:
                raise KeyError(f"Payment {payment_id} not found")
            if resp.status == "DISBURSED":
                raise ValueError("Cannot void a DISBURSED payment — initiate a recall instead")
            resp.status = "VOIDED"
            resp.failure_reason = reason
            return resp

    def list_payments(self, claim_id: str) -> List[Dict[str, Any]]:
        return [
            asdict(p) for p in self._payments.values()
            if p.claim_id == claim_id
        ]


# ───────────────────────────────────────────────────────────────────────────
# Mock implementation (POC default) — pure in-memory.
# ───────────────────────────────────────────────────────────────────────────

class MockAdapter(InMemoryFallbackMixin, BaseSORAdapter):
    name = "mock"
    _adapter_mode = "mock"

    def __init__(self) -> None:
        self._init_inmemory()

    def health(self) -> Dict[str, Any]:
        return {"adapter": self.name, "status": "ok",
                "payment_mode": self._adapter_mode,
                "policies_seeded": len(CANONICAL_POLICIES),
                "claims_count": len(self._claims),
                "payments_count": len(self._payments)}


# ───────────────────────────────────────────────────────────────────────────
# Duck Creek adapter (PRIMARY SOR). Inherits InMemoryFallbackMixin so the POC
# can run without DC connectivity — the fallback is consciously opted into,
# not inherited from MockAdapter. When live endpoints are wired, override
# the relevant methods to call DC; un-mix the fallback when fully cut over.
# ───────────────────────────────────────────────────────────────────────────

class DuckCreekAdapter(InMemoryFallbackMixin, BaseSORAdapter):
    """Duck Creek OnDemand-aligned adapter.

    Claim CRUD: falls back to in-memory until DC_BASE_URL + DC_API_KEY set.
    Payment API: live when credentials present; shell (realistic mock) otherwise.

    Duck Creek OnDemand payment endpoints used (carrier tenant-specific paths):
      POST   /api/v1/claims/{claimId}/payments/authorize
      POST   /api/v1/claims/{claimId}/payments/{paymentId}/disburse
      GET    /api/v1/claims/{claimId}/payments/{paymentId}
      DELETE /api/v1/claims/{claimId}/payments/{paymentId}   (void)

    Auth: OAuth 2.0 client credentials (DC_OAUTH_TOKEN or DC_API_KEY as Bearer).
    Idempotency: DC accepts X-Idempotency-Key header on all payment POSTs.

    Canonical-to-DC field mapping (carrier-specific in production):
      claim_number          → ClaimNumber
      policy_number         → PolicyNumber
      loss_date_time        → LossDate
      jurisdiction_state    → LossState
      named_insured         → InsuredName
      amount_usd            → PaymentAmount.Value + PaymentAmount.Currency="USD"
      payment_method        → PaymentMethod (ACH/CHECK/WIRE)
      payee_name            → PayeeName
      payee_account         → BankAccountNumber (carrier vault ref)
      payee_routing         → BankRoutingNumber (carrier vault ref)
    """
    name = "duckcreek"
    _adapter_mode = "shell"

    def __init__(self) -> None:
        self._init_inmemory()
        self.base_url = os.getenv("DC_BASE_URL", "").rstrip("/")
        self.api_key  = os.getenv("DC_API_KEY") or os.getenv("DC_OAUTH_TOKEN", "")
        self.tenant_id = os.getenv("DC_TENANT_ID", "")
        self._connected = bool(self.base_url and self.api_key)
        if self._connected:
            self._adapter_mode = "live"

    def _dc_headers(self, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "X-Tenant-Id":   self.tenant_id,
        }
        if idempotency_key:
            h["X-Idempotency-Key"] = idempotency_key
        return h

    def health(self) -> Dict[str, Any]:
        return {
            "adapter":         self.name,
            "status":          "connected" if self._connected else "fallback_inmemory",
            "payment_mode":    self._adapter_mode,
            "policies_seeded": len(CANONICAL_POLICIES),
            "claims_count":    len(self._claims),
            "payments_count":  len(self._payments),
        }

    def create_claim(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a claim record — shell or live DC path.

        Shell: delegates to InMemoryFallbackMixin (in-memory store) after
        building the DC-shaped body and storing it alongside the canonical payload.
        Live:  POST {DC_BASE_URL}/api/v1/claims with _to_dc_claim_body(payload).
        """
        # Build DC-mapped body (stored for audit; used in live call)
        dc_body = _to_dc_claim_body(payload)

        if not self._connected:
            record = super().create_claim(payload)
            record["dc_claim_body"] = dc_body   # attach for introspection / testing
            return record

        try:
            import httpx
            r = httpx.post(
                f"{self.base_url}/api/v1/claims",
                json=dc_body,
                headers=self._dc_headers(),
                timeout=15.0,
            )
            r.raise_for_status()
            dc_resp = r.json()
            cid = (payload.get("claim_id")
                   or str(dc_resp.get("ClaimNumber") or dc_resp.get("id") or ""))
            record = {**payload, "claim_id": cid,
                      "sor_system": self.name,
                      "sor_record_id": str(dc_resp.get("ClaimId") or cid),
                      "dc_claim_body": dc_body}
            with self._lock:
                self._claims[cid] = record
            return record
        except Exception as exc:
            import logging
            logging.getLogger("fnol.sor.dc").error(
                "DC create_claim failed: %s — shell fallback", exc)
            self._adapter_mode = "shell"
            record = super().create_claim(payload)
            record["dc_claim_body"] = dc_body
            return record

    def update_claim(self, claim_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Update claim — shell or live DC PATCH path."""
        if not self._connected:
            return super().update_claim(claim_id, patch)

        try:
            import httpx
            full_payload = {**(self._claims.get(claim_id) or {}), **patch}
            dc_body = _to_dc_claim_body(full_payload)
            r = httpx.patch(
                f"{self.base_url}/api/v1/claims/{claim_id}",
                json=dc_body,
                headers=self._dc_headers(),
                timeout=15.0,
            )
            r.raise_for_status()
            with self._lock:
                current = self._claims.get(claim_id) or {}
                merged = {**current, **patch, "claim_id": claim_id,
                          "dc_claim_body": dc_body}
                self._claims[claim_id] = merged
            return merged
        except Exception as exc:
            import logging
            logging.getLogger("fnol.sor.dc").error(
                "DC update_claim failed for %s: %s — shell fallback", claim_id, exc)
            return super().update_claim(claim_id, patch)

    def authorize_payment(self, req: PaymentRequest) -> PaymentResponse:
        """Authorize a payment.

        Shell mode: delegates to InMemoryFallbackMixin.authorize_payment()
        Live mode:  POST {DC_BASE_URL}/api/v1/claims/{claimId}/payments/authorize
        """
        if not self._connected:
            return super().authorize_payment(req)

        # Live DC call
        try:
            import httpx
            body = {
                "PaymentType":       req.payment_type,
                "PaymentMethod":     req.payment_method,
                "PaymentAmount":     {"Value": req.amount_usd, "Currency": "USD"},
                "PayeeName":         req.payee_name,
                "Memo":              req.memo,
                "AuthorityTier":     req.authority_tier,
                "CoveragePart":      req.coverage_part or "",
                "DeductibleApplied": req.deductible_applied,
                "ReleaseObtained":   req.release_obtained,
            }
            if req.payee_account:
                body["BankAccountRef"] = req.payee_account
            if req.payee_routing:
                body["BankRoutingRef"] = req.payee_routing
            if req.payee_address:
                body["PayeeAddress"] = req.payee_address

            url = f"{self.base_url}/api/v1/claims/{req.claim_id}/payments/authorize"
            r = httpx.post(
                url, json=body,
                headers=self._dc_headers(req.idempotency_key),
                timeout=10.0,
            )
            r.raise_for_status()
            payload = r.json()
            pid = f"PAY-{uuid.uuid4().hex[:10].upper()}"
            resp = PaymentResponse(
                payment_id=pid,
                sor_payment_id=str(payload.get("PaymentId") or payload.get("id") or ""),
                claim_id=req.claim_id,
                status=_map_dc_payment_status(str(payload.get("Status") or "AUTHORIZED")),
                payment_type=req.payment_type,
                payment_method=req.payment_method,
                amount_usd=req.amount_usd,
                payee_name=req.payee_name,
                authority_tier=req.authority_tier,
                authorized_at=_now_utc_iso(),
                expected_settle_date=_settle_date(req.payment_method),
                sor_transaction_ref=str(payload.get("TransactionRef") or ""),
                adapter_mode="live",
            )
            with self._lock:
                self._payments[pid] = resp
            return resp
        except Exception as exc:
            import logging
            logging.getLogger("fnol.sor.dc").error(
                "DC authorize_payment failed for claim %s: %s — shell fallback", req.claim_id, exc
            )
            self._adapter_mode = "shell"
            return super().authorize_payment(req)

    def disburse_payment(self, payment_id: str) -> PaymentResponse:
        resp = self._payments.get(payment_id)
        if resp is None:
            raise KeyError(f"Payment {payment_id} not found")

        if not self._connected or resp.adapter_mode != "live":
            return super().disburse_payment(payment_id)

        try:
            import httpx
            url = f"{self.base_url}/api/v1/claims/{resp.claim_id}/payments/{resp.sor_payment_id}/disburse"
            r = httpx.post(url, headers=self._dc_headers(), timeout=10.0)
            r.raise_for_status()
            payload = r.json()
            with self._lock:
                resp.status = "DISBURSED"
                resp.disbursed_at = payload.get("DisbursedAt") or _now_utc_iso()
                if resp.claim_id in self._claims:
                    self._claims[resp.claim_id]["payment_status"] = "DISBURSED"
                    self._claims[resp.claim_id]["disbursed_at"] = resp.disbursed_at
            return resp
        except Exception as exc:
            import logging
            logging.getLogger("fnol.sor.dc").error("DC disburse failed for %s: %s — mock fallback", payment_id, exc)
            return super().disburse_payment(payment_id)

    def get_payment_status(self, payment_id: str) -> PaymentStatusResponse:
        resp = self._payments.get(payment_id)
        if resp is None:
            raise KeyError(f"Payment {payment_id} not found")

        if not self._connected or resp.adapter_mode != "live":
            return super().get_payment_status(payment_id)

        try:
            import httpx
            url = f"{self.base_url}/api/v1/claims/{resp.claim_id}/payments/{resp.sor_payment_id}"
            r = httpx.get(url, headers=self._dc_headers(), timeout=10.0)
            r.raise_for_status()
            payload = r.json()
            return PaymentStatusResponse(
                payment_id=payment_id,
                sor_payment_id=resp.sor_payment_id,
                claim_id=resp.claim_id,
                status=_map_dc_payment_status(str(payload.get("Status") or "")),
                amount_usd=resp.amount_usd,
                payment_method=resp.payment_method,
                authorized_at=resp.authorized_at,
                disbursed_at=payload.get("DisbursedAt"),
                cleared_at=payload.get("ClearedAt"),
                adapter_mode="live",
            )
        except Exception:
            return super().get_payment_status(payment_id)

    def void_payment(self, payment_id: str, reason: str) -> PaymentResponse:
        resp = self._payments.get(payment_id)
        if resp is None:
            raise KeyError(f"Payment {payment_id} not found")

        if not self._connected or resp.adapter_mode != "live":
            return super().void_payment(payment_id, reason)

        try:
            import httpx
            url = f"{self.base_url}/api/v1/claims/{resp.claim_id}/payments/{resp.sor_payment_id}"
            r = httpx.delete(url, headers=self._dc_headers(), params={"reason": reason}, timeout=10.0)
            r.raise_for_status()
            with self._lock:
                resp.status = "VOIDED"
                resp.failure_reason = reason
            return resp
        except Exception:
            return super().void_payment(payment_id, reason)


def _map_dc_payment_status(dc_status: str) -> str:
    """Normalize Duck Creek payment status codes to platform canonical values."""
    return {
        "Authorized":   "AUTHORIZED",
        "Pending":      "PENDING_DISBURSE",
        "Disbursed":    "DISBURSED",
        "Cleared":      "DISBURSED",
        "Voided":       "VOIDED",
        "Failed":       "FAILED",
        "OnHold":       "ON_HOLD",
    }.get(dc_status, dc_status.upper() if dc_status else "UNKNOWN")


# ───────────────────────────────────────────────────────────────────────────
# Guidewire adapter (SECONDARY SOR) — ClaimCenter REST stub
# ───────────────────────────────────────────────────────────────────────────

class GuidewireAdapter(InMemoryFallbackMixin, BaseSORAdapter):
    """Guidewire ClaimCenter REST adapter (stub).

    Endpoints expected (configured via env):
      GW_BASE_URL, GW_API_USER, GW_API_PASS

    The InMemoryFallbackMixin keeps CRUD + payment working until live endpoints land.
    """
    name = "guidewire"
    _adapter_mode = "shell"

    def __init__(self) -> None:
        self._init_inmemory()
        self.base_url = os.getenv("GW_BASE_URL", "").rstrip("/")
        self.api_user = os.getenv("GW_API_USER")
        self.api_pass = os.getenv("GW_API_PASS")
        self._connected = bool(self.base_url and self.api_user and self.api_pass)
        if self._connected:
            self._adapter_mode = "live"

    def health(self) -> Dict[str, Any]:
        return {"adapter": self.name,
                "status": "connected" if self._connected else "fallback_inmemory",
                "payment_mode": self._adapter_mode,
                "policies_seeded": len(CANONICAL_POLICIES),
                "claims_count": len(self._claims),
                "payments_count": len(self._payments)}

    def create_claim(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a claim in Guidewire ClaimCenter.

        Shell: delegates to InMemoryFallbackMixin after building GW-shaped body.
        Live:  POST {GW_BASE_URL}/cc/v1/claims with _to_gw_claim_body(payload).
        """
        gw_body = _to_gw_claim_body(payload)

        if not self._connected:
            record = super().create_claim(payload)
            record["gw_claim_body"] = gw_body
            return record

        try:
            import httpx, base64
            creds = base64.b64encode(
                f"{self.api_user}:{self.api_pass}".encode()).decode()
            headers = {
                "Authorization": f"Basic {creds}",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            }
            r = httpx.post(
                f"{self.base_url}/cc/v1/claims",
                json={"data": {"attributes": gw_body, "type": "Claim"}},
                headers=headers,
                timeout=15.0,
            )
            r.raise_for_status()
            resp = r.json().get("data", {})
            cid = (payload.get("claim_id")
                   or str(resp.get("id") or resp.get("attributes", {}).get("claimNumber") or ""))
            record = {**payload, "claim_id": cid,
                      "sor_system": self.name,
                      "sor_record_id": str(resp.get("id") or cid),
                      "gw_claim_body": gw_body}
            with self._lock:
                self._claims[cid] = record
            return record
        except Exception as exc:
            import logging
            logging.getLogger("fnol.sor.gw").error(
                "GW create_claim failed: %s — shell fallback", exc)
            self._adapter_mode = "shell"
            record = super().create_claim(payload)
            record["gw_claim_body"] = gw_body
            return record

    def update_claim(self, claim_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Update claim in GW ClaimCenter — shell or live PATCH path."""
        if not self._connected:
            return super().update_claim(claim_id, patch)

        try:
            import httpx, base64
            full_payload = {**(self._claims.get(claim_id) or {}), **patch}
            gw_body = _to_gw_claim_body(full_payload)
            creds = base64.b64encode(
                f"{self.api_user}:{self.api_pass}".encode()).decode()
            headers = {
                "Authorization": f"Basic {creds}",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            }
            r = httpx.patch(
                f"{self.base_url}/cc/v1/claims/{claim_id}",
                json={"data": {"attributes": gw_body, "type": "Claim"}},
                headers=headers,
                timeout=15.0,
            )
            r.raise_for_status()
            with self._lock:
                current = self._claims.get(claim_id) or {}
                merged = {**current, **patch, "claim_id": claim_id,
                          "gw_claim_body": gw_body}
                self._claims[claim_id] = merged
            return merged
        except Exception as exc:
            import logging
            logging.getLogger("fnol.sor.gw").error(
                "GW update_claim failed for %s: %s — shell fallback", claim_id, exc)
            return super().update_claim(claim_id, patch)


# ───────────────────────────────────────────────────────────────────────────
# Factory
# ───────────────────────────────────────────────────────────────────────────

_SINGLETON: Optional[SORAdapter] = None
_SINGLETON_LOCK = threading.Lock()

def get_sor_adapter() -> SORAdapter:
    """Return the process-wide SOR adapter, instantiating it on first call.

    Always acquires the lock — the GIL protects ref-stores today but PEP 703
    free-threading does not, and the cost of one uncontended lock acquire on
    a hot path is negligible compared to the potential for seeing a partially
    constructed adapter under future free-threaded Python.
    """
    global _SINGLETON
    from fnol_settings import settings
    with _SINGLETON_LOCK:
        if _SINGLETON is not None:
            return _SINGLETON
        kind = (settings.sor_type or "mock").lower().strip()
        if kind == "duckcreek":
            _SINGLETON = DuckCreekAdapter()
        elif kind == "guidewire":
            _SINGLETON = GuidewireAdapter()
        else:
            _SINGLETON = MockAdapter()
        return _SINGLETON


if __name__ == "__main__":
    import json as _j
    sor = get_sor_adapter()
    print(_j.dumps(sor.health(), indent=2))
    print(_j.dumps(sor.lookup_policy("POC-POL-00123"), indent=2, default=str))
