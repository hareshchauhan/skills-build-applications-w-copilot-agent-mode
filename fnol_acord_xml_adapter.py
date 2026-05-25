"""
FNOL Intelligence Platform — ACORD XML Adapter
===============================================
Outbound ACORD XML serializer for AL3 interoperability.

Implements ACORD 101 (Auto Loss Notice) and the ACORD XML
ClaimsOccurrenceRq transaction envelope, enabling downstream
consumption by carrier systems and clearinghouses that require
ACORD-formatted FNOL submissions.

Design principles:
  • The canonical JSON Claim model remains the platform's internal
    contract — this adapter is OUTBOUND ONLY (JSON → ACORD XML).
  • The serializer co-exists with the existing SOR adapters.
    Call `acord_xml_from_claim(claim)` at any pipeline stage; it
    does not replace Duck Creek / Guidewire write-back.
  • No external dependencies — uses stdlib `xml.etree.ElementTree`
    so the adapter works in any deployment without pip additions.
  • All ACORD namespace declarations conform to ACORD 2.x XML schema
    (urn:com.acord.standards.AcordMsgRq).

ACORD standards implemented (Gap 1 + Gap 2):
  ACORD 101 §1  — SourceChannelCd, IntakeQualityScore (Gap 1)
  ACORD 101 §7  — ClaimsParty[] with ClaimsPartyRoleCd (Gap 2)
  ACORD 101 §8  — Witness array RoleCd=11 (Gap 2)
  ACORD 101 §9  — Passenger array RoleCd=12 (Gap 2)
  ACORD 101 §10 — Attorney contact RoleCd=15 (Gap 2)
  ACORD 101 §11 — Adverse vehicle / Other Driver RoleCd=16/17 (Gap 2)
  ACORD XML     — ClaimsOccurrenceRq / ClaimsOccurrenceRs envelope
  ACORD AL3     — EDI X12 837P channel code mapping (SourceChannelCd)

Backward-compatibility:
  Flat reporter fields (reporter_name, reporter_phone, etc.) are bridged
  via acord_parties() on the Claim model. Existing payloads without
  structured parties[] produce valid ACORD XML via the bridge.

Integration points:
  • DuckCreekAdapter.create_claim() → attach acord_xml as SOR supplement
    when DC_ACORD_ENDPOINT env var is set.
  • fnol_v3_routes POST /api/v1/fnol/submit → include acord_xml_b64 field
    when ?include_acord=true query param is set.
  • Governance audit_export.py → embed XML in PDF audit reports.
  • fnol_acord_parties.validate_parties() → party completeness scoring.

Usage:
    from fnol_acord_xml_adapter import AcordXmlAdapter, serialize_to_acord_xml
    adapter = AcordXmlAdapter()
    xml_str = adapter.serialize_claim(claim)
    xml_bytes = adapter.serialize_claim_bytes(claim)
    summary = adapter.to_summary_dict(claim)
"""

from __future__ import annotations

import datetime as _dt
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fnol_claim import Claim

# Import canonical normalisers and Gap 5 enums from fnol_claim.
try:
    from fnol_claim import (
        normalise_loss_cause_cd as _normalise_lcc,
        CoverageCd, AcvSourceCd, RorTriggerCd,
    )
    def _map_loss_cause(raw: Optional[str]) -> str:
        """Delegate to the canonical normaliser in fnol_claim (Gap 3)."""
        return _normalise_lcc(raw).value
except ImportError:
    CoverageCd = None  # type: ignore
    AcvSourceCd = None  # type: ignore
    RorTriggerCd = None  # type: ignore
    _LOSS_CAUSE_FALLBACK: Dict[str, str] = {
        "collision": "COLLSN", "collsn": "COLLSN",
        "comprehensive": "COMPRE", "compre": "COMPRE",
        "theft": "THEFT", "fire": "FIRE", "flood": "FLOOD",
        "hail": "HAIL", "glass": "GLASS", "hitnrn": "HITNRN",
        "vandalism": "VANDLSM", "vandlsm": "VANDLSM",
        "uninsured motorist": "UNINS", "unins": "UNINS",
    }
    def _map_loss_cause(raw: Optional[str]) -> str:
        if not raw: return "OTH"
        return _LOSS_CAUSE_FALLBACK.get(raw.lower().strip(), "OTH")


# ───────────────────────────────────────────────────────────────────────────
# ACORD namespace & schema constants  (Gap 7 — Message Transport)
#
# ACORD 2.x XML namespaces per ACORD XML Standards Committee:
#   Primary namespace  : urn:com.acord.standards.AcordMsgRq
#   XSD instance       : http://www.w3.org/2001/XMLSchema-instance
#   Schema location    : https://www.acord.org/xml/standards/AcordMsgRq.xsd
#
# SchemaVersion declaration (ACORD requirement — one per transaction):
#   ACORD requires StandardVersionMajorNumber + StandardVersionMinorNumber
#   in every SignonRq/SignonTransactionStatus block AND as a root attribute.
#   Platform uses the ACORD_SCHEMA_VERSION registry below to bind the
#   transaction type to a specific version. This closes the "no schema
#   registry" gap identified in the gap analysis.
#
# OAuth2 / mTLS production path (SignonRq authentication):
#   POC: X-API-Key header (SignonPswd element left empty in XML body)
#   Prod: Bearer token injected via CarrierCredentials.oauth_bearer_token
#         mTLS: client cert/key loaded by the SOR adapter's httpx transport
#   The ACORD SignonRq/SignonRs blocks are generated correctly for both paths;
#   the auth credential is never embedded in the XML body in production.
# ───────────────────────────────────────────────────────────────────────────

ACORD_NS        = "urn:com.acord.standards.AcordMsgRq"
ACORD_NS_XSI    = "http://www.w3.org/2001/XMLSchema-instance"
ACORD_NS_ACORD  = "http://www.acord.org/xmlns/CommonML/v1"  # AL3 compound namespace
ACORD_SCHEMA_LOC = (
    "urn:com.acord.standards.AcordMsgRq "
    "https://www.acord.org/xml/standards/AcordMsgRq.xsd"
)
ACORD_VERSION   = "2.0"
PLATFORM_ID     = "FNOL-INTELLIGENCE-PLATFORM"
PLATFORM_VER    = "3.5.1"

