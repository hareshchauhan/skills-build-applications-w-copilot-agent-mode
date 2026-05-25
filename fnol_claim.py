"""
FNOL Intelligence Platform — Canonical Claim model
==================================================
Single Pydantic model used end-to-end by the workflow engine, the agents,
and the API surface. Replaces the previous `Dict[str, Any]` claim plumbing
that required every consumer to call `.get(name, default)` and rebuild the
shape by hand (see fnol_conversational_agent._build_claim_payload before
this refactor for an example).

The model is INTENTIONALLY permissive on absent values — most fields are
Optional with sensible defaults — because intake from a customer phone call
or partial telematics signal may produce a claim with many unset fields.
Pipeline stages are responsible for emitting HITL/advisories when a field
they need is missing.

Boundary rules:
  • The API server, the workflow engine, the conversational agent, and the
    A11 total-loss agent all PASS `Claim` instances between themselves.
  • The SOR adapter remains dict-based (its internal records carry
    pipeline-derived fields like `summary`, `policy_snapshot`, etc., which
    are NOT part of the claim contract). The engine serialises Claim →
    dict at that boundary via `.model_dump(...)`.\
  • The Adjuster Co-Pilot's `_compact_claim_view` takes the SOR-returned
    record (a dict), not a Claim, because that's the post-pipeline shape
    that contains stage-outputs.

Backward-compatibility contract (ACORD Gap 2 — Party & Role Structure):
  • ALL flat reporter/party fields (reporter_name, reporter_phone,
    reporter_email, attorney_represented, third_party_carrier,
    third_party_policy_number, injury_reported, injury_severity) are
    PRESERVED unchanged. Existing agents have 20+ call sites on these
    fields and must not be broken.
  • The new structured arrays (parties, witnesses, passengers,
    other_vehicle, attorney_contact) are ADDITIVE. They default to
    empty / None so legacy payloads that omit them remain valid.
  • acord_parties() builds the canonical ACORD ClaimsParty[] view by
    merging structured arrays with the flat fields. Agents that need
    ACORD-conformant output call this; legacy agents ignore it entirely.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ───────────────────────────────────────────────────────────────────────────
# ACORD-conformant enumerations  (Gap 1 — Core Identity, Gap 2 — Parties)
# ───────────────────────────────────────────────────────────────────────────

class SourceChannelCd(str, Enum):
    """ACORD 101 §1 — SourceChannelCd intake channel coded values.

    Maps directly to the ACORD TransactionSource enumeration used in the
    ClaimsOccurrenceRq envelope and AL3 EDI X12 837P transaction set.

    THIRD_PARTY_API is the platform-extended value for machine-originated
    intake (telematics, OEM crash-notification, ISO ClaimSearch push).
    All five values are accepted in the ACORD XML serializer.

    Channel drives the SOR write-back field:
      Duck Creek OnDemand  → Claim.SourceChannel (string code)
      Guidewire ClaimCenter → ClaimContact.ContactRole + IntakeChannel
      ACORD XML            → ClaimsOccurrenceRq/MsgStatus/SourceSystemId
    """
    WEB              = "WEB"
    IVR              = "IVR"
    AGENT            = "AGENT"
    MOBILE           = "MOBILE"
    THIRD_PARTY_API  = "THIRD_PARTY_API"


class ClaimsPartyRoleCd(str, Enum):
    """ACORD 101 ClaimsPartyRoleCd — role of each party in the loss event.

    Numeric string values are the ACORD canonical codes; the enum name is
    the platform-readable alias. Used in ClaimsParty.role_cd and emitted
    verbatim into ACORD XML <ClaimsPartyRoleCd> elements.

    ACORD 101 role codes relevant to auto FNOL:
      1  Named Insured     — primary policyholder
      2  Additional Insured
      7  Claimant          — may differ from named insured (e.g. permissive user)
      11 Witness
      12 Passenger         — occupant of insured vehicle
      15 Attorney          — legal representative
      16 Other Driver      — driver of the adverse / third-party vehicle
      17 Other Owner       — owner of adverse vehicle if different from driver
    """
    NAMED_INSURED       = "1"
    ADDITIONAL_INSURED  = "2"
    CLAIMANT            = "7"
    WITNESS             = "11"
    PASSENGER           = "12"
    ATTORNEY            = "15"
    OTHER_DRIVER        = "16"
    OTHER_OWNER         = "17"


class InjurySeverityCd(str, Enum):
    """ACORD-aligned injury severity codes used on ClaimsParty and Claim."""
    NONE     = "NONE"
    MINOR    = "MINOR"
    MODERATE = "MODERATE"
    SEVERE   = "SEVERE"
    FATAL    = "FATAL"


class SeatPositionCd(str, Enum):
    """Seat position codes for passenger parties (ACORD extension)."""
    FRONT_LEFT  = "FRONT_LEFT"
    FRONT_RIGHT = "FRONT_RIGHT"
    REAR_LEFT   = "REAR_LEFT"
    REAR_CENTER = "REAR_CENTER"
    REAR_RIGHT  = "REAR_RIGHT"
    UNKNOWN     = "UNKNOWN"


class VehicleDamageAreaCd(str, Enum):
    """ACORD 101 §5 VehicleDamageAreaCd — coded primary damage area.

    Promotes the free-text `primary_damage_area` field to an ACORD-coded
    enum. The free-text field is preserved for backward-compat; this enum
    is the ACORD-conformant representation used in XML serialisation and
    SOR write-back.

    ACORD 101 §5 canonical codes:
      FRT     — Front (bumper, hood, grille, headlights)
      REAR    — Rear (bumper, trunk, tailgate, taillights)
      LFTSD   — Left side (driver side in LHT jurisdictions)
      RGTSD   — Right side (passenger side in LHT jurisdictions)
      ROOF    — Roof / top panel
      UNDRBD  — Underbody / undercarriage (frame, floor pan)
      INTRNL  — Interior (cabin damage, airbag deployment)
      ALL     — All areas / total loss candidate / rollover
      UNKNWN  — Unknown / not captured at intake

    SOR mapping:
      Duck Creek OnDemand  → Claim.DamageArea (string code)
      Guidewire ClaimCenter → VehicleIncident.impactType (carrier-mapped)
      ACORD XML            → AutoLossInfo/VehicleInfo/VehicleDamageAreaCd

    Note: UNKNWN (not OTH) is used as the default because the adjuster
    will always record a coded area during inspection; OTH implies a
    recognised but uncategorised cause, which does not apply to a body
    panel. ACORD uses UNKNWN for 'not captured at intake'.
    """
    FRT    = "FRT"
    REAR   = "REAR"
    LFTSD  = "LFTSD"
    RGTSD  = "RGTSD"
    ROOF   = "ROOF"
    UNDRBD = "UNDRBD"
    INTRNL = "INTRNL"
    ALL    = "ALL"
    UNKNWN = "UNKNWN"


# ── VehicleDamageAreaCd normalisation map (single source of truth) ─────────
# Three vocabulary spaces:
#   1. ACORD codes  (identity passthrough)
#   2. Platform free-text (from conversational agent / structured intake)
#   3. Common adjuster shorthand / OEM damage report labels
# Adapter's local _DAMAGE_AREA_MAP is deleted; it delegates here.

_DAMAGE_AREA_NORMALISE_MAP: Dict[str, str] = {
    # ACORD identity passthrough
    "frt":    "FRT",
    "rear":   "REAR",
    "lftsd":  "LFTSD",
    "rgtsd":  "RGTSD",
    "roof":   "ROOF",
    "undrbd": "UNDRBD",
    "intrnl": "INTRNL",
    "all":    "ALL",
    "unknwn": "UNKNWN",

    # Front variants
    "front":         "FRT",
    "front end":     "FRT",
    "front-end":     "FRT",
    "hood":          "FRT",
    "bumper front":  "FRT",
    "front bumper":  "FRT",
    "grille":        "FRT",
    "headlight":     "FRT",
    "headlights":    "FRT",

    # Rear variants
    "back":          "REAR",
    "rear end":      "REAR",
    "rear-end":      "REAR",
    "trunk":         "REAR",
    "tailgate":      "REAR",
    "rear bumper":   "REAR",
    "bumper rear":   "REAR",
    "taillight":     "REAR",
    "taillights":    "REAR",

    # Left side variants
    "left":          "LFTSD",
    "left side":     "LFTSD",
    "driver side":   "LFTSD",
    "driver's side": "LFTSD",
    "drivers side":  "LFTSD",
    "lf":            "LFTSD",
    "lr":            "LFTSD",

    # Right side variants
    "right":         "RGTSD",
    "right side":    "RGTSD",
    "passenger side":"RGTSD",
    "rf":            "RGTSD",
    "rr":            "RGTSD",

    # Roof variants
    "top":           "ROOF",
    "top panel":     "ROOF",
    "sunroof":       "ROOF",
    "moonroof":      "ROOF",
    "convertible top": "ROOF",

    # Underbody variants
    "under":         "UNDRBD",
    "underbody":     "UNDRBD",
    "undercarriage": "UNDRBD",
    "frame":         "UNDRBD",
    "floor pan":     "UNDRBD",
    "bottom":        "UNDRBD",

    # Interior variants
    "interior":      "INTRNL",
    "inside":        "INTRNL",
    "cabin":         "INTRNL",
    "airbag":        "INTRNL",
    "airbags":       "INTRNL",
    "dashboard":     "INTRNL",
    "windshield":    "INTRNL",   # Glass-interior boundary; coded INTRNL per ACORD

    # All / total variants
    "total":         "ALL",
    "total loss":    "ALL",
    "all over":      "ALL",
    "rollover":      "ALL",
    "rolled":        "ALL",
    "all areas":     "ALL",

    # Unknown / not captured
    "unknown":       "UNKNWN",
    "n/a":           "UNKNWN",
    "not sure":      "UNKNWN",
    "tbd":           "UNKNWN",
    "other":         "UNKNWN",   # ACORD uses UNKNWN not OTH for vehicle area
}


def normalise_damage_area_cd(raw: Optional[str]) -> "VehicleDamageAreaCd":
    """Normalise any damage area string to a VehicleDamageAreaCd enum member.

    Precedence:
      1. Exact key match (lower-stripped, hyphens → spaces)
      2. Substring match (longest key wins)
      3. Default: VehicleDamageAreaCd.UNKNWN

    Called by Claim.effective_damage_area_cd and the ACORD XML adapter.

    Args:
        raw: Free-text primary_damage_area, platform code, or ACORD code.
    Returns:
        VehicleDamageAreaCd enum member; UNKNWN when unresolvable.
    """
    if not raw:
        return VehicleDamageAreaCd.UNKNWN
    normalised = raw.lower().strip().replace("-", " ")

    hit = _DAMAGE_AREA_NORMALISE_MAP.get(normalised)
    if hit:
        return VehicleDamageAreaCd(hit)

    best_key, best_val = "", ""
    for key, val in _DAMAGE_AREA_NORMALISE_MAP.items():
        if key in normalised and len(key) > len(best_key):
            best_key, best_val = key, val
    if best_val:
        return VehicleDamageAreaCd(best_val)

    return VehicleDamageAreaCd.UNKNWN


class CoverageCd(str, Enum):
    """ACORD CoverageCd — line-of-coverage codes for auto claims.

    Used in `claimant_asserted_coverages` on Claim (intake capture of which
    coverages the claimant believes apply) and in ClaimantCoverage.coverage_cd
    for per-coverage limit/deductible representation.

    ACORD P&C Data Standards — auto coverage codes:
      COLL  — Collision
      COMP  — Comprehensive (fire, theft, weather, animal)
      BI    — Bodily Injury Liability
      PD    — Property Damage Liability
      MED   — Medical Payments (MedPay / PIP depending on state)
      UM    — Uninsured Motorist
      UIM   — Underinsured Motorist
      RENTAL— Rental Reimbursement (platform extension; accepted by ACORD as LOC extension)
      ROAD  — Roadside / Towing Assistance (platform extension)

    SOR mapping:
      Duck Creek OnDemand  → Claim.CoverageType / ClaimLine.CoverageCode
      Guidewire ClaimCenter → Coverage.type (carrier-configured typelist)
      ACORD XML            → CoverageInfo/CoverageCd
    """
    COLL   = "COLL"
    COMP   = "COMP"
    BI     = "BI"
    PD     = "PD"
    MED    = "MED"
    UM     = "UM"
    UIM    = "UIM"
    RENTAL = "RENTAL"
    ROAD   = "ROAD"


class AcvSourceCd(str, Enum):
    """Source of the vehicle Actual Cash Value figure.

    Governs how the pipeline and SOR adapters treat `vehicle_acv_usd`.
    MISSING is the honest default — it forces the TL route to defer rather
    than silently guess. This closes the zero-ACV → MODERATE routing bug
    documented in the ACORD gap analysis.

    SOR write-back: the source code is written alongside the ACV value so
    the carrier system knows whether to trust it or trigger a valuation step.
    """
    MISSING     = "MISSING"       # Not yet captured — TL determination deferred
    CLAIMANT    = "CLAIMANT"      # Self-reported by insured at intake
    GUIDEBOOK   = "GUIDEBOOK"     # NADA / KBB / CCC lookup (adjuster or automated)
    INDEPENDENT = "INDEPENDENT"   # Independent appraisal
    AGREED      = "AGREED"        # Agreed value (classic / stated value policy)
    PRIOR_SOR   = "PRIOR_SOR"     # Retrieved from prior SOR record (renewal carry-forward)


class RorTriggerCd(str, Enum):
    """ACORD-aligned Reservation of Rights trigger codes.

    Extends the free-text `ror_letter_text` in S2 outputs with a coded
    array captured at intake and enriched by S2. Enables the governance
    layer and SOR adapters to route ROR letters programmatically without
    parsing the text body.

    SOR mapping:
      Duck Creek OnDemand  → Claim.ReservationOfRightsReason
      Guidewire ClaimCenter → Claim.rorStatus + Claim.rorReason (carrier-configured)
    """
    POLICY_LAPSE       = "POLICY_LAPSE"        # Policy not in force at loss date
    EXCLUSION          = "EXCLUSION"           # Named exclusion on policy triggered
    COVERAGE_DISPUTE   = "COVERAGE_DISPUTE"    # Coverage applicability unclear
    LATE_NOTICE        = "LATE_NOTICE"         # Late reporting — prejudice analysis needed
    FRAUD_SUSPECTED    = "FRAUD_SUSPECTED"     # SIU referral active — S3/S4A trigger
    SUBROGATION        = "SUBROGATION"         # Third-party recovery possible — rights preserved
    ATTORNEY_INVOLVED  = "ATTORNEY_INVOLVED"   # Attorney representation at intake
    MULTI_CLAIMANT     = "MULTI_CLAIMANT"      # Multiple claimants — limits analysis needed
    EUO_REQUIRED       = "EUO_REQUIRED"        # Examination Under Oath required


class ClaimantCoverage(BaseModel):
    """Per-coverage intake record — claimant's stated coverage belief at FNOL.

    Represents what the claimant asserts applies to their loss, not the
    policy truth (S2 adjudicates actual coverage). Capturing the assertion
    at intake:
      • Satisfies ACORD 101 §3 requirement to record tendered coverages
      • Enables S2 to quickly surface mis-assertions (e.g. COMP for collision)
      • Feeds governance adverse-action risk scoring when asserted ≠ adjudicated

    ACORD XML: serialised as <CoverageInfo> blocks within ClaimsOccurrenceRq.
    Duck Creek: mapped to ClaimLine per coverage.
    Guidewire: mapped to Coverage.type per line item.

    Fields:
        coverage_cd:    ACORD CoverageCd — the coverage the claimant asserts.
        deductible_usd: Claimant-stated or policy-retrieved deductible for this
                        coverage. None = not captured; 0 = stated as waived.
        limit_usd:      Policy limit for this coverage. None = not retrieved.
        applies:        True = claimant believes this coverage applies.
                        False = claimant explicitly disclaims (rare at intake).
                        None = not evaluated.
        note:           Free-text annotation (e.g. "rental waiver negotiated").
    """
    model_config = ConfigDict(extra="ignore")

    coverage_cd:    CoverageCd
    deductible_usd: Optional[float] = Field(
        default=None,
        ge=0,
        description="Per-coverage deductible in USD. None = not captured; 0 = waived.",
    )
    limit_usd:      Optional[float] = Field(
        default=None,
        ge=0,
        description="Policy limit for this coverage in USD.",
    )
    applies:        Optional[bool] = Field(
        default=None,
        description="Claimant assertion: True = applies, False = disclaimed, None = not evaluated.",
    )
    note:           Optional[str] = None


class LossCauseCd(str, Enum):
    """ACORD 101 §6 LossCauseCd — coded loss cause enumeration.

    Replaces/encodes the free-text `loss_cause` field for ACORD XML
    serialisation and SOR write-back. The free-text field is preserved
    unchanged for backward-compat; this enum is the ACORD-conformant
    parallel representation.

    ACORD canonical codes (ACORD 101 §6 / P&C Data Standards):
      COLLSN   — Collision (any contact with another vehicle or object)
      COMPRE   — Comprehensive (weather, animal, fire, flood, theft-of-parts)
      THEFT    — Theft / stolen vehicle
      FIRE     — Fire (origin: electrical, arson, external)
      FLOOD    — Flood / water immersion
      HAIL     — Hail damage
      GLASS    — Glass only (windshield, windows — no other damage)
      HITNRN   — Hit and run (adverse driver fled scene)
      VANDLSM  — Vandalism / malicious mischief
      UNINS    — Uninsured / underinsured motorist
      OTH      — Other / not coded at intake (ACORD catch-all)

    SOR mapping:
      Duck Creek OnDemand  → Claim.CauseOfLoss (string code)
      Guidewire ClaimCenter → Claim.lossCause (string, carrier-configured)
      ACORD XML            → LossInfo/LossCauseCd
    """
    COLLSN  = "COLLSN"
    COMPRE  = "COMPRE"
    THEFT   = "THEFT"
    FIRE    = "FIRE"
    FLOOD   = "FLOOD"
    HAIL    = "HAIL"
    GLASS   = "GLASS"
    HITNRN  = "HITNRN"
    VANDLSM = "VANDLSM"
    UNINS   = "UNINS"
    OTH     = "OTH"


class LossTypeCd(str, Enum):
    """ACORD LossTypeCd — line-of-business / coverage pathway indicator.

    Derived from LossCauseCd + injury/third-party flags in the pipeline
    (S1 stamps it; S5 may refine it). Drives coverage routing and reserve
    calculations in S2 / S6.

    ACORD P&C codes (ACORD 101 / Claim Data Standards):
      APD   — Auto Physical Damage (first-party vehicle repair / TL)
      BI    — Bodily Injury (third-party or UM/UIM injury claim)
      PD    — Property Damage (third-party vehicle / property)
      THEFT — Theft / vehicle recovery
      GLASS — Glass-only claim (low complexity; STP-eligible)
      COMBO — Combined APD + BI (insured vehicle damage + injury present)

    SOR mapping:
      Duck Creek OnDemand  → Claim.ClaimType
      Guidewire ClaimCenter → Claim.lossType
    """
    APD   = "APD"
    BI    = "BI"
    PD    = "PD"
    THEFT = "THEFT"
    GLASS = "GLASS"
    COMBO = "COMBO"


# ───────────────────────────────────────────────────────────────────────────
# ACORD Gap 3 — Loss cause normalisation map (single source of truth)
#
# Unifies three vocabulary spaces into one canonical lookup:
#   1. ACORD codes  (identity: "COLLSN" → COLLSN)
#   2. Platform codes from fnol_conversational_agent.LOSS_CAUSE_MAP
#      ("REAR_END_COLLISION", "SIDE_IMPACT", "ANIMAL_STRIKE", ...)
#   3. Free-text intake phrases ("rear-end", "hit and run", "hail", ...)
#
# Used by normalise_loss_cause_cd() and Claim.effective_loss_cause_cd.
# The ACORD XML adapter delegates to effective_loss_cause_cd, eliminating
# its own local map and making this the platform-wide single source.
# ───────────────────────────────────────────────────────────────────────────

_LOSS_CAUSE_NORMALISE_MAP: Dict[str, str] = {
    # ── ACORD codes (identity — already coded payloads pass through) ──────
    "collsn":   "COLLSN",
    "compre":   "COMPRE",
    "theft":    "THEFT",
    "fire":     "FIRE",
    "flood":    "FLOOD",
    "hail":     "HAIL",
    "glass":    "GLASS",
    "hitnrn":   "HITNRN",
    "vandlsm":  "VANDLSM",
    "unins":    "UNINS",
    "oth":      "OTH",

    # ── Platform codes from fnol_conversational_agent.LOSS_CAUSE_MAP ──────
    "rear_end_collision":  "COLLSN",
    "head_on_collision":   "COLLSN",
    "side_impact":         "COLLSN",
    "single_vehicle":      "COLLSN",   # rollover / run-off-road
    "animal_strike":       "COMPRE",   # animals are comprehensive in ACORD
    "glass_only":          "GLASS",
    "vandalism":           "VANDLSM",

    # ── Free-text intake phrases ───────────────────────────────────────────
    # Collision variants
    "collision":           "COLLSN",
    "collided":            "COLLSN",
    "rear-end":            "COLLSN",
    "rear end":            "COLLSN",
    "rear ended":          "COLLSN",
    "rear-ended":          "COLLSN",
    "head-on":             "COLLSN",
    "head on":             "COLLSN",
    "t-bone":              "COLLSN",
    "t bone":              "COLLSN",
    "side-swipe":          "COLLSN",
    "sideswipe":           "COLLSN",
    "side swipe":          "COLLSN",
    "rollover":            "COLLSN",
    "rolled over":         "COLLSN",
    "ran off road":        "COLLSN",
    "run off road":        "COLLSN",
    "hit a pole":          "COLLSN",
    "hit a tree":          "COLLSN",
    "hit a wall":          "COLLSN",
    "struck":              "COLLSN",

    # Comprehensive variants
    "comprehensive":       "COMPRE",
    "animal":              "COMPRE",
    "deer":                "COMPRE",
    "hailstorm":           "HAIL",
    "hail damage":         "HAIL",
    "hail storm":          "HAIL",
    "weather":             "HAIL",    # Most weather = hail in auto
    "flood":               "FLOOD",
    "flooded":             "FLOOD",
    "water damage":        "FLOOD",
    "submerged":           "FLOOD",
    "fire":                "FIRE",
    "caught fire":         "FIRE",
    "burned":              "FIRE",
    "arson":               "FIRE",

    # Theft variants
    "theft":               "THEFT",
    "stolen":              "THEFT",
    "vehicle stolen":      "THEFT",
    "car stolen":          "THEFT",
    "theft of vehicle":    "THEFT",

    # Glass variants
    "glass":               "GLASS",
    "windshield":          "GLASS",
    "cracked windshield":  "GLASS",
    "broken windshield":   "GLASS",
    "window":              "GLASS",

    # Hit and run
    "hit and run":         "HITNRN",
    "hit-and-run":         "HITNRN",
    "hit & run":           "HITNRN",
    "fled the scene":      "HITNRN",
    "left the scene":      "HITNRN",
    "unknown driver":      "HITNRN",

    # Vandalism
    "vandal":              "VANDLSM",
    "keyed":               "VANDLSM",
    "spray paint":         "VANDLSM",
    "graffiti":            "VANDLSM",
    "slashed tires":       "VANDLSM",
    "malicious":           "VANDLSM",

    # Uninsured motorist
    "uninsured motorist":  "UNINS",
    "uninsured":           "UNINS",
    "underinsured":        "UNINS",
    "um claim":            "UNINS",
    "uim claim":           "UNINS",

    # Catch-all / ambiguous
    "other":               "OTH",
    "unknown":             "OTH",
    "not sure":            "OTH",
}


def normalise_loss_cause_cd(raw: Optional[str]) -> "LossCauseCd":
    """Normalise any loss cause string to an ACORD LossCauseCd enum member.

    Precedence order within the lookup:
      1. Exact key match (after lower-strip)
      2. Substring match against any key (longest key wins to avoid
         short-key false positives like 'glass' matching 'windshield glass')
      3. Default: LossCauseCd.OTH

    Called by Claim.effective_loss_cause_cd (property) and by the
    workflow engine S1 stage to stamp loss_cause_cd at intake.

    Args:
        raw: Free-text loss_cause, platform code, or ACORD code string.
             None and empty strings resolve to OTH.
    Returns:
        LossCauseCd enum member.
    """
    if not raw:
        return LossCauseCd.OTH
    normalised = raw.lower().strip().replace("-", " ")

    # Exact match first
    hit = _LOSS_CAUSE_NORMALISE_MAP.get(normalised)
    if hit:
        return LossCauseCd(hit)

    # Substring: longest matching key wins (avoids 'glass' inside 'fiberglass')
    best_key = ""
    best_val = ""
    for key, val in _LOSS_CAUSE_NORMALISE_MAP.items():
        if key in normalised and len(key) > len(best_key):
            best_key = key
            best_val = val
    if best_val:
        return LossCauseCd(best_val)

    return LossCauseCd.OTH


# LossTypeCd derivation table  (LossCauseCd → base type before injury flag)
_CAUSE_TO_BASE_TYPE: Dict[str, str] = {
    "COLLSN":  "APD",
    "COMPRE":  "APD",
    "THEFT":   "THEFT",
    "FIRE":    "APD",
    "FLOOD":   "APD",
    "HAIL":    "APD",
    "GLASS":   "GLASS",
    "HITNRN":  "APD",
    "VANDLSM": "APD",
    "UNINS":   "PD",     # Uninsured → PD base; BI added by injury flag below
    "OTH":     "APD",
}


def derive_loss_type_cd(
    cause_cd: "LossCauseCd",
    injury_reported: bool = False,
    third_party_present: bool = False,
) -> "LossTypeCd":
    """Derive ACORD LossTypeCd from cause + claim flags.

    Logic:
      1. Look up base type from cause code.
      2. If base is THEFT or GLASS → return as-is (no BI upgrade).
      3. If injury_reported:
           APD → COMBO  (vehicle damage + bodily injury present)
           PD  → BI     (third-party / UM-UIM bodily injury)
      4. If third_party_present and not injury:
           APD → COMBO  (there IS a third party even if no injury coded yet;
                         reserve for potential late-reported BI)
         (Carrier may override to APD after investigation — POC default is
         conservative per Blueprint §S2 reservation principle.)

    Args:
        cause_cd:           ACORD LossCauseCd enum member.
        injury_reported:    Claim.injury_reported or NLP signal from S1.
        third_party_present: True when third_party_carrier or other_vehicle set.

    Returns:
        LossTypeCd enum member.
    """
    cause_str = cause_cd.value if hasattr(cause_cd, "value") else str(cause_cd)
    base = _CAUSE_TO_BASE_TYPE.get(cause_str, "APD")

    if base in ("THEFT", "GLASS"):
        return LossTypeCd(base)

    if injury_reported:
        if base == "APD":
            return LossTypeCd.COMBO
        if base == "PD":
            return LossTypeCd.BI

    if third_party_present and base == "APD":
        return LossTypeCd.COMBO   # Conservative — reserve for late-reported BI

    return LossTypeCd(base)


# ───────────────────────────────────────────────────────────────────────────
# Telematics — nested signal payload (ACORD Gap 6 — extended)
# ───────────────────────────────────────────────────────────────────────────

class TelematicsDataScopeCd(str, Enum):
    """Scope of telematics data available and permissioned for use.

    Governs which fields in TelematicsPayload the platform may ingest and
    which it must exclude from AI inputs. Drives the consent gate in S0.

    Values ranked most → least permissive:
      FULL          — Full signal set: impact, location, speed, airbag
      LOCATION_ONLY — GPS/location only; no impact or speed data
      IMPACT_ONLY   — Impact metrics only; no location transmitted
      NONE          — Consent revoked or never granted; no data usable

    Regulatory context:
      CA SB 1231, TX OCCC rules, NAIC Telematics Model Law (draft 2024):
      telematics data used in claims decisions requires explicit consent.
      NONE and LOCATION_ONLY data must not be fed into AI severity scoring.

    SOR write-back:
      Duck Creek OnDemand → Claim.TelematicsConsentScope
      Guidewire ClaimCenter → Claim.telematicsDataScope (carrier typelist)
    """
    FULL          = "FULL"
    LOCATION_ONLY = "LOCATION_ONLY"
    IMPACT_ONLY   = "IMPACT_ONLY"
    NONE          = "NONE"


class CrashNotificationSourceCd(str, Enum):
    """Source of the crash notification / telematics alert that triggered S0.

    ACORD extension field — not in ACORD 101 base standard but required by
    modern FNOL platforms integrating OEM connected-vehicle APIs (OnStar,
    FordPass, Tesla, BMW Assist) and third-party telematics (LexisNexis,
    Cambridge Mobile Telematics, Verisk Telematics).

    SOR write-back:
      Duck Creek OnDemand → Claim.CrashNotificationSource
      Guidewire ClaimCenter → Claim.crashNotificationSource
      ACORD XML → TelematicsInfo/CrashNotificationSourceCd (platform ext)
    """
    OEM             = "OEM"             # Manufacturer connected-vehicle API (OnStar, FordPass, Tesla)
    TELEMATICS_APP  = "TELEMATICS_APP"  # Insurer or third-party mobile telematics app
    IVR             = "IVR"             # Claimant self-reported via interactive voice
    MANUAL          = "MANUAL"          # Adjuster manually created S0 record
    API             = "API"             # Machine-to-machine push (ISO ClaimSearch, OEM API)
    UNKNOWN         = "UNKNOWN"         # Source not recorded


class TelematicsPayload(BaseModel):
    """Pre-FNOL signal capture from a connected vehicle / IoT device.

    Extended in ACORD Gap 6 with:
      - crash_notification_source_cd: OEM/app/IVR/manual provenance
      - telematics_data_scope: consent-gated data scope
      - oem_event_id: OEM-assigned crash event reference for audit trail
      - vehicle_speed_mph: reported speed at impact (ACORD TelematicsInfo)
      - location_lat / location_lon: GPS coordinates at impact
      - seatbelt_deployed: occupant safety system activation

    Consent gate (enforced in S0 stage_s0_pre_fnol):
      telematics_data_scope == NONE or LOCATION_ONLY:
        → impact fields EXCLUDED from AI scoring (consent_given=False path)
        → telematics_used_in_ai = False in S0 outputs
      telematics_data_scope == IMPACT_ONLY or FULL:
        → all permissioned fields flow into AI pipeline
        → telematics_used_in_ai = True

    Backward-compatibility: consent_given=False still triggers the exclusion
    path; telematics_data_scope is the more granular successor. When both are
    present, data_scope takes precedence; when scope is absent, consent_given
    governs (original S0 behavior preserved).

    SOR write-back: all fields are mapped in DuckCreekAdapter._to_dc_claim_body()
    and GuidewireAdapter._to_gw_claim_body() under TelematicsInfo sub-record.
    """
    model_config = ConfigDict(extra="ignore")

    # ── Core signals (original fields — unchanged) ────────────────────────
    crash_alert_received:   bool  = False
    delta_v_mph:            float = Field(0.0, ge=0)
    impact_severity_score:  float = Field(0.0, ge=0, le=10.0)
    airbag_deployed:        bool  = False
    consent_given:          bool  = False   # PRESERVED — original consent gate

    # ── ACORD Gap 6 — Extended telematics fields ─────────────────────────
    crash_notification_source_cd: CrashNotificationSourceCd = Field(
        default=CrashNotificationSourceCd.UNKNOWN,
        description=(
            "Source of crash notification. OEM | TELEMATICS_APP | IVR | MANUAL | API | UNKNOWN. "
            "Used in SIU fraud scoring: OEM source lowers fraud probability; "
            "MANUAL source with high impact severity raises it."
        ),
    )
    telematics_data_scope: TelematicsDataScopeCd = Field(
        default=TelematicsDataScopeCd.NONE,
        description=(
            "Consent-gated data scope. Governs which fields S0 may pass to AI. "
            "FULL | LOCATION_ONLY | IMPACT_ONLY | NONE. "
            "NONE and LOCATION_ONLY → telematics_used_in_ai=False in S0 outputs."
        ),
    )
    oem_event_id: Optional[str] = Field(
        default=None,
        description=(
            "OEM-assigned crash event identifier. Used for audit trail and "
            "subrogation evidence chain. Example: OnStar event GID-20260519-001234."
        ),
    )
    vehicle_speed_mph: Optional[float] = Field(
        default=None,
        ge=0,
        description="Reported vehicle speed at moment of impact in mph.",
    )
    location_lat: Optional[float] = Field(
        default=None,
        ge=-90.0,
        le=90.0,
        description="GPS latitude at impact point. Only used when scope permits location.",
    )
    location_lon: Optional[float] = Field(
        default=None,
        ge=-180.0,
        le=180.0,
        description="GPS longitude at impact point. Only used when scope permits location.",
    )
    seatbelt_deployed: Optional[bool] = Field(
        default=None,
        description="Seatbelt pre-tensioner activation at impact. Used for BI severity inference.",
    )

    @property
    def ai_usable(self) -> bool:
        """True when telematics data may be passed to AI scoring in S0.

        Precedence: telematics_data_scope (granular) → consent_given (binary).
        FULL and IMPACT_ONLY scopes permit AI use; LOCATION_ONLY and NONE do not.
        """
        scope = self.telematics_data_scope
        scope_val = scope.value if hasattr(scope, "value") else str(scope)
        if scope_val in (TelematicsDataScopeCd.FULL.value,
                         TelematicsDataScopeCd.IMPACT_ONLY.value):
            return True
        if scope_val in (TelematicsDataScopeCd.LOCATION_ONLY.value,
                         TelematicsDataScopeCd.NONE.value):
            return False
        # Fallback to legacy consent_given
        return bool(self.consent_given)

    @property
    def location_available(self) -> bool:
        """True when GPS coordinates are present and scope permits location use."""
        scope_val = (self.telematics_data_scope.value
                     if hasattr(self.telematics_data_scope, "value")
                     else str(self.telematics_data_scope))
        has_coords = self.location_lat is not None and self.location_lon is not None
        scope_ok = scope_val in (TelematicsDataScopeCd.FULL.value,
                                 TelematicsDataScopeCd.LOCATION_ONLY.value)
        return has_coords and scope_ok


# ───────────────────────────────────────────────────────────────────────────
# ACORD Gap 2 — Party & Role Structure models
# ───────────────────────────────────────────────────────────────────────────

class ContactInfo(BaseModel):
    """ACORD ContactInfo — structured contact block used across party types.

    Shared by ClaimsParty, WitnessParty, and AttorneyContact.
    All fields optional so partial intake (name-only at scene) remains valid.
    """
    model_config = ConfigDict(extra="ignore")

    full_name:    str
    phone:        Optional[str] = Field(None, description="Primary phone number")
    phone_type:   Optional[str] = Field(None, description="Phone | Cell | Work | Home")
    email:        Optional[str] = None
    address_line1: Optional[str] = None
    city:         Optional[str] = None
    state:        Optional[str] = None
    postal_code:  Optional[str] = None


class ClaimsParty(BaseModel):
    """ACORD 101 ClaimsParty — structured party with role code.

    Covers Named Insured, Claimant, Additional Insured roles.
    Witnesses and Passengers have their own dedicated models (see below)
    because they carry domain-specific fields (statement, seat_position)
    that do not apply to the general party concept.

    ACORD XML serialization:
      <ClaimsParty>
        <ClaimsPartyRoleCd>{role_cd}</ClaimsPartyRoleCd>
        <ContactInfo>
          <FullName>{contact.full_name}</FullName>
          ...
        </ContactInfo>
        <InjuryInd>Y|N</InjuryInd>
        <InjurySeverityCd>{injury_severity_cd}</InjurySeverityCd>
      </ClaimsParty>
    """
    model_config = ConfigDict(extra="ignore")

    role_cd:            ClaimsPartyRoleCd
    role_desc:          Optional[str] = None      # Human-readable override
    contact:            ContactInfo
    injury_ind:         bool = False
    injury_severity_cd: Optional[InjurySeverityCd] = None
    # Attorney-represented flag at the party level (ACORD 101 §7)
    attorney_represented: bool = False


class WitnessParty(BaseModel):
    """ACORD 101 §8 — Witness at the loss scene.

    Distinct from ClaimsParty to carry witness-specific fields:
    statement text and contact_consent (TCPA / state opt-in compliance).

    ACORD XML role code: ClaimsPartyRoleCd = 11 (Witness)

    contact_consent: True = witness agreed to be contacted by the carrier.
    Required in several jurisdictions before the carrier may reach out.
    """
    model_config = ConfigDict(extra="ignore")

    full_name:       str
    phone:           Optional[str] = None
    email:           Optional[str] = None
    address:         Optional[str] = None
    statement:       Optional[str] = Field(
        None,
        description="Verbatim or summarised witness statement captured at intake",
    )
    contact_consent: bool = Field(
        False,
        description="Witness consented to carrier follow-up contact (TCPA compliance)",
    )


class PassengerParty(BaseModel):
    """ACORD 101 §9 — Passenger in the insured vehicle.

    Captures each occupant for BI exposure assessment and MedPay routing.
    seat_position drives priority of BI evaluation: front-seat = higher
    impact severity potential per biomechanical studies.

    ACORD XML role code: ClaimsPartyRoleCd = 12 (Passenger)

    injury_ind / injury_severity_cd drive:
      • S3 SIU fraud scoring (injury cluster patterns)
      • S5 liability pathway (BI coverage trigger)
      • S6 payment routing (MedPay disbursement per occupant)
    """
    model_config = ConfigDict(extra="ignore")

    full_name:          str
    phone:              Optional[str] = None
    email:              Optional[str] = None
    seat_position:      SeatPositionCd = SeatPositionCd.UNKNOWN
    injury_ind:         bool = False
    injury_severity_cd: Optional[InjurySeverityCd] = None
    treatment_sought:   bool = False
    hospital_name:      Optional[str] = None


class OtherVehicleParty(BaseModel):
    """Structured adverse / third-party vehicle and party information.

    Replaces and extends the flat `third_party_carrier` /
    `third_party_policy_number` fields. The flat fields remain on Claim
    for backward-compatibility; this object is the ACORD-conformant
    representation that the XML adapter serializes as two ClaimsParty
    elements (OTHER_DRIVER + OTHER_OWNER when owner ≠ driver) plus an
    AdverseVehicle block.

    ACORD XML role codes:
      ClaimsPartyRoleCd = 16 (Other Driver)
      ClaimsPartyRoleCd = 17 (Other Owner, if different)
    """
    model_config = ConfigDict(extra="ignore")

    # Driver of the adverse vehicle
    driver_name:    Optional[str] = None
    driver_phone:   Optional[str] = None
    driver_email:   Optional[str] = None
    driver_license: Optional[str] = None
    driver_state:   Optional[str] = None

    # Owner (when different from driver — ACORD 101 requires separation)
    owner_name:     Optional[str] = None
    owner_phone:    Optional[str] = None

    # Adverse vehicle identification
    vin:            Optional[str] = None
    year:           Optional[int] = None
    make:           Optional[str] = None
    model:          Optional[str] = None
    license_plate:  Optional[str] = None
    plate_state:    Optional[str] = None
    color:          Optional[str] = None

    # Insurance coverage (extends flat third_party_carrier/policy_number)
    carrier:        Optional[str] = Field(
        None,
        description="Third-party carrier name (mirrors Claim.third_party_carrier for ACORD)",
    )
    policy_number:  Optional[str] = Field(
        None,
        description="Third-party policy number (mirrors Claim.third_party_policy_number)",
    )
    claim_number:   Optional[str] = None   # Adverse carrier's own claim reference


class AttorneyContact(BaseModel):
    """ACORD ClaimsPartyRoleCd=15 — Attorney / legal representative.

    Extends the boolean `attorney_represented` flag on Claim with the
    full ContactInfo ACORD requires. The boolean flag is PRESERVED for
    backward compat; this object is populated when attorney details are
    captured (typically at re-inspection or demand-letter stage).

    All fields optional — the flag may be set without full details at
    initial FNOL; details collected during subsequent contact.
    """
    model_config = ConfigDict(extra="ignore")

    full_name:  Optional[str] = None
    firm_name:  Optional[str] = None
    phone:      Optional[str] = None
    email:      Optional[str] = None
    fax:        Optional[str] = None
    address:    Optional[str] = None
    bar_number: Optional[str] = None
    state_bar:  Optional[str] = None   # State where bar number is held


# ───────────────────────────────────────────────────────────────────────────
# Claim — canonical end-to-end shape
# ───────────────────────────────────────────────────────────────────────────

class Claim(BaseModel):
    """Canonical claim contract. Carries everything the pipeline reads from
    intake through settlement. Runtime fields (`claim_id`, `status`, etc.)
    are set by the engine after intake validation.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=False)

    # ── Identity & lifecycle (engine-managed) ───────────────────────────
    claim_id: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    # ── ACORD 101 §1 — Core identity & claim envelope (Gap 1) ───────────
    source_channel_cd: SourceChannelCd = Field(
        default=SourceChannelCd.WEB,
        description=(
            "ACORD SourceChannelCd — coded intake channel. "
            "WEB | IVR | AGENT | MOBILE | THIRD_PARTY_API. "
            "Mapped to SourceChannel in Duck Creek, IntakeChannel in Guidewire, "
            "and SourceSystemId in ACORD XML ClaimsOccurrenceRq envelope."
        ),
    )
    intake_quality_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "ACORD IntakeQualityScore (0.0–1.0). Derived by the pipeline "
            "S0/S1 stages and surfaced as a first-class ACORD-conformant field. "
            "1.0 = all mandatory ACORD 101 fields present and validated; "
            "< 0.6 triggers HITL advisory in the governance layer."
        ),
    )

    # ── Policy ──────────────────────────────────────────────────────────
    policy_number: str = Field(..., examples=["POC-POL-00123"])

    # ── Reporter (FLAT — backward-compat, required by REQUIRED_INTAKE_FIELDS)
    # These fields are the legacy intake contract consumed by:
    #   fnol_workflow_engine.REQUIRED_INTAKE_FIELDS
    #   fnol_conversational_agent (10+ call sites)
    #   fnol_copilot_agent, fnol_governance_agent, fnol_siu_agent
    # DO NOT REMOVE. Use acord_parties() for ACORD-structured access.
    reporter_name: str
    reporter_phone: str
    reporter_email: Optional[str] = None

    # ── ACORD Gap 2 — Structured Party & Role arrays ────────────────────
    # All default to empty/None — absent from legacy payloads is valid.

    parties: List[ClaimsParty] = Field(
        default_factory=list,
        description=(
            "ACORD ClaimsParty[] — structured party array with role codes. "
            "Carries Named Insured (1), Claimant (7), Additional Insured (2). "
            "Populated at intake when role separation is captured; "
            "acord_parties() bridges flat reporter fields when this is empty."
        ),
    )

    witnesses: List[WitnessParty] = Field(
        default_factory=list,
        description=(
            "ACORD 101 §8 — Witness array. Each witness captured at scene. "
            "ClaimsPartyRoleCd=11 in ACORD XML. Empty list is valid at FNOL "
            "— witnesses added via supplemental intake or adjuster contact."
        ),
    )

    passengers: List[PassengerParty] = Field(
        default_factory=list,
        description=(
            "ACORD 101 §9 — Passenger array. Occupants of the insured vehicle. "
            "ClaimsPartyRoleCd=12 in ACORD XML. Drives BI exposure and MedPay "
            "routing in S5/S6 pipeline stages."
        ),
    )

    other_vehicle: Optional[OtherVehicleParty] = Field(
        default=None,
        description=(
            "Structured adverse / third-party vehicle and party info. "
            "Extends flat third_party_carrier / third_party_policy_number. "
            "Serialized as ClaimsPartyRoleCd=16/17 + AdverseVehicle in ACORD XML."
        ),
    )

    attorney_contact: Optional[AttorneyContact] = Field(
        default=None,
        description=(
            "ACORD ClaimsPartyRoleCd=15 — attorney contact details. "
            "Extends boolean attorney_represented. Populated when details "
            "captured; attorney_represented flag drives pipeline logic."
        ),
    )

    # ── Loss details ────────────────────────────────────────────────────
    loss_date_time: str = Field(..., examples=["2026-05-10T14:25:00Z"])
    loss_location: str
    loss_cause: str    # Free-text — PRESERVED; REQUIRED_INTAKE_FIELDS. See loss_cause_cd.
    loss_description: str
    loss_location_zip: Optional[str] = None
    location_zip: Optional[str] = None
    fatality_indicator: bool = False

    # ── ACORD Gap 3 — Loss cause & type coded fields ─────────────────────
    # Additive alongside free-text loss_cause; both co-exist.
    # loss_cause_cd is stamped by workflow engine S1 via normalise_loss_cause_cd().
    # effective_loss_cause_cd property normalises on-read when cd not yet stamped.
    loss_cause_cd: Optional[LossCauseCd] = Field(
        default=None,
        description=(
            "ACORD 101 §6 LossCauseCd — coded cause of loss. "
            "Stamped by S1 from free-text loss_cause via normalise_loss_cause_cd(). "
            "Duck Creek: Claim.CauseOfLoss · Guidewire: Claim.lossCause · "
            "ACORD XML: LossInfo/LossCauseCd."
        ),
    )
    loss_type_cd: Optional[LossTypeCd] = Field(
        default=None,
        description=(
            "ACORD LossTypeCd — coverage pathway indicator. "
            "Derived from loss_cause_cd + injury/third-party flags in S1. "
            "Drives S2 reserve calculation and S6 payment routing. "
            "APD | BI | PD | THEFT | GLASS | COMBO. "
            "Duck Creek: Claim.ClaimType · Guidewire: Claim.lossType."
        ),
    )

    # ── ACORD Gap 3 — Police report ──────────────────────────────────────
    # ACORD 101 §6 requires police report capture at FNOL when filed.
    # Blueprint §A2 references policeReportRef in stage outputs but the
    # Claim model previously had no intake fields for it.
    police_report_filed: bool = Field(
        default=False,
        description="True when a police report was filed at the scene or shortly after.",
    )
    police_report_number: Optional[str] = Field(
        default=None,
        description=(
            "ACORD 101 §6 PoliceReport.ReportNumber — agency-assigned report number. "
            "Required for subrogation pathway (S7) when third-party fault."
        ),
    )
    police_report_agency: Optional[str] = Field(
        default=None,
        description=(
            "ACORD 101 §6 PoliceReport.AgencyName — law enforcement agency name "
            "(e.g. 'Austin Police Department', 'Travis County Sheriff')."
        ),
    )
    police_report_agency_phone: Optional[str] = Field(
        default=None,
        description="ACORD 101 §6 PoliceReport.AgencyPhone — agency records desk phone.",
    )

    # ── ACORD Gap 3 — Conditions at loss (ACORD 101 §6, low priority) ────
    # Relevant for fraud scoring (weather alibi validation) and subrogation.
    # Coded as strings rather than enums — carrier-specific vocabularies vary.
    weather_condition_cd: Optional[str] = Field(
        default=None,
        description=(
            "ACORD WeatherConditionCd at time of loss. "
            "Accepted values: CLEAR | RAIN | SNOW | FOG | ICE | WIND | UNKNOWN. "
            "Used in SIU fraud scoring (S3/S4A) to validate weather-alibi claims."
        ),
    )
    road_condition_cd: Optional[str] = Field(
        default=None,
        description=(
            "ACORD RoadConditionCd at time of loss. "
            "Accepted values: DRY | WET | ICY | SNOWY | GRAVEL | CONSTRUCTION | UNKNOWN. "
            "Relevant for comparative fault analysis in S5 and subrogation in S7."
        ),
    )

    # ── Vehicle ─────────────────────────────────────────────────────────
    vin: Optional[str] = None
    vehicle_year: Optional[int] = None
    vehicle_make: Optional[str] = None
    vehicle_model: Optional[str] = None
    vehicle_mileage: Optional[int] = None
    vehicle_condition: str = "AVERAGE"
    vehicle_options: List[str] = Field(default_factory=list)
    vehicle_class: str = "STD"
    vehicle_state: Optional[str] = None
    state: Optional[str] = None                   # jurisdiction override

    # vehicle_acv_usd: FIXED default — None is the honest sentinel for
    # "not yet captured". The previous Field(0.0) caused a zero-ACV →
    # MODERATE total-loss routing bug: base/acv with acv=0 → ZeroDivisionError
    # or forced-False tl, silently misclassifying every claim where ACV was
    # simply not provided at intake. The workflow engine S4B already guards
    # `if acv <= 0` but a 0.0 default meant legacy payloads never hit the
    # guard — they had a valid (wrong) float. None forces the guard to fire.
    vehicle_acv_usd: Optional[float] = Field(
        default=None,
        ge=0,
        description=(
            "Actual Cash Value of the insured vehicle in USD. "
            "None = not captured (MISSING) — TL determination deferred. "
            "Do NOT default to 0 or any placeholder in new code. "
            "Use acv_source_cd to record provenance."
        ),
    )
    vehicle_recall_indicator: bool = False
    primary_damage_area: Optional[str] = None     # Free-text — PRESERVED; see damage_area_cd
    deductible_usd: float = Field(
        500.0,
        ge=0,
        description=(
            "Global claim deductible in USD (intake-side hint). "
            "Per-coverage deductibles are in claimant_asserted_coverages[].deductible_usd; "
            "S2 adjudicated deductible is in context['S2']['deductible_collision']. "
            "This flat field remains for backward-compat with legacy payloads."
        ),
    )

    # ── ACORD Gap 4 — Vehicle identification & damage coding ─────────────
    # Additive fields; all Optional with None defaults — legacy payloads valid.

    damage_area_cd: Optional[VehicleDamageAreaCd] = Field(
        default=None,
        description=(
            "ACORD 101 §5 VehicleDamageAreaCd — coded primary damage area. "
            "Stamped by S4B damage assessment or at intake when structured. "
            "effective_damage_area_cd normalises free-text primary_damage_area "
            "on-read when this is not set. "
            "FRT | REAR | LFTSD | RGTSD | ROOF | UNDRBD | INTRNL | ALL | UNKNWN. "
            "Duck Creek: Claim.DamageArea · Guidewire: VehicleIncident.impactType."
        ),
    )
    license_plate: Optional[str] = Field(
        default=None,
        description=(
            "ACORD 101 §5 LicensePlateNumber — insured vehicle plate. "
            "Used for DMV cross-check (VIN ↔ plate ↔ registrant validation), "
            "alternate VIN lookup when VIN is unknown, and fraud scoring "
            "(plate history vs. VIN history mismatch is a SIU signal)."
        ),
    )
    registration_state: Optional[str] = Field(
        default=None,
        description=(
            "ACORD 101 §5 LicensePlateStateCd — 2-char state/province code "
            "for the insured vehicle's registration. "
            "May differ from loss jurisdiction (vehicle_state / state) and "
            "garaging state in the governance demographics cache. "
            "Required for DMV validation and some state SIU referral workflows."
        ),
    )

    # ── ACORD Gap 5 — Coverage & Financial Fields ─────────────────────────

    acv_source_cd: AcvSourceCd = Field(
        default=AcvSourceCd.MISSING,
        description=(
            "Source of vehicle_acv_usd. MISSING is the honest default. "
            "Governs whether S4B may make a TL determination or must defer. "
            "Set to CLAIMANT when insured provides ACV at intake; "
            "GUIDEBOOK after NADA/KBB/CCC lookup by adjuster or automated step."
        ),
    )

    claimant_asserted_coverages: List[ClaimantCoverage] = Field(
        default_factory=list,
        description=(
            "ACORD 101 §3 — coverages the claimant asserts apply at intake. "
            "Not the policy truth — S2 adjudicates actual coverage. "
            "Each entry carries CoverageCd + per-coverage deductible + limit. "
            "Empty list is valid; S2 populates adjudicated coverages in context. "
            "Serialised as <CoverageInfo> blocks in ACORD XML."
        ),
    )

    ror_trigger_cds: List[RorTriggerCd] = Field(
        default_factory=list,
        description=(
            "Coded ROR triggers captured at intake or stamped by S2. "
            "Extends the free-text ror_letter_text in S2 outputs. "
            "Enables programmatic ROR routing without text parsing. "
            "POLICY_LAPSE | EXCLUSION | COVERAGE_DISPUTE | LATE_NOTICE | "
            "FRAUD_SUSPECTED | SUBROGATION | ATTORNEY_INVOLVED | "
            "MULTI_CLAIMANT | EUO_REQUIRED."
        ),
    )

    # ── Damage estimate (intake-side hint; S4B is authoritative) ────────
    estimated_loss_usd: float = Field(0.0, ge=0)
    prior_damage_usd: float = Field(0.0, ge=0)
    photo_count: int = Field(0, ge=0)
    photo_quality_score: float = Field(0.0, ge=0, le=1.0)
    drivable_indicator: bool = True

    # ── Injury & liability ──────────────────────────────────────────────
    injury_reported: bool = False
    injury_severity: Optional[str] = None
    treatment_outlier: bool = False
    liability_clear: bool = True
    rear_ended_other: bool = False
    rear_ended_by_other: bool = False
    attorney_represented: bool = False   # FLAT flag — preserved for engine logic

    # ── Third party (FLAT — preserved for backward compat) ──────────────
    # Structured equivalent: other_vehicle.carrier / other_vehicle.policy_number
    third_party_carrier: Optional[str] = None
    third_party_policy_number: Optional[str] = None

    # ── Fraud signals ───────────────────────────────────────────────────
    prior_claims_count: int = Field(0, ge=0)
    iso_match: bool = False
    policy_tenure_days: int = Field(365, ge=0)
    seed_fraud: bool = False                      # POC demo toggle

    # ── Telematics ──────────────────────────────────────────────────────
    telematics: Optional[TelematicsPayload] = None

    # ── Convenience accessors ───────────────────────────────────────────

    @property
    def effective_state(self) -> str:
        """Jurisdiction with the cascade the agents use throughout:
        explicit `state` → `vehicle_state` → 'TX' (POC default)."""
        return (self.state or self.vehicle_state or "TX").upper()

    @property
    def effective_zip(self) -> str:
        """Salvage adapter's preferred ZIP source with documented fallback."""
        return (self.location_zip or self.loss_location_zip or "00000")

    @property
    def effective_damage_area_cd(self) -> str:
        """Return ACORD VehicleDamageAreaCd string value for this claim.

        Precedence:
          1. Explicit damage_area_cd (stamped by S4B / structured intake) → .value
          2. Normalise free-text primary_damage_area via normalise_damage_area_cd()

        Always returns a string; resolves to 'UNKNWN' when unresolvable.
        Used by the ACORD XML adapter as its single call-site — eliminates the
        adapter's local _DAMAGE_AREA_MAP entirely (same pattern as Gap 3
        effective_loss_cause_cd).

        Example:
            claim.primary_damage_area = "driver side"   # no damage_area_cd set
            claim.effective_damage_area_cd               # → "LFTSD"

            claim.damage_area_cd = VehicleDamageAreaCd.REAR
            claim.effective_damage_area_cd               # → "REAR"
        """
        if self.damage_area_cd is not None:
            return (self.damage_area_cd.value
                    if hasattr(self.damage_area_cd, "value")
                    else str(self.damage_area_cd))
        return normalise_damage_area_cd(self.primary_damage_area).value

    @property
    def effective_acv_usd(self) -> Optional[float]:
        """Return the vehicle ACV or None when not yet captured.

        Replaces the pre-Gap-5 pattern of `claim.vehicle_acv_usd or 0` which
        silently treated a missing ACV as zero and routed the claim to the
        MODERATE bucket rather than deferring TL determination.

        Consumers MUST check for None before making TL/reserve decisions:

            acv = claim.effective_acv_usd
            if acv is None:
                advisories.append("ACV missing — TL deferred")
                tl = False
            else:
                tl = (damage / acv) >= TL_RATIO

        The workflow engine S4B is patched to use this property.
        ACV from claimant_asserted_coverages is NOT used here — those are
        per-coverage limits, not the vehicle market value.
        """
        v = self.vehicle_acv_usd
        if v is None or (isinstance(v, float) and v <= 0.0):
            return None
        return float(v)

    @property
    def effective_deductible_usd(self) -> float:
        """Return the best available collision deductible at intake.

        Precedence:
          1. claimant_asserted_coverages COLL entry deductible_usd
          2. Flat deductible_usd field (legacy / global)
          3. 0.0 (no deductible claimed — not the same as unknown)

        S2 always overrides this with the policy-verified deductible from
        context['S2']['deductible_collision']. This property is only used
        for pre-S2 advisories and intake quality scoring.
        """
        for cov in self.claimant_asserted_coverages:
            ccd = cov.coverage_cd.value if hasattr(cov.coverage_cd, "value") else str(cov.coverage_cd)
            if ccd == "COLL" and cov.deductible_usd is not None:
                return float(cov.deductible_usd)
        return float(self.deductible_usd or 0.0)

    @property
    def effective_loss_cause_cd(self) -> str:
        """Return ACORD LossCauseCd string value for this claim.

        Precedence:
          1. Explicit loss_cause_cd (stamped by S1) → return its .value
          2. Normalise free-text loss_cause via normalise_loss_cause_cd()

        Always returns a string (never None); resolves to 'OTH' when the
        cause cannot be coded. Used by the ACORD XML adapter as its single
        call-site — eliminates the adapter's local normalisation map.

        Example:
            claim.loss_cause = "rear-ended"   # no loss_cause_cd set
            claim.effective_loss_cause_cd     # → "COLLSN"

            claim.loss_cause_cd = LossCauseCd.HAIL
            claim.effective_loss_cause_cd     # → "HAIL"
        """
        if self.loss_cause_cd is not None:
            return (self.loss_cause_cd.value
                    if hasattr(self.loss_cause_cd, "value")
                    else str(self.loss_cause_cd))
        return normalise_loss_cause_cd(self.loss_cause).value

    # ── ACORD Gap 2 — Party bridge ───────────────────────────────────────

    def acord_parties(self) -> List[ClaimsParty]:
        """Build the canonical ACORD ClaimsParty[] list.

        Merge strategy (precedence: structured > flat):
          1. If self.parties is populated, use it as-is (structured intake).
          2. Synthesise a Named Insured party from flat reporter_* fields
             so every claim always has at least one ACORD party record.
          3. Append attorney as ClaimsPartyRoleCd=15 when attorney_contact
             is set, or attorney_represented=True with no contact detail.

        The witness[], passenger[], and other_vehicle entries are NOT
        included here (they have distinct ACORD elements in the XML).
        Call them directly from the ACORD XML adapter.

        Returns:
            List[ClaimsParty] — always at least one element (the reporter
            bridged as Named Insured when no structured parties exist).
        """
        result: List[ClaimsParty] = list(self.parties)  # copy; don't mutate

        # Bridge flat reporter fields → Named Insured party when no structured
        # parties are present, or when no NAMED_INSURED role exists yet.
        has_named_insured = any(
            p.role_cd == ClaimsPartyRoleCd.NAMED_INSURED for p in result
        )
        if not has_named_insured and self.reporter_name:
            result.insert(0, ClaimsParty(
                role_cd=ClaimsPartyRoleCd.NAMED_INSURED,
                role_desc="Bridged from flat reporter fields — Phase 1 intake",
                contact=ContactInfo(
                    full_name=self.reporter_name,
                    phone=self.reporter_phone or None,
                    email=self.reporter_email,
                ),
                injury_ind=self.injury_reported,
                injury_severity_cd=(
                    InjurySeverityCd(self.injury_severity.upper())
                    if self.injury_severity and self.injury_severity.upper()
                    in InjurySeverityCd._value2member_map_
                    else None
                ),
                attorney_represented=self.attorney_represented,
            ))

        # Attorney party — structured contact takes precedence over boolean flag
        has_attorney = any(
            p.role_cd == ClaimsPartyRoleCd.ATTORNEY for p in result
        )
        if not has_attorney and (
            self.attorney_contact or self.attorney_represented
        ):
            ac = self.attorney_contact
            result.append(ClaimsParty(
                role_cd=ClaimsPartyRoleCd.ATTORNEY,
                role_desc="Attorney / legal representative",
                contact=ContactInfo(
                    full_name=ac.full_name or "Attorney — details pending",
                    phone=ac.phone,
                    email=ac.email,
                ) if ac else ContactInfo(
                    full_name="Attorney — details pending FNOL",
                ),
                injury_ind=False,
            ))

        return result

    @property
    def all_injured_parties(self) -> List[Dict[str, Any]]:
        """Flat list of all parties with injury_ind=True across all arrays.

        Used by S5 liability and S6 payment stages to enumerate BI exposure.
        Returns dicts rather than model instances for dict-based pipeline compat.
        """
        injured: List[Dict[str, Any]] = []

        # Named insured / claimant
        for p in self.acord_parties():
            if p.injury_ind:
                injured.append({
                    "role":     p.role_cd.value,
                    "name":     p.contact.full_name,
                    "severity": p.injury_severity_cd.value if p.injury_severity_cd else None,
                })

        # Passengers
        for p in self.passengers:
            if p.injury_ind:
                injured.append({
                    "role":     ClaimsPartyRoleCd.PASSENGER.value,
                    "name":     p.full_name,
                    "severity": p.injury_severity_cd.value if p.injury_severity_cd else None,
                    "seat":     p.seat_position.value,
                })

        # Flat injury_reported bridge (when no structured parties yet)
        if not injured and self.injury_reported and self.reporter_name:
            injured.append({
                "role":     ClaimsPartyRoleCd.NAMED_INSURED.value,
                "name":     self.reporter_name,
                "severity": (self.injury_severity or "").upper() or None,
            })

        return injured

    def with_runtime(self, **updates: Any) -> "Claim":
        """Return a copy with runtime fields applied. Use this from the
        engine when stamping `claim_id`, `status`, `created_at`."""
        return self.model_copy(update=updates)

    def to_sor_payload(self) -> Dict[str, Any]:
        """Serialise for the SOR adapter boundary. Drops None entries to
        keep the SOR record compact (the adapter merges-with-existing on
        update, so absent fields mean 'no change').

        source_channel_cd is always emitted (enum serialised as string value)
        so SOR adapters can write it to the carrier SOR without a None-guard.

        Structured party arrays are serialised as nested dicts; SOR adapters
        that don't consume them yet will silently ignore the extra keys.
        """
        raw = self.model_dump(mode="python")

        # SourceChannelCd enum → string value
        if isinstance(raw.get("source_channel_cd"), SourceChannelCd):
            raw["source_channel_cd"] = raw["source_channel_cd"].value

        # LossCauseCd / LossTypeCd enums → string values (Gap 3)
        if isinstance(raw.get("loss_cause_cd"), LossCauseCd):
            raw["loss_cause_cd"] = raw["loss_cause_cd"].value
        if isinstance(raw.get("loss_type_cd"), LossTypeCd):
            raw["loss_type_cd"] = raw["loss_type_cd"].value

        # VehicleDamageAreaCd enum → string value (Gap 4)
        if isinstance(raw.get("damage_area_cd"), VehicleDamageAreaCd):
            raw["damage_area_cd"] = raw["damage_area_cd"].value

        # Gap 5 — Coverage & Financial enums → string values
        if isinstance(raw.get("acv_source_cd"), AcvSourceCd):
            raw["acv_source_cd"] = raw["acv_source_cd"].value

        for cov in raw.get("claimant_asserted_coverages", []):
            if isinstance(cov.get("coverage_cd"), CoverageCd):
                cov["coverage_cd"] = cov["coverage_cd"].value

        raw["ror_trigger_cds"] = [
            (t.value if hasattr(t, "value") else str(t))
            for t in raw.get("ror_trigger_cds", [])
        ] or raw.get("ror_trigger_cds", [])

        # ClaimsPartyRoleCd enums within parties[] → string values
        for party in raw.get("parties", []):
            if isinstance(party.get("role_cd"), ClaimsPartyRoleCd):
                party["role_cd"] = party["role_cd"].value
            if isinstance(party.get("injury_severity_cd"), InjurySeverityCd):
                party["injury_severity_cd"] = party["injury_severity_cd"].value

        # Passenger injury severity enums
        for pax in raw.get("passengers", []):
            if isinstance(pax.get("injury_severity_cd"), InjurySeverityCd):
                pax["injury_severity_cd"] = pax["injury_severity_cd"].value
            if isinstance(pax.get("seat_position"), SeatPositionCd):
                pax["seat_position"] = pax["seat_position"].value

        return {k: v for k, v in raw.items() if v is not None}
