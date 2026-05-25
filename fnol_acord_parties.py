"""
FNOL Intelligence Platform — ACORD Party & Role Structure Helper
================================================================
ACORD Gap 2 — Party & Role Structure implementation.

Provides:
  • XML serialization of ClaimsParty[], WitnessParty[], PassengerParty[],
    OtherVehicleParty, and AttorneyContact into ACORD 101-conformant
    XML elements (consumed by fnol_acord_xml_adapter).
  • Party completeness validation with HITL advisory generation.
  • SOR field-mapping dictionaries for Duck Creek and Guidewire write-back.
  • Intake quality scoring contribution from party data completeness.

ACORD standards implemented:
  ACORD 101 §7  — ClaimsParty with ClaimsPartyRoleCd
  ACORD 101 §8  — Witness capture (RoleCd=11)
  ACORD 101 §9  — Passenger capture (RoleCd=12)
  ACORD 101 §10 — Attorney contact (RoleCd=15)
  ACORD 101 §11 — Adverse vehicle / Other Driver (RoleCd=16/17)

Design:
  This module is PURE (no FastAPI, no DB). It operates on the Pydantic
  model types from fnol_claim.py and returns xml.etree.ElementTree elements
  or plain dicts. No circular imports.

Usage:
    from fnol_acord_parties import AcordPartySerializer, validate_parties

    # In ACORD XML adapter:
    serializer = AcordPartySerializer()
    for el in serializer.parties_elements(claim):
        claim_rq.append(el)

    # In governance / intake quality:
    result = validate_parties(claim)
    advisories.extend(result.advisories)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fnol_claim import (
        Claim, ClaimsParty, WitnessParty, PassengerParty,
        OtherVehicleParty, AttorneyContact,
        ClaimsPartyRoleCd, InjurySeverityCd, SeatPositionCd,
    )


# ───────────────────────────────────────────────────────────────────────────
# ACORD role code → human label map (for XML role_desc fallback)
# ───────────────────────────────────────────────────────────────────────────

ROLE_LABELS: Dict[str, str] = {
    "1":  "Named Insured",
    "2":  "Additional Insured",
    "7":  "Claimant",
    "11": "Witness",
    "12": "Passenger",
    "15": "Attorney / Legal Representative",
    "16": "Other Driver",
    "17": "Other Vehicle Owner",
}


# ───────────────────────────────────────────────────────────────────────────
# XML element helpers (local — avoids importing from adapter)
# ───────────────────────────────────────────────────────────────────────────

def _sub(parent: ET.Element, tag: str,
         text: Optional[str] = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el

def _yn(val: bool) -> str:
    return "Y" if val else "N"

def _str_enum(val: Any) -> Optional[str]:
    """Safely get .value from an Enum or return the string as-is."""
    if val is None:
        return None
    return val.value if hasattr(val, "value") else str(val)


# ───────────────────────────────────────────────────────────────────────────
# XML Serializer
# ───────────────────────────────────────────────────────────────────────────

class AcordPartySerializer:
    """Generates ACORD XML elements for all party types from a Claim.

    Thread-safe and stateless — one instance may be shared across requests.
    All methods return ET.Element instances; the caller (AcordXmlAdapter)
    appends them to the ClaimsOccurrenceRq parent.
    """

    # ── ClaimsParty (Named Insured, Claimant, Additional Insured, Attorney) ─

    def party_element(self, party: "ClaimsParty") -> ET.Element:
        """Serialize a single ClaimsParty to an ACORD <ClaimsParty> element."""
        el = ET.Element("ClaimsParty")
        role = _str_enum(party.role_cd) or "1"
        _sub(el, "ClaimsPartyRoleCd", role)
        _sub(el, "ClaimsPartyRoleDesc",
             party.role_desc or ROLE_LABELS.get(role, "Unknown"))

        # ContactInfo block
        ci = _sub(el, "ContactInfo")
        _sub(ci, "FullName", party.contact.full_name)
        if party.contact.phone:
            ph = _sub(ci, "PhoneInfo")
            _sub(ph, "PhoneTypeCd", party.contact.phone_type or "Phone")
            _sub(ph, "PhoneNumber", party.contact.phone)
        if party.contact.email:
            _sub(ci, "EmailAddr", party.contact.email)
        if any([party.contact.address_line1, party.contact.city,
                party.contact.state, party.contact.postal_code]):
            addr = _sub(ci, "Addr")
            if party.contact.address_line1:
                _sub(addr, "Addr1", party.contact.address_line1)
            if party.contact.city:
                _sub(addr, "City", party.contact.city)
            if party.contact.state:
                _sub(addr, "StateProvCd", party.contact.state.upper())
            if party.contact.postal_code:
                _sub(addr, "PostalCode", party.contact.postal_code)

        # Injury indicators
        _sub(el, "InjuryInd", _yn(party.injury_ind))
        if party.injury_ind and party.injury_severity_cd:
            _sub(el, "InjurySeverityCd", _str_enum(party.injury_severity_cd))

        # Attorney flag at party level
        if party.attorney_represented:
            _sub(el, "AttorneyRepresentedInd", "Y")

        return el

    def parties_elements(self, claim: "Claim") -> List[ET.Element]:
        """Return ACORD XML elements for ALL ClaimsParty entries.

        Sources (merged in order):
          1. claim.acord_parties()  — structured + bridged flat reporter
          2. claim.witnesses        — each as ClaimsPartyRoleCd=11
          3. claim.passengers       — each as ClaimsPartyRoleCd=12
          4. claim.other_vehicle    — driver as RoleCd=16, owner as 17
          5. claim.attorney_contact — if not already in acord_parties()

        Returns list of ET.Element in ACORD document order.
        """
        elements: List[ET.Element] = []

        # 1. Named Insured / Claimant / Additional Insured / Attorney
        for p in claim.acord_parties():
            elements.append(self.party_element(p))

        # 2. Witnesses (ClaimsPartyRoleCd = 11)
        for w in claim.witnesses:
            elements.append(self.witness_element(w))

        # 3. Passengers (ClaimsPartyRoleCd = 12)
        for pax in claim.passengers:
            elements.append(self.passenger_element(pax))

        # 4. Adverse vehicle parties (RoleCd 16/17)
        if claim.other_vehicle:
            elements.extend(self.other_vehicle_elements(claim.other_vehicle))
        elif claim.third_party_carrier or claim.third_party_policy_number:
            # Bridge flat fields → minimal OTHER_DRIVER placeholder
            elements.append(self._bridge_third_party_element(claim))

        return elements

    # ── Witness (ClaimsPartyRoleCd = 11) ────────────────────────────────

    def witness_element(self, witness: "WitnessParty") -> ET.Element:
        """Serialize a WitnessParty to ACORD <ClaimsParty> with RoleCd=11."""
        el = ET.Element("ClaimsParty")
        _sub(el, "ClaimsPartyRoleCd", "11")
        _sub(el, "ClaimsPartyRoleDesc", "Witness")

        ci = _sub(el, "ContactInfo")
        _sub(ci, "FullName", witness.full_name)
        if witness.phone:
            ph = _sub(ci, "PhoneInfo")
            _sub(ph, "PhoneTypeCd", "Phone")
            _sub(ph, "PhoneNumber", witness.phone)
        if witness.email:
            _sub(ci, "EmailAddr", witness.email)
        if witness.address:
            _sub(ci, "Addr").append(
                ET.fromstring(f"<Addr1>{witness.address}</Addr1>"))

        # TCPA contact consent
        _sub(el, "ContactConsentInd", _yn(witness.contact_consent))

        # Witness statement (ACORD extension — platform field)
        if witness.statement:
            stmt = _sub(el, "WitnessStatement")
            _sub(stmt, "StatementText", witness.statement)

        return el

    # ── Passenger (ClaimsPartyRoleCd = 12) ──────────────────────────────

    def passenger_element(self, pax: "PassengerParty") -> ET.Element:
        """Serialize a PassengerParty to ACORD <ClaimsParty> with RoleCd=12."""
        el = ET.Element("ClaimsParty")
        _sub(el, "ClaimsPartyRoleCd", "12")
        _sub(el, "ClaimsPartyRoleDesc", "Passenger")

        ci = _sub(el, "ContactInfo")
        _sub(ci, "FullName", pax.full_name)
        if pax.phone:
            ph = _sub(ci, "PhoneInfo")
            _sub(ph, "PhoneTypeCd", "Phone")
            _sub(ph, "PhoneNumber", pax.phone)
        if pax.email:
            _sub(ci, "EmailAddr", pax.email)

        # Seat position (ACORD extension)
        seat = _str_enum(pax.seat_position) or "UNKNOWN"
        _sub(el, "SeatPositionCd", seat)

        # Injury
        _sub(el, "InjuryInd", _yn(pax.injury_ind))
        if pax.injury_ind:
            if pax.injury_severity_cd:
                _sub(el, "InjurySeverityCd",
                     _str_enum(pax.injury_severity_cd))
            _sub(el, "TreatmentSoughtInd", _yn(pax.treatment_sought))
            if pax.hospital_name:
                _sub(el, "HospitalName", pax.hospital_name)

        return el

    # ── Adverse / Other Vehicle (ClaimsPartyRoleCd = 16 / 17) ────────────

    def other_vehicle_elements(
            self, ov: "OtherVehicleParty") -> List[ET.Element]:
        """Return 1–3 ET.Elements for OtherVehicleParty.

        Elements emitted:
          • ClaimsParty (RoleCd=16) — Other Driver (always, if any info)
          • ClaimsParty (RoleCd=17) — Other Owner (only when owner ≠ driver
            and owner_name is present)
          • AdverseVehicleInfo — vehicle identification block
        """
        elements: List[ET.Element] = []

        # Driver party
        if ov.driver_name or ov.carrier:
            driver_el = ET.Element("ClaimsParty")
            _sub(driver_el, "ClaimsPartyRoleCd", "16")
            _sub(driver_el, "ClaimsPartyRoleDesc", "Other Driver")
            ci = _sub(driver_el, "ContactInfo")
            _sub(ci, "FullName", ov.driver_name or "Unknown — Third Party")
            if ov.driver_phone:
                ph = _sub(ci, "PhoneInfo")
                _sub(ph, "PhoneTypeCd", "Phone")
                _sub(ph, "PhoneNumber", ov.driver_phone)
            if ov.driver_email:
                _sub(ci, "EmailAddr", ov.driver_email)
            if ov.driver_license:
                _sub(driver_el, "DriverLicenseNumber", ov.driver_license)
                if ov.driver_state:
                    _sub(driver_el, "DriverLicenseStateCd",
                         ov.driver_state.upper())
            # Insurance reference on driver party
            if ov.carrier or ov.policy_number:
                ins = _sub(driver_el, "InsuranceInfo")
                if ov.carrier:
                    _sub(ins, "CarrierName", ov.carrier)
                if ov.policy_number:
                    _sub(ins, "PolicyNumber", ov.policy_number)
                if ov.claim_number:
                    _sub(ins, "ClaimNumber", ov.claim_number)
            elements.append(driver_el)

        # Owner party (only when explicitly different from driver)
        if ov.owner_name and ov.owner_name != ov.driver_name:
            owner_el = ET.Element("ClaimsParty")
            _sub(owner_el, "ClaimsPartyRoleCd", "17")
            _sub(owner_el, "ClaimsPartyRoleDesc", "Other Vehicle Owner")
            ci = _sub(owner_el, "ContactInfo")
            _sub(ci, "FullName", ov.owner_name)
            if ov.owner_phone:
                ph = _sub(ci, "PhoneInfo")
                _sub(ph, "PhoneTypeCd", "Phone")
                _sub(ph, "PhoneNumber", ov.owner_phone)
            elements.append(owner_el)

        # Adverse vehicle identification block
        if any([ov.vin, ov.make, ov.license_plate, ov.year]):
            veh_el = ET.Element("AdverseVehicleInfo")
            if ov.vin:
                _sub(veh_el, "VehIdentificationNumber", ov.vin)
            if ov.year:
                _sub(veh_el, "ModelYear", str(ov.year))
            if ov.make:
                _sub(veh_el, "Manufacturer", ov.make)
            if ov.model:
                _sub(veh_el, "Model", ov.model)
            if ov.color:
                _sub(veh_el, "VehicleColor", ov.color)
            if ov.license_plate:
                _sub(veh_el, "LicensePlateNumber", ov.license_plate)
                if ov.plate_state:
                    _sub(veh_el, "LicensePlateStateCd",
                         ov.plate_state.upper())
            elements.append(veh_el)

        return elements

    def _bridge_third_party_element(self, claim: "Claim") -> ET.Element:
        """Synthesise minimal OTHER_DRIVER ClaimsParty from flat fields.

        Called when claim.other_vehicle is absent but flat third_party_*
        fields are set. Preserves backward-compat for legacy payloads.
        """
        el = ET.Element("ClaimsParty")
        _sub(el, "ClaimsPartyRoleCd", "16")
        _sub(el, "ClaimsPartyRoleDesc",
             "Other Driver — bridged from flat third_party fields")
        ci = _sub(el, "ContactInfo")
        _sub(ci, "FullName", "Unknown — Third Party")
        if claim.third_party_carrier or claim.third_party_policy_number:
            ins = _sub(el, "InsuranceInfo")
            if claim.third_party_carrier:
                _sub(ins, "CarrierName", claim.third_party_carrier)
            if claim.third_party_policy_number:
                _sub(ins, "PolicyNumber", claim.third_party_policy_number)
        return el


# ───────────────────────────────────────────────────────────────────────────
# Party Completeness Validator
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class PartyValidationResult:
    """Output of validate_parties(). Consumed by governance / intake scoring."""
    score: float                          # 0.0–1.0 party completeness contribution
    advisories: List[str] = field(default_factory=list)
    hitl_required: bool = False
    gap_fields: List[str] = field(default_factory=list)
    acord_roles_present: List[str] = field(default_factory=list)
    party_counts: Dict[str, int] = field(default_factory=dict)


def validate_parties(claim: "Claim") -> PartyValidationResult:
    """Assess party data completeness against ACORD 101 requirements.

    Scoring weights:
      Named Insured ContactInfo complete (name + phone)  → 0.30
      Injury parties have severity coded                 → 0.20
      Witnesses captured when fatality_indicator=True    → 0.15
      Passengers captured when injury_reported=True      → 0.15
      OtherVehicle structured when 3P carrier present    → 0.10
      Attorney contact when attorney_represented=True    → 0.10

    Returns:
        PartyValidationResult with score, advisories, and HITL flag.
        HITL is triggered when score < 0.50 AND injury_reported = True.
    """
    advisories: List[str] = []
    gap_fields: List[str] = []
    score = 0.0

    # ── 1. Named Insured contact completeness ────────────────────────────
    parties = claim.acord_parties()
    roles_present = [_str_enum(p.role_cd) for p in parties]

    ni_parties = [p for p in parties if _str_enum(p.role_cd) == "1"]
    if ni_parties:
        ni = ni_parties[0]
        if ni.contact.full_name and ni.contact.phone:
            score += 0.30
        elif ni.contact.full_name:
            score += 0.15
            advisories.append(
                "ACORD 101 §7: Named Insured contact phone missing — "
                "required for carrier outreach and ACORD ClaimsParty write-back"
            )
            gap_fields.append("parties[0].contact.phone")
        else:
            advisories.append(
                "ACORD 101 §7: Named Insured party has no valid ContactInfo — "
                "HITL review required before SOR write-back"
            )
            gap_fields.append("parties[0].contact")
    else:
        advisories.append(
            "ACORD 101 §7: No Named Insured party (RoleCd=1) — "
            "reporter_name bridged; structured intake recommended"
        )

    # ── 2. Injury party severity coding ─────────────────────────────────
    injured_parties = [p for p in parties if p.injury_ind]
    injured_pax = [p for p in claim.passengers if p.injury_ind]
    all_injured = injured_parties + injured_pax

    if all_injured:
        coded = sum(1 for p in all_injured if getattr(p, "injury_severity_cd", None))
        if coded == len(all_injured):
            score += 0.20
        elif coded > 0:
            score += 0.10
            advisories.append(
                f"ACORD 101 §7: {len(all_injured) - coded} of {len(all_injured)} "
                "injured parties missing InjurySeverityCd — required for BI pathway"
            )
            gap_fields.append("injury_severity_cd")
        else:
            advisories.append(
                "ACORD 101 §7: Injury reported but no InjurySeverityCd on any party — "
                "SIU scoring and BI routing will use legacy injury_severity field"
            )
            gap_fields.append("injury_severity_cd (all injured parties)")
    else:
        # No injuries — full credit for this dimension
        score += 0.20

    # ── 3. Witness capture (required when fatality or police report) ─────
    if claim.fatality_indicator and not claim.witnesses:
        advisories.append(
            "ACORD 101 §8: Fatality indicated — witness statements required. "
            "Add WitnessParty[] before subrogation / litigation hold"
        )
        gap_fields.append("witnesses[]")
    else:
        score += 0.15

    # ── 4. Passenger capture (required when injury reported) ─────────────
    if claim.injury_reported and not claim.passengers:
        advisories.append(
            "ACORD 101 §9: Injury reported — passenger array empty. "
            "Capture PassengerParty[] for BI exposure assessment and MedPay routing"
        )
        gap_fields.append("passengers[]")
    else:
        score += 0.15

    # ── 5. Structured other_vehicle vs flat fields ───────────────────────
    has_tp = bool(claim.third_party_carrier or claim.third_party_policy_number)
    if has_tp and not claim.other_vehicle:
        advisories.append(
            "ACORD 101 §11: Third-party carrier present but OtherVehicleParty not "
            "structured. Adverse driver/VIN captured via flat fields only — "
            "upgrade to other_vehicle for ACORD XML ClaimsPartyRoleCd=16 compliance"
        )
        gap_fields.append("other_vehicle")
        score += 0.05   # Partial credit — flat fields carry the data
    else:
        score += 0.10

    # ── 6. Attorney contact completeness ────────────────────────────────
    if claim.attorney_represented and not claim.attorney_contact:
        advisories.append(
            "ACORD 101 §10: attorney_represented=True but AttorneyContact not "
            "populated — name, firm, and phone required for ClaimsPartyRoleCd=15"
        )
        gap_fields.append("attorney_contact")
        score += 0.05   # Partial — boolean flag preserves pipeline logic
    else:
        score += 0.10

    score = round(min(score, 1.0), 4)

    # HITL trigger: injury present + party data substantially incomplete
    hitl = (score < 0.50 and claim.injury_reported) or \
           (claim.fatality_indicator and not claim.witnesses)

    return PartyValidationResult(
        score=score,
        advisories=advisories,
        hitl_required=hitl,
        gap_fields=gap_fields,
        acord_roles_present=roles_present,
        party_counts={
            "parties":    len(claim.parties),
            "witnesses":  len(claim.witnesses),
            "passengers": len(claim.passengers),
            "other_vehicle": 1 if claim.other_vehicle else 0,
            "attorney_contact": 1 if claim.attorney_contact else 0,
        },
    )


# ───────────────────────────────────────────────────────────────────────────
# SOR Field Mapping Dictionaries
# ───────────────────────────────────────────────────────────────────────────

def party_to_duck_creek(party: "ClaimsParty") -> Dict[str, Any]:
    """Map a ClaimsParty to Duck Creek OnDemand ClaimParticipant fields.

    Duck Creek OnDemand ClaimParticipant schema (relevant subset):
      ParticipantType     → ClaimsPartyRoleCd mapped string
      ParticipantName     → contact.full_name
      PhoneNumber         → contact.phone
      EmailAddress        → contact.email
      InjuryIndicator     → injury_ind
      InjurySeverity      → injury_severity_cd
    """
    dc_role_map = {
        "1":  "NamedInsured",
        "2":  "AdditionalInsured",
        "7":  "Claimant",
        "11": "Witness",
        "12": "Passenger",
        "15": "Attorney",
        "16": "OtherDriver",
        "17": "OtherOwner",
    }
    role = _str_enum(party.role_cd) or "1"
    return {
        "ParticipantType":    dc_role_map.get(role, "Claimant"),
        "ParticipantName":    party.contact.full_name,
        "PhoneNumber":        party.contact.phone,
        "EmailAddress":       party.contact.email,
        "InjuryIndicator":    party.injury_ind,
        "InjurySeverity":     _str_enum(party.injury_severity_cd),
        "AttorneyRepresented": party.attorney_represented,
    }


def party_to_guidewire(party: "ClaimsParty") -> Dict[str, Any]:
    """Map a ClaimsParty to Guidewire ClaimCenter ClaimContact fields.

    Guidewire ContactRole codes (carrier-configured; platform defaults):
      insured      → Named Insured
      claimant     → Claimant
      witness      → Witness
      passenger    → Passenger
      attorney     → Attorney
      otherdriver  → Other Driver
    """
    gw_role_map = {
        "1":  "insured",
        "2":  "additionalinsured",
        "7":  "claimant",
        "11": "witness",
        "12": "passenger",
        "15": "attorney",
        "16": "otherdriver",
        "17": "otherowner",
    }
    role = _str_enum(party.role_cd) or "1"
    return {
        "contactRole":      gw_role_map.get(role, "claimant"),
        "displayName":      party.contact.full_name,
        "workPhone":        party.contact.phone,
        "emailAddress1":    party.contact.email,
        "injuryDescription": _str_enum(party.injury_severity_cd),
        "represented":       party.attorney_represented,
    }


def other_vehicle_to_duck_creek(ov: "OtherVehicleParty") -> Dict[str, Any]:
    """Map OtherVehicleParty to Duck Creek AdverseParty + AdverseVehicle."""
    return {
        "AdverseParty": {
            "ParticipantType":  "OtherDriver",
            "ParticipantName":  ov.driver_name or "Unknown",
            "PhoneNumber":      ov.driver_phone,
            "DriverLicense":    ov.driver_license,
            "LicenseState":     ov.driver_state,
            "CarrierName":      ov.carrier,
            "PolicyNumber":     ov.policy_number,
            "ClaimNumber":      ov.claim_number,
        },
        "AdverseVehicle": {
            "VIN":              ov.vin,
            "Year":             ov.year,
            "Make":             ov.make,
            "Model":            ov.model,
            "LicensePlate":     ov.license_plate,
            "PlateState":       ov.plate_state,
            "Color":            ov.color,
        },
    }


# ───────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ───────────────────────────────────────────────────────────────────────────

_SERIALIZER: Optional[AcordPartySerializer] = None

def get_party_serializer() -> AcordPartySerializer:
    global _SERIALIZER
    if _SERIALIZER is None:
        _SERIALIZER = AcordPartySerializer()
    return _SERIALIZER


# ───────────────────────────────────────────────────────────────────────────
# CLI smoke-test
# ───────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, json

    # Minimal shims to test without importing the full stack
    class _E(str):
        @property
        def value(self): return str(self)

    class _CI:
        def __init__(self, **kw):
            for k, v in kw.items(): setattr(self, k, v)

    class _CP:
        role_cd = _E("1")
        role_desc = "Named Insured"
        injury_ind = True
        injury_severity_cd = _E("MINOR")
        attorney_represented = False
        contact = _CI(full_name="Aria Castillo", phone="512-555-0192",
                      email="aria@example.com", phone_type="Cell",
                      address_line1=None, city=None, state=None, postal_code=None)

    class _WP:
        full_name = "Marco Reyes"
        phone = "512-555-9988"
        email = None
        address = "I-35 N near exit 240, Austin TX"
        statement = "The blue sedan rear-ended the Honda without braking."
        contact_consent = True

    class _PP:
        full_name = "Lucia Castillo"
        phone = None
        email = None
        seat_position = _E("REAR_LEFT")
        injury_ind = True
        injury_severity_cd = _E("MINOR")
        treatment_sought = True
        hospital_name = "St. David's Medical Center"

    class _OV:
        driver_name = "Kyle Petersen"
        driver_phone = "713-555-4410"
        driver_email = None
        driver_license = "TX-98765432"
        driver_state = "TX"
        owner_name = "Kyle Petersen"
        owner_phone = "713-555-4410"
        vin = "5NPE24AF8FH123456"
        year = 2018
        make = "Hyundai"
        model = "Sonata"
        license_plate = "TXM-4421"
        plate_state = "TX"
        color = "Blue"
        carrier = "ACME Mutual"
        policy_number = "ACM-7782-99"
        claim_number = None

    serializer = AcordPartySerializer()
    root = ET.Element("ClaimsOccurrenceRq")

    root.append(serializer.party_element(_CP()))
    root.append(serializer.witness_element(_WP()))
    root.append(serializer.passenger_element(_PP()))
    for el in serializer.other_vehicle_elements(_OV()):
        root.append(el)

    try:
        ET.indent(root, space="  ")
    except AttributeError:
        pass

    xml_out = ET.tostring(root, encoding="unicode")
    print("=" * 70)
    print("ACORD PARTY SERIALIZER — SMOKE TEST")
    print("=" * 70)
    print(xml_out)
    print("\nDuck Creek party mapping:")
    print(json.dumps(party_to_duck_creek(_CP()), indent=2, default=str))
    print("\nGuidewire party mapping:")
    print(json.dumps(party_to_guidewire(_CP()), indent=2, default=str))
    print("\nOther vehicle DC mapping:")
    print(json.dumps(other_vehicle_to_duck_creek(_OV()), indent=2, default=str))
    print("\n✓ Party serializer smoke test passed")