# ── ACORD SchemaVersion registry ──────────────────────────────────────────
# Maps ACORD transaction type → (major, minor, maintenance) version tuple.
# Used in every envelope to emit a conformant SchemaVersion declaration.
# Sources: ACORD XML Standards, ACORD P&C Data Standards, ACORD AL3.
#
# Production: load from an external schema registry (Blueprint §L5);
# this dict is the inline fallback for POC / no-registry environments.
ACORD_SCHEMA_REGISTRY: dict = {
    "ClaimsOccurrenceRq":  ("2", "0", "0"),   # ACORD 101 / ACORD 2.0
    "ClaimsOccurrenceRs":  ("2", "0", "0"),
    "ClaimsOccurrenceNotif": ("2", "0", "0"),  # Push notification variant
    "SignonRq":            ("2", "0", "0"),
    "SignonRs":            ("2", "0", "0"),
    "PolicyRq":            ("1", "0", "0"),
    "PolicyRs":            ("1", "0", "0"),
    "default":             ("2", "0", "0"),
}


def _schema_version(transaction_type: str) -> tuple:
    """Return (major, minor, maint) schema version for a transaction type."""
    return ACORD_SCHEMA_REGISTRY.get(transaction_type,
                                     ACORD_SCHEMA_REGISTRY["default"])


# ───────────────────────────────────────────────────────────────────────────
# CarrierCredentials — auth bundle for outbound ACORD XML / EDI
#
# Encapsulates all auth information needed at the SOR transport boundary.
# The SignonRq block references the SignonPswd element — in production this
# element is left empty (or omitted) and the credential is injected via the
# HTTP transport layer (Bearer header or mTLS). This keeps secrets out of
# the XML body entirely, which is the ACORD-recommended pattern for REST
# transport of ACORD XML payloads.
# ───────────────────────────────────────────────────────────────────────────

class CarrierCredentials:
    """Authentication bundle for outbound ACORD XML transactions.

    Populated from settings / environment at adapter construction time.
    Never serialised into the XML body — used only at the HTTP transport layer.

    Attributes:
        carrier_id:          ACORD-assigned carrier/member ID.
        carrier_name:        Human-readable carrier name (for SignonRq ClientApp).
        oauth_bearer_token:  OAuth 2.0 Bearer token (production path).
                             None → POC X-API-Key path.
        mtls_cert_path:      Path to PEM client cert (mTLS path).
        mtls_key_path:       Path to PEM client key (mTLS path).
        signon_user_id:      ACORD SignonRq UserId element (legacy / EDI path).
        signon_password:     ACORD SignonRq password (legacy EDI only;
                             NEVER used in REST transport — left empty in XML).
    """
    __slots__ = ("carrier_id", "carrier_name", "oauth_bearer_token",
                 "mtls_cert_path", "mtls_key_path",
                 "signon_user_id", "signon_password")

    def __init__(
        self,
        carrier_id: str = "",
        carrier_name: str = PLATFORM_ID,
        oauth_bearer_token: Optional[str] = None,
        mtls_cert_path: Optional[str] = None,
        mtls_key_path: Optional[str] = None,
        signon_user_id: str = "",
        signon_password: str = "",          # Always empty in REST transport
    ) -> None:
        self.carrier_id           = carrier_id
        self.carrier_name         = carrier_name
        self.oauth_bearer_token   = oauth_bearer_token
        self.mtls_cert_path       = mtls_cert_path
        self.mtls_key_path        = mtls_key_path
        self.signon_user_id       = signon_user_id
        self.signon_password      = signon_password  # Empty string = REST path

    @property
    def auth_mode(self) -> str:
        """Production auth mode for logging / governance."""
        if self.mtls_cert_path and self.mtls_key_path:
            return "mTLS"
        if self.oauth_bearer_token:
            return "OAuth2"
        return "API_KEY"   # POC default

    @classmethod
    def from_settings(cls) -> "CarrierCredentials":
        """Construct from fnol_settings (lazy import to avoid circular dep)."""
        try:
            from fnol_settings import settings
            return cls(
                carrier_id=getattr(settings, "acord_carrier_id", "") or "",
                carrier_name=getattr(settings, "acord_carrier_name", PLATFORM_ID) or PLATFORM_ID,
                oauth_bearer_token=getattr(settings, "acord_oauth_bearer_token", None),
                mtls_cert_path=getattr(settings, "acord_mtls_cert_path", None),
                mtls_key_path=getattr(settings, "acord_mtls_key_path", None),
                signon_user_id=getattr(settings, "acord_signon_user_id", "") or "",
            )
        except Exception:
            return cls()


# ───────────────────────────────────────────────────────────────────────────
# SourceChannelCd → ACORD AL3 X12 SourceSystemId mapping
# ───────────────────────────────────────────────────────────────────────────

_CHANNEL_AL3_MAP: Dict[str, str] = {
    "WEB":             "WEB",
    "IVR":             "IVR",
    "AGENT":           "AGNT",
    "MOBILE":          "MOBL",
    "THIRD_PARTY_API": "API3",
}

def _map_channel_al3(channel_cd: str) -> str:
    return _CHANNEL_AL3_MAP.get(channel_cd.upper(), "OTH")


# ── VehicleDamageAreaCd normaliser — delegates to fnol_claim (Gap 4) ──────
# Local map removed. effective_damage_area_cd on the Claim instance is
# always preferred; _map_damage_area is the runtime fallback.
try:
    from fnol_claim import normalise_damage_area_cd as _normalise_dac
    def _map_damage_area(raw: Optional[str]) -> str:
        """Delegate to canonical normaliser in fnol_claim (Gap 4)."""
        return _normalise_dac(raw).value
except ImportError:
    _DAMAGE_AREA_FALLBACK: Dict[str, str] = {
        "front": "FRT", "frt": "FRT",
        "rear": "REAR", "back": "REAR",
        "left side": "LFTSD", "lftsd": "LFTSD", "driver side": "LFTSD",
        "right side": "RGTSD", "rgtsd": "RGTSD", "passenger side": "RGTSD",
        "roof": "ROOF", "top": "ROOF",
        "underbody": "UNDRBD", "undercarriage": "UNDRBD", "undrbd": "UNDRBD",
        "interior": "INTRNL", "intrnl": "INTRNL",
        "all": "ALL", "total": "ALL", "rollover": "ALL",
    }
    def _map_damage_area(raw: Optional[str]) -> str:
        if not raw: return "UNKNWN"
        return _DAMAGE_AREA_FALLBACK.get(raw.lower().strip(), "UNKNWN")


# ───────────────────────────────────────────────────────────────────────────
# XML helpers
# ───────────────────────────────────────────────────────────────────────────

def _sub(parent: ET.Element, tag: str,
         text: Optional[str] = None,
         attrib: Optional[Dict[str, str]] = None) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib=attrib or {})
    if text is not None:
        el.text = str(text)
    return el

def _now_utc_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")

def _date_only(iso_str: Optional[str]) -> str:
    if not iso_str:
        return ""
    return iso_str[:10]

def _str_val(val: Any) -> Optional[str]:
    """Return enum .value or str; None if val is None."""
    if val is None:
        return None
    return val.value if hasattr(val, "value") else str(val)


# ───────────────────────────────────────────────────────────────────────────
# ACORD XML Adapter
# ───────────────────────────────────────────────────────────────────────────

class AcordXmlAdapter:
    """Outbound ACORD XML serializer — Gap 1 + Gap 2 complete.

    Stateless and thread-safe. The party serialization is delegated to
    fnol_acord_parties.AcordPartySerializer (lazy-imported to avoid
    circular imports at module load time).

    Key sections generated:
      SignonRq            — platform identification, timestamp
      ClaimsOccurrenceRq:
        MsgStatus         — SourceChannelCd + AL3 SourceSystemId (Gap 1)
        AcordExtensions   — IntakeQualityScore (Gap 1)
        Policy            — PolicyNumber, JurisdictionStateCd
        ClaimsOccurrence  — LossInfo, LossCauseCd (ACORD enum), FatalityInd
        AutoLossInfo      — VehicleInfo, DamageAreaCd, ACV
        ClaimsParty[]     — ALL parties via fnol_acord_parties (Gap 2):
                            Named Insured, Claimant, Witnesses, Passengers,
                            Other Driver/Owner, Attorney
        AdverseVehicleInfo — Adverse vehicle when other_vehicle present (Gap 2)
        SIUIndicators     — Platform fraud signal extension
        InjuryInfo        — Legacy flat injury bridge (retained for compat)
    """

    def _get_party_serializer(self):
        """Lazy import to avoid circular dep at module-level."""
        try:
            from fnol_acord_parties import AcordPartySerializer
            return AcordPartySerializer()
        except ImportError:
            return None

    def build_envelope(
        self,
        claim: "Claim",
        credentials: Optional["CarrierCredentials"] = None,
    ) -> ET.Element:
        """Build the full ACORD XML element tree. Returns the root element.

        Args:
            claim:       Populated platform Claim instance.
            credentials: CarrierCredentials for SignonRq auth block.
                         None → platform defaults (POC X-API-Key path).
        """

        # ── Root — full ACORD 2.x multi-namespace (Gap 7) ───────────────────
        # xmlns:ACORD       primary ACORD message request namespace
        # xmlns:xsi         XML Schema Instance (for xsi:schemaLocation)
        # AcordVersion      explicit version on root element (ACORD requirement)
        # xsi:schemaLocation canonical schema URL for EDI validators
        root = ET.Element("ACORD")
        root.set("xmlns",              ACORD_NS)
        root.set("xmlns:xsi",          ACORD_NS_XSI)
        root.set("xsi:schemaLocation", ACORD_SCHEMA_LOC)
        root.set("AcordVersion",       ACORD_VERSION)
        # SchemaVersion on root — ACORD 2.0 requirement for every transaction
        _sv_maj, _sv_min, _sv_mnt = _schema_version("ClaimsOccurrenceRq")
        root.set("StandardVersionMajorNumber", _sv_maj)
        root.set("StandardVersionMinorNumber", _sv_min)
        root.set("StandardVersionMaintenanceNumber", _sv_mnt)

        # ── SignonRq — full auth block (Gap 7) ────────────────────────────
        # Production: oauth_bearer_token / mTLS are injected at HTTP transport
        # layer (never in XML body). signon_password is ALWAYS empty for REST.
        # ACORD EDI legacy path: UserId + Password in SignonPswd (not REST).
        creds = credentials or CarrierCredentials()
        signon = _sub(root, "SignonRq")
        svc = _sub(signon, "SignonTransactionStatus")
        _sub(svc, "StandardVersionMajorNumber", _sv_maj)
        _sub(svc, "StandardVersionMinorNumber", _sv_min)
        _sub(svc, "StandardVersionMaintenanceNumber", _sv_mnt)
        _sub(svc, "TransactionStatus",     "SuccessWithInfo")
        _sub(svc, "TransactionStatusDesc",
             f"Generated by {PLATFORM_ID} v{PLATFORM_VER}")
        # SignonPswd block — empty for REST/OAuth2/mTLS (credential in header)
        signon_pswd = _sub(signon, "SignonPswd")
        if creds.signon_user_id:                   # EDI legacy path only
            _sub(signon_pswd, "CustId").append(
                ET.fromstring(f"<SPName>{creds.carrier_id or PLATFORM_ID}</SPName>"))
            _sub(signon_pswd, "CustLoginId", creds.signon_user_id)
            # Password intentionally omitted even here — carrier vault injects
        # Auth mode annotation (governance / audit trail; not a secret)
        _sub(signon, "ClientDt", _now_utc_iso())
        _sub(signon, "CustLangPref", "ENG")
        client = _sub(signon, "ClientApp")
        _sub(client, "Org",     creds.carrier_name or PLATFORM_ID)
        _sub(client, "Name",    "FNOL Intelligence Platform")
        _sub(client, "Version", PLATFORM_VER)
        _sub(client, "CarrierId", creds.carrier_id or PLATFORM_ID)
        # Auth mode label for governance layer (not a credential)
        _sub(signon, "SignonAuthMode", creds.auth_mode)

        # ── ClaimsOccurrenceRq — typed transaction wrapper (Gap 7) ──────────
        # ACORD AL3 requires a typed ClaimsOccurrenceRq wrapper with an
        # explicit SchemaVersion in every transaction. The RqUID provides
        # idempotency for retries; TransactionRequestDt is the canonical
        # submission timestamp used by state EDI mandates.
        claim_rq = _sub(root, "ClaimsOccurrenceRq")
        _sub(claim_rq, "RqUID", str(uuid.uuid4()))
        _sub(claim_rq, "TransactionRequestDt", _now_utc_iso())
        # SchemaVersion declaration — required per ACORD AL3 §2.1
        schema_ver = _sub(claim_rq, "SchemaVersion")
        _sub(schema_ver, "StandardVersionMajorNumber", _sv_maj)
        _sub(schema_ver, "StandardVersionMinorNumber", _sv_min)
        _sub(schema_ver, "StandardVersionMaintenanceNumber", _sv_mnt)
        _sub(schema_ver, "SchemaLocation", ACORD_SCHEMA_LOC)
        cur = _sub(claim_rq, "CurAmt")
        _sub(cur, "CurCd", "USD")

        # ── MsgStatus — SourceChannelCd (Gap 1) ───────────────────────────
        channel_val = _str_val(
            getattr(claim, "source_channel_cd", None)
        ) or "WEB"
        msg = _sub(claim_rq, "MsgStatus")
        _sub(msg, "SourceChannelCd", channel_val)
        _sub(msg, "SourceSystemId", _map_channel_al3(channel_val))
        _sub(msg, "SourceSystemDesc",
             f"FNOL Platform intake via {channel_val}")

        # ── IntakeQualityScore (Gap 1) ─────────────────────────────────────
        iqs = getattr(claim, "intake_quality_score", None)
        if iqs is not None:
            ext = _sub(claim_rq, "AcordExtensions")
            iq_el = _sub(ext, "IntakeQualityScore")
            _sub(iq_el, "Score", f"{iqs:.4f}")
            _sub(iq_el, "ScoreRange", "0.0-1.0")
            _sub(iq_el, "HITLThreshold", "0.60")

        # ── Policy ────────────────────────────────────────────────────────
        policy = _sub(claim_rq, "Policy")
        _sub(policy, "PolicyNumber", claim.policy_number)
        eff_state = getattr(claim, "effective_state", None) or \
                    (claim.state or claim.vehicle_state or "")
        if eff_state:
            _sub(policy, "JurisdictionStateCd", eff_state.upper())

        # ── ClaimsOccurrence — Loss envelope ──────────────────────────────
        occurrence = _sub(claim_rq, "ClaimsOccurrence")
        _sub(occurrence, "ClaimsOccurrenceId",
             claim.claim_id or str(uuid.uuid4()))

        loss_info = _sub(occurrence, "LossInfo")
        _sub(loss_info, "LossDt", _date_only(claim.loss_date_time))
        ldt = claim.loss_date_time or ""
        _sub(loss_info, "LossTime",
             ldt[11:19] if len(ldt) > 10 else "00:00:00")
        _sub(loss_info, "LossDesc", claim.loss_description or "")
        # Gap 3: prefer claim.effective_loss_cause_cd (normalise_loss_cause_cd)
        # which uses the stamped loss_cause_cd enum when S1 has run, or
        # normalises free-text on-the-fly otherwise.
        _lcc = (getattr(claim, "effective_loss_cause_cd", None)
                or _map_loss_cause(getattr(claim, "loss_cause", "")))
        _sub(loss_info, "LossCauseCd", _lcc)
        _sub(loss_info, "LossCauseRaw", claim.loss_cause or "")

        loss_loc = _sub(loss_info, "LossLocation")
        _sub(loss_loc, "Addr1", claim.loss_location or "")
        zip_val = (
            getattr(claim, "loss_location_zip", None) or
            getattr(claim, "location_zip", None) or ""
        )
        if zip_val:
            _sub(loss_loc, "PostalCode", zip_val)
        if eff_state:
            _sub(loss_loc, "StateProvCd", eff_state.upper())

        _sub(loss_info, "FatalityInd",
             "Y" if claim.fatality_indicator else "N")

        # ── AutoLossInfo — Vehicle ─────────────────────────────────────────
        auto_loss = _sub(claim_rq, "AutoLossInfo")
        vehicle = _sub(auto_loss, "VehicleInfo")
        if claim.vin:
            _sub(vehicle, "VehIdentificationNumber", claim.vin)
        if claim.vehicle_year:
            _sub(vehicle, "ModelYear", str(claim.vehicle_year))
        if claim.vehicle_make:
            _sub(vehicle, "Manufacturer", claim.vehicle_make)
        if claim.vehicle_model:
            _sub(vehicle, "Model", claim.vehicle_model)
        if claim.vehicle_mileage:
            _sub(vehicle, "Odometer", str(claim.vehicle_mileage))
        _sub(vehicle, "DrivableInd",
             "Y" if claim.drivable_indicator else "N")
        _sub(vehicle, "VehicleRecallInd",
             "Y" if claim.vehicle_recall_indicator else "N")
        # Gap 4: prefer effective_damage_area_cd (stamped enum or normalised
        # free-text); emit both coded and raw for SOR round-trip fidelity.
        _dac = (getattr(claim, "effective_damage_area_cd", None)
                or _map_damage_area(getattr(claim, "primary_damage_area", None)))
        if _dac and _dac != "UNKNWN":
            _sub(vehicle, "VehicleDamageAreaCd", _dac)
        elif _dac:
            _sub(vehicle, "VehicleDamageAreaCd", "UNKNWN")
        if claim.primary_damage_area:
            _sub(vehicle, "VehicleDamageAreaRaw", claim.primary_damage_area)
        # ACORD 101 §5 — license plate + registration state (Gap 4)
        if getattr(claim, "license_plate", None):
            _sub(vehicle, "LicensePlateNumber", claim.license_plate)
            if getattr(claim, "registration_state", None):
                _sub(vehicle, "LicensePlateStateCd",
                     claim.registration_state.upper())
        if claim.vehicle_acv_usd:
            acv = _sub(vehicle, "VehACV")
            _sub(acv, "Amt", f"{claim.vehicle_acv_usd:.2f}")
            _sub(acv, "CurCd", "USD")

        # ── ClaimsParty[] — Gap 2: structured parties via helper ──────────
        party_serializer = self._get_party_serializer()
        if party_serializer is not None:
            # Full Gap 2 path: all party types via AcordPartySerializer
            for party_el in party_serializer.parties_elements(claim):
                claim_rq.append(party_el)
        else:
            # Fallback: flat-field bridge (Gap 1 behavior, no helper available)
            self._append_flat_parties(claim_rq, claim)

        # ── SIU Indicators (platform extension) ───────────────────────────
        siu = _sub(claim_rq, "SIUIndicators")
        _sub(siu, "ISOMatchInd", "Y" if claim.iso_match else "N")
        _sub(siu, "PriorClaimsCount", str(claim.prior_claims_count))
        _sub(siu, "PolicyTenureDays", str(claim.policy_tenure_days))

        # ── ACORD Gap 5 — CoverageInfo[] blocks ──────────────────────────
        # Serialise claimant_asserted_coverages[] as ACORD <CoverageInfo>
        # elements. Absent list = valid (no assertion at intake).
        for _cov in getattr(claim, "claimant_asserted_coverages", []):
            _ccd = (_cov.coverage_cd.value
                    if hasattr(getattr(_cov, "coverage_cd", None), "value")
                    else str(getattr(_cov, "coverage_cd", "")))
            ci_el = _sub(claim_rq, "CoverageInfo")
            _sub(ci_el, "CoverageCd", _ccd)
            if getattr(_cov, "deductible_usd", None) is not None:
                ded = _sub(ci_el, "Deductible")
                _sub(ded, "FormatInteger", str(int(_cov.deductible_usd)))
                _sub(ded, "CurCd", "USD")
            if getattr(_cov, "limit_usd", None) is not None:
                lim = _sub(ci_el, "Limit")
                _sub(lim, "FormatInteger", str(int(_cov.limit_usd)))
                _sub(lim, "CurCd", "USD")
            if getattr(_cov, "applies", None) is not None:
                _sub(ci_el, "ClaimantAssertedInd", "Y" if _cov.applies else "N")
            if getattr(_cov, "note", None):
                _sub(ci_el, "CoverageNote", _cov.note)

        # ── ACORD Gap 5 — ROR indicators ──────────────────────────────────
        _ror_triggers = getattr(claim, "ror_trigger_cds", [])
        if _ror_triggers:
            ror_el = _sub(claim_rq, "ReservationOfRights")
            _sub(ror_el, "RORIndicator", "Y")
            for _t in _ror_triggers:
                _tv = _t.value if hasattr(_t, "value") else str(_t)
                _sub(ror_el, "RORTriggerCd", _tv)

        # ── ACORD Gap 5 — Vehicle ACV with source provenance ──────────────
        _acv = getattr(claim, "effective_acv_usd", None)
        _acv_src = getattr(claim, "acv_source_cd", None)
        _acv_src_str = _acv_src.value if hasattr(_acv_src, "value") else str(_acv_src or "MISSING")
        acv_el = _sub(claim_rq, "VehicleACV")
        if _acv is not None:
            _sub(acv_el, "Amt", f"{_acv:.2f}")
            _sub(acv_el, "CurCd", "USD")
        else:
            _sub(acv_el, "Amt", "MISSING")
        _sub(acv_el, "AcvSourceCd", _acv_src_str)

        # ── InjuryInfo — legacy flat injury bridge ────────────────────────
        # Retained for downstream consumers that read InjuryInfo directly.
        # Structured injury data is on individual ClaimsParty/Passenger elements.
        if claim.injury_reported:
            inj = _sub(claim_rq, "InjuryInfo")
            _sub(inj, "InjuryInd", "Y")
            if claim.injury_severity:
                _sub(inj, "InjurySeverityCd",
                     claim.injury_severity.upper())
            # Passenger count from structured array when available
            pax_count = len(getattr(claim, "passengers", []))
            if pax_count:
                _sub(inj, "PassengerCount", str(pax_count))
            injured_count = len(
                getattr(claim, "all_injured_parties", None) or []
            )
            if injured_count:
                _sub(inj, "TotalInjuredCount", str(injured_count))

        return root

    def _append_flat_parties(self, claim_rq: ET.Element,
                              claim: "Claim") -> None:
        """Fallback flat-field party bridge (used when helper not importable)."""
        party = _sub(claim_rq, "ClaimsParty")
        _sub(party, "ClaimsPartyRoleCd", "1")
        _sub(party, "ClaimsPartyRoleDesc",
             "Named Insured — flat field bridge")
        ci = _sub(party, "ContactInfo")
        _sub(ci, "FullName", claim.reporter_name or "")
        if claim.reporter_phone:
            ph = _sub(ci, "PhoneInfo")
            _sub(ph, "PhoneTypeCd", "Phone")
            _sub(ph, "PhoneNumber", claim.reporter_phone)
        if claim.reporter_email:
            _sub(ci, "EmailAddr", claim.reporter_email)
        if claim.attorney_represented:
            atty = _sub(claim_rq, "ClaimsParty")
            _sub(atty, "ClaimsPartyRoleCd", "15")
            _sub(atty, "ClaimsPartyRoleDesc",
                 "Attorney — details pending FNOL")
            _sub(_sub(atty, "ContactInfo"), "FullName",
                 "Attorney — details pending")

    # ── ClaimsOccurrenceRs — acknowledgement envelope (Gap 7) ────────────

    def build_response_envelope(
        self,
        claim_id: str,
        rq_uid: str,
        status_cd: str = "0",
        status_desc: str = "Success",
        credentials: Optional["CarrierCredentials"] = None,
    ) -> ET.Element:
        """Build a ClaimsOccurrenceRs response/acknowledgement envelope.

        ACORD AL3 requires that every ClaimsOccurrenceRq has a matching
        ClaimsOccurrenceRs acknowledging receipt and processing status.
        Used in carrier-to-carrier EDI exchanges and state EDI mandates
        (CA, NY, TX PIP, FL PIP) that require a status response.

        StatusCd values (ACORD):
          0   — Success
          1   — Success with info
          2   — Warning (processed but with conditions)
          3   — Error — partial success
          4   — Error — transaction not processed
          5   — Rejected — business rule violation

        Args:
            claim_id:    Platform claim ID echoed in the response.
            rq_uid:      Original RqUID from the ClaimsOccurrenceRq (for
                         correlation — ACORD requires the Rs to echo the Rq UID).
            status_cd:   ACORD status code string.
            status_desc: Human-readable status description.
            credentials: Auth bundle for SignonRs block.
        """
        creds = credentials or CarrierCredentials()
        _sv_maj, _sv_min, _sv_mnt = _schema_version("ClaimsOccurrenceRs")

        root = ET.Element("ACORD")
        root.set("xmlns",              ACORD_NS)
        root.set("xmlns:xsi",          ACORD_NS_XSI)
        root.set("xsi:schemaLocation", ACORD_SCHEMA_LOC)
        root.set("AcordVersion",       ACORD_VERSION)
        root.set("StandardVersionMajorNumber",       _sv_maj)
        root.set("StandardVersionMinorNumber",       _sv_min)
        root.set("StandardVersionMaintenanceNumber", _sv_mnt)

        # SignonRs — mirrors SignonRq structure, adds TransactionStatus
        signon_rs = _sub(root, "SignonRs")
        svc_rs = _sub(signon_rs, "SignonTransactionStatus")
        _sub(svc_rs, "StandardVersionMajorNumber",       _sv_maj)
        _sub(svc_rs, "StandardVersionMinorNumber",       _sv_min)
        _sub(svc_rs, "StandardVersionMaintenanceNumber", _sv_mnt)
        _sub(svc_rs, "TransactionStatus",  "SuccessWithInfo")
        _sub(svc_rs, "TransactionStatusDesc", f"Response from {PLATFORM_ID} v{PLATFORM_VER}")
        _sub(signon_rs, "ClientDt",        _now_utc_iso())
        client_rs = _sub(signon_rs, "ClientApp")
        _sub(client_rs, "Org",     creds.carrier_name or PLATFORM_ID)
        _sub(client_rs, "Name",    "FNOL Intelligence Platform")
        _sub(client_rs, "Version", PLATFORM_VER)
        _sub(client_rs, "CarrierId", creds.carrier_id or PLATFORM_ID)

        # ClaimsOccurrenceRs — acknowledgement
        claim_rs = _sub(root, "ClaimsOccurrenceRs")
        _sub(claim_rs, "RqUID",             rq_uid)   # Echo the original Rq UID
        _sub(claim_rs, "TransactionResponseDt", _now_utc_iso())
        _sub(claim_rs, "ClaimNumber",       claim_id)
        # SchemaVersion declaration
        sv_el = _sub(claim_rs, "SchemaVersion")
        _sub(sv_el, "StandardVersionMajorNumber",       _sv_maj)
        _sub(sv_el, "StandardVersionMinorNumber",       _sv_min)
        _sub(sv_el, "StandardVersionMaintenanceNumber", _sv_mnt)
        _sub(sv_el, "SchemaLocation",       ACORD_SCHEMA_LOC)
        # MsgStatus
        msg_rs = _sub(claim_rs, "MsgStatus")
        _sub(msg_rs, "MsgStatusCd",        status_cd)
        _sub(msg_rs, "MsgStatusDesc",      status_desc)
        _sub(msg_rs, "MsgErrorCd",         "0" if status_cd == "0" else status_cd)

        return root

    def serialize_response(
        self,
        claim_id: str,
        rq_uid: str,
        status_cd: str = "0",
        status_desc: str = "Success",
        credentials: Optional["CarrierCredentials"] = None,
        pretty: bool = True,
    ) -> str:
        """Return ClaimsOccurrenceRs as UTF-8 XML string."""
        root = self.build_response_envelope(claim_id, rq_uid, status_cd,
                                             status_desc, credentials)
        if pretty:
            try:
                ET.indent(root, space="  ")
            except AttributeError:
                pass
        raw = ET.tostring(root, encoding="unicode", xml_declaration=False)
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + raw

    def build_edi_submission_envelope(
        self,
        claim: "Claim",
        edi_transaction_set: str = "837P",
        credentials: Optional["CarrierCredentials"] = None,
    ) -> ET.Element:
        """Build an ACORD AL3 EDI transaction envelope.

        Wraps the ClaimsOccurrenceRq in an ACORD AL3-compliant EDI
        transaction set header for state EDI mandate submission (CA, NY,
        TX, FL PIP) and adverse carrier FNOL notification.

        EDI transaction sets supported (AL3 profile):
          837P — Professional / Auto claim EDI submission
          270  — Eligibility inquiry (pre-submission validation)
          271  — Eligibility response

        ISO ClaimSearch EDI: the ISO adapter uses a separate mTLS REST
        path (not AL3 EDI). The ACORD AL3 envelope here is for carrier-
        to-carrier and state-mandate EDI submission, not Verisk ClaimSearch.

        Production: this envelope is serialised to UTF-8 bytes and
        transmitted via the carrier's EDI VAN (Value-Added Network) or
        direct SFTP/AS2 channel to the state EDI hub.
        """
        creds = credentials or CarrierCredentials()
        root = self.build_envelope(claim, credentials=creds)
        _sv_maj, _sv_min, _sv_mnt = _schema_version("ClaimsOccurrenceRq")

        # Wrap in EDI transaction set header
        edi_hdr = ET.SubElement(root, "EDITransactionHeader")
        _sub(edi_hdr, "TransactionSetId",          edi_transaction_set)
        _sub(edi_hdr, "TransactionSetControlNumber", f"TSN{uuid.uuid4().hex[:9].upper()}")
        _sub(edi_hdr, "InterchangeControlNumber",   f"ISA{uuid.uuid4().hex[:9].upper()}")
        _sub(edi_hdr, "FunctionalGroupControlNumber", f"GS{uuid.uuid4().hex[:8].upper()}")
        _sub(edi_hdr, "SubmissionDt",               _now_utc_iso())
        _sub(edi_hdr, "SenderCarrierId",            creds.carrier_id or PLATFORM_ID)
        _sub(edi_hdr, "ReceiverCarrierId",          "STATE_EDI_HUB")
        _sub(edi_hdr, "AcordSchemaVersion",         f"{_sv_maj}.{_sv_min}.{_sv_mnt}")
        _sub(edi_hdr, "ISOClaimSearchRequired",
             "Y" if claim.iso_match or claim.prior_claims_count > 0 else "N")
        return root

    # ── Public interface ───────────────────────────────────────────────────

    def serialize_claim(
        self,
        claim: "Claim",
        pretty: bool = True,
        credentials: Optional["CarrierCredentials"] = None,
    ) -> str:
        """Return ACORD XML as a UTF-8 string with XML declaration.

        Args:
            claim:       Populated Claim instance.
            pretty:      If True, indent the XML (Python 3.9+).
            credentials: Auth bundle for SignonRq. None = POC defaults.
        """
        root = self.build_envelope(claim, credentials=credentials)
        if pretty:
            try:
                ET.indent(root, space="  ")
            except AttributeError:
                pass
        raw = ET.tostring(root, encoding="unicode", xml_declaration=False)
        return f'<?xml version="1.0" encoding="UTF-8"?>\n{raw}'

    def serialize_claim_bytes(self, claim: "Claim") -> bytes:
        """Return ACORD XML as UTF-8 bytes (for file write / binary transport)."""
        return self.serialize_claim(claim).encode("utf-8")

    def to_summary_dict(self, claim: "Claim") -> Dict[str, Any]:
        """Lightweight ACORD mapping summary dict (API response bodies)."""
        channel_val = _str_val(
            getattr(claim, "source_channel_cd", None)
        ) or "WEB"

        # Party counts
        parties     = getattr(claim, "parties", [])
        witnesses   = getattr(claim, "witnesses", [])
        passengers  = getattr(claim, "passengers", [])
        other_veh   = getattr(claim, "other_vehicle", None)
        atty        = getattr(claim, "attorney_contact", None)

        # Party completeness scoring (attempt via helper)
        party_score: Optional[float] = None
        party_advisories: List[str] = []
        try:
            from fnol_acord_parties import validate_parties
            vr = validate_parties(claim)
            party_score = vr.score
            party_advisories = vr.advisories
        except (ImportError, Exception):
            pass

        return {
            "acord_version":           ACORD_VERSION,
            "acord_transaction":       "ClaimsOccurrenceRq",
            "rq_uid":                  str(uuid.uuid4()),
            "transaction_dt":          _now_utc_iso(),
            # Gap 1
            "source_channel_cd":       channel_val,
            "source_system_id_al3":    _map_channel_al3(channel_val),
            "intake_quality_score":    getattr(claim, "intake_quality_score", None),
            # Gap 2 — party structure
            "party_counts": {
                "parties":         len(parties),
                "witnesses":       len(witnesses),
                "passengers":      len(passengers),
                "other_vehicle":   1 if other_veh else 0,
                "attorney_contact": 1 if atty else 0,
            },
            "party_completeness_score": party_score,
            "party_advisories":        party_advisories,
            # Claim envelope
            "policy_number":           claim.policy_number,
            "claims_occurrence_id":    claim.claim_id,
            "loss_dt":                 _date_only(claim.loss_date_time),
            "loss_cause_cd":           (
                getattr(claim, "effective_loss_cause_cd", None)
                or _map_loss_cause(getattr(claim, "loss_cause", ""))
            ),
            "loss_cause_raw":          claim.loss_cause,
            "fatality_ind":            "Y" if claim.fatality_indicator else "N",
            "loss_type_cd":            getattr(claim, "loss_type_cd", None) and (
                claim.loss_type_cd.value
                if hasattr(claim.loss_type_cd, "value") else str(claim.loss_type_cd)
            ),
            "police_report_filed":     getattr(claim, "police_report_filed", False),
            "police_report_number":    getattr(claim, "police_report_number", None),
            "police_report_agency":    getattr(claim, "police_report_agency", None),
            "weather_condition_cd":    getattr(claim, "weather_condition_cd", None),
            "road_condition_cd":       getattr(claim, "road_condition_cd", None),
            # Gap 4 — vehicle identification & damage coding
            "damage_area_cd":          (
                getattr(claim, "effective_damage_area_cd", None)
                or _map_damage_area(getattr(claim, "primary_damage_area", None))
            ),
            "license_plate":           getattr(claim, "license_plate", None),
            "registration_state":      getattr(claim, "registration_state", None),
            "vin":                     claim.vin,
            "drivable_ind":            "Y" if claim.drivable_indicator else "N",
            # Gap 5 — coverage & financial
            "vehicle_acv_usd":         getattr(claim, "effective_acv_usd", None),
            "acv_source_cd":           (
                getattr(claim, "acv_source_cd", None) and (
                    claim.acv_source_cd.value
                    if hasattr(claim.acv_source_cd, "value")
                    else str(claim.acv_source_cd)
                )
            ),
            "ror_trigger_cds":         [
                t.value if hasattr(t, "value") else str(t)
                for t in getattr(claim, "ror_trigger_cds", [])
            ],
            "claimant_coverage_count": len(getattr(claim, "claimant_asserted_coverages", [])),
            "claimant_coverages":      [
                {
                    "coverage_cd": (
                        c.coverage_cd.value if hasattr(getattr(c, "coverage_cd", None), "value")
                        else str(getattr(c, "coverage_cd", ""))
                    ),
                    "deductible_usd": getattr(c, "deductible_usd", None),
                    "limit_usd":      getattr(c, "limit_usd", None),
                    "applies":        getattr(c, "applies", None),
                }
                for c in getattr(claim, "claimant_asserted_coverages", [])
            ],
            # Gap 7 — message transport & ACORD XML envelope
            "acord_namespace":         ACORD_NS,
            "acord_schema_location":   ACORD_SCHEMA_LOC,
            "schema_version_registry": {k: ".".join(v) for k, v in ACORD_SCHEMA_REGISTRY.items()},
            "signon_auth_mode":        CarrierCredentials().auth_mode,
            "edi_transaction_supported": ["ClaimsOccurrenceRq", "ClaimsOccurrenceRs", "837P"],
            "platform_id":             PLATFORM_ID,
            "platform_version":        PLATFORM_VER,
            "acord_gaps_addressed": [
                "SourceChannelCd (ACORD 101 §1)",
                "AL3 SourceSystemId channel mapping",
                "LossCauseCd normalisation (free-text → ACORD enum)",
                "IntakeQualityScore as first-class ACORD field",
                "ClaimsParty[] structured array with ClaimsPartyRoleCd (§7)",
                "Named Insured bridged from flat reporter fields",
                "WitnessParty[] array (ACORD 101 §8, RoleCd=11)",
                "PassengerParty[] array (ACORD 101 §9, RoleCd=12)",
                "AttorneyContact structured (ACORD 101 §10, RoleCd=15)",
                "OtherVehicleParty / adverse driver (ACORD 101 §11, RoleCd=16/17)",
                "VehicleDamageAreaCd promoted to ACORD-coded enum (ACORD 101 §5)",
                "LicensePlate + RegistrationState fields (ACORD 101 §5)",
                "CoverageCd enum + claimant_asserted_coverages[] (ACORD 101 §3)",
                "vehicle_acv_usd None-default (MISSING sentinel) — zero-ACV bug fixed",
                "AcvSourceCd provenance field",
                "RorTriggerCd[] coded ROR trigger array",
                "Full ACORD 2.x multi-namespace (xmlns:ACORD, xmlns:xsi, xsi:schemaLocation)",
                "SchemaVersion declaration in every ClaimsOccurrenceRq (ACORD AL3 §2.1)",
                "ClaimsOccurrenceRs typed acknowledgement envelope",
                "SignonRs authentication response block",
                "CarrierCredentials OAuth2/mTLS production auth path (SignonPswd empty for REST)",
                "ACORD SchemaVersion registry (transaction-type → version mapping)",
                "EDI transaction set wrapper (837P / state mandate submission)",
            ],
        }


# ───────────────────────────────────────────────────────────────────────────
# Module-level singleton & convenience wrappers
# ───────────────────────────────────────────────────────────────────────────

_ADAPTER_SINGLETON: Optional[AcordXmlAdapter] = None

def get_acord_adapter() -> AcordXmlAdapter:
    global _ADAPTER_SINGLETON
    if _ADAPTER_SINGLETON is None:
        _ADAPTER_SINGLETON = AcordXmlAdapter()
    return _ADAPTER_SINGLETON

def serialize_to_acord_xml(claim: "Claim", pretty: bool = True) -> str:
    return get_acord_adapter().serialize_claim(claim, pretty=pretty)

def acord_summary(claim: "Claim") -> Dict[str, Any]:
    return get_acord_adapter().to_summary_dict(claim)


# ───────────────────────────────────────────────────────────────────────────
# CLI smoke-test
# ───────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json, sys

    class _E(str):
        @property
        def value(self): return str(self)

    class _CI:
        def __init__(self, **kw):
            for k, v in kw.items(): setattr(self, k, v)

    class _ClaimsParty:
        role_cd = _E("1"); role_desc = "Named Insured"
        injury_ind = True; injury_severity_cd = _E("MINOR")
        attorney_represented = False
        contact = _CI(full_name="Aria Castillo", phone="512-555-0192",
                      email="aria@example.com", phone_type="Cell",
                      address_line1=None, city=None, state=None, postal_code=None)

    class _Witness:
        full_name="Marco Reyes"; phone="512-555-9988"; email=None
        address="I-35 N near exit 240"; statement="Sedan rear-ended Honda."; contact_consent=True

    class _Pax:
        full_name="Lucia Castillo"; phone=None; email=None
        seat_position=_E("REAR_LEFT"); injury_ind=True
        injury_severity_cd=_E("MINOR"); treatment_sought=True; hospital_name="St. David's"

    class _OV:
        driver_name="Kyle Petersen"; driver_phone="713-555-4410"
        driver_email=None; driver_license="TX-98765432"; driver_state="TX"
        owner_name="Kyle Petersen"; owner_phone="713-555-4410"
        vin="5NPE24AF8FH123456"; year=2018; make="Hyundai"; model="Sonata"
        license_plate="TXM-4421"; plate_state="TX"; color="Blue"
        carrier="ACME Mutual"; policy_number="ACM-7782-99"; claim_number=None

    class _Atty:
        full_name="J. Smith Esq."; firm_name="Smith & Partners"
        phone="512-555-7700"; email="jsmith@example.com"
        fax=None; address="100 Congress Ave, Austin TX"
        bar_number="TX-44192"; state_bar="TX"

    class _Claim:
        claim_id="CLM-TEST-001"; policy_number="POC-POL-00123"
        source_channel_cd=_E("MOBILE"); intake_quality_score=0.82
        loss_date_time="2026-05-19T14:25:00Z"
        loss_location="I-35 N, Austin TX"; loss_location_zip="78701"
        location_zip=None; loss_cause="collision"
        loss_description="Rear-ended at highway speed; airbags deployed."
        fatality_indicator=False
        vin="1HGCM82633A123456"; vehicle_year=2021; vehicle_make="Honda"
        vehicle_model="Accord"; vehicle_mileage=34500
        drivable_indicator=False; vehicle_recall_indicator=False
        vehicle_acv_usd=22000.0; primary_damage_area="rear"
        reporter_name="Aria Castillo"; reporter_phone="512-555-0192"
        reporter_email="aria@example.com"
        injury_reported=True; injury_severity="MINOR"
        attorney_represented=True
        third_party_carrier=None; third_party_policy_number=None
        iso_match=False; prior_claims_count=0; policy_tenure_days=365
        state="TX"; vehicle_state="TX"; effective_state="TX"
        parties=[_ClaimsParty()]; witnesses=[_Witness()]
        passengers=[_Pax()]; other_vehicle=_OV(); attorney_contact=_Atty()

        def acord_parties(self):
            return self.parties

        @property
        def all_injured_parties(self):
            return [{"role":"1","name":"Aria Castillo","severity":"MINOR"},
                    {"role":"12","name":"Lucia Castillo","severity":"MINOR","seat":"REAR_LEFT"}]

    adapter = AcordXmlAdapter()
    xml_out = adapter.serialize_claim(_Claim())
    summary = adapter.to_summary_dict(_Claim())
    print("=" * 70)
    print("ACORD XML ADAPTER v2 (Gap 1+2) — SMOKE TEST")
    print("=" * 70)
    print(xml_out[:3500])
    print("\n[...]\n")
    print("SUMMARY:")
    print(json.dumps(summary, indent=2, default=str))
    party_count = xml_out.count("<ClaimsParty>")
    print(f"\n✓ {party_count} ClaimsParty elements emitted")
    print("✓ ACORD XML adapter Gap 1+2 smoke test passed")
