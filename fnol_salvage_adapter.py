"""
FNOL Intelligence Platform — Salvage Vendor Adapter
====================================================
Pluggable adapter pattern for total-loss salvage assignment, mirroring the
SOR adapter design. Adapters: Copart, IAA (Insurance Auto Auctions), Mock.

Public interface (every adapter implements these):
  - vendor_name() -> str
  - assign(request: SalvageAssignmentRequest) -> SalvageAssignmentResponse
  - status(vendor_lot_id: str) -> Dict[str, Any]
  - cancel(vendor_lot_id: str, reason: str) -> Dict[str, Any]

The Copart and Guidewire adapters are interface shells — wire real REST/EDI
endpoints in production. The Mock adapter ships deterministic, realistic
returns suitable for demos, smoke tests, and carrier evaluation.

Environment:
  SALVAGE_VENDOR        : copart | iaa | mock | auto    (default: auto -> mock)
  COPART_API_BASE_URL   : Copart partner API base
  COPART_API_KEY        : Copart partner API key
  IAA_API_BASE_URL      : IAA partner API base
  IAA_API_KEY           : IAA partner API key
"""

from __future__ import annotations
import os
import random
import time
import uuid
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional


# ───────────────────────────────────────────────────────────────────────────
# Data contracts
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class SalvageAssignmentRequest:
    claim_id: str
    vin: str
    year: int
    make: str
    model: str
    trim: Optional[str] = None
    mileage: Optional[int] = None
    acv_usd: float = 0.0
    damage_severity: str = "MODERATE"          # LIGHT | MODERATE | SEVERE | TOTAL
    drivable: bool = False
    primary_damage_area: str = "FRONT"         # FRONT | REAR | SIDE | ROOF | UNDERCARRIAGE | INTERIOR
    title_brand: str = "SALVAGE"               # SALVAGE | NON_REPAIRABLE | FLOOD | HAIL
    location_zip: str = "00000"
    photo_count: int = 0
    prior_damage_disclosed: bool = False
    notes: str = ""


@dataclass
class SalvageAssignmentResponse:
    vendor: str
    vendor_lot_id: str
    yard_location: str
    yard_address: str
    pickup_eta_days: int
    expected_sale_date: str               # ISO date
    expected_gross_return_usd: float
    vendor_fees_usd: float
    expected_net_return_usd: float
    salvage_recovery_pct: float           # net/ACV, useful for reporting
    assignment_status: str = "ASSIGNED"
    confidence: float = 0.0
    rationale: str = ""
    raw_vendor_payload: Dict[str, Any] = field(default_factory=dict)


# ───────────────────────────────────────────────────────────────────────────
# Mock vendor (always available, deterministic)
# ───────────────────────────────────────────────────────────────────────────

# Realistic salvage recovery percentages by damage severity and primary damage
# area. POC tunable — production should load from carrier-managed reference
# table or vendor historical-return analytics.
_RECOVERY_PCT_TABLE = {
    # severity:        front  rear   side   roof   under  interior
    "LIGHT":     {"FRONT": 0.42, "REAR": 0.45, "SIDE": 0.40, "ROOF": 0.30, "UNDERCARRIAGE": 0.28, "INTERIOR": 0.35},
    "MODERATE":  {"FRONT": 0.32, "REAR": 0.34, "SIDE": 0.30, "ROOF": 0.22, "UNDERCARRIAGE": 0.20, "INTERIOR": 0.25},
    "SEVERE":    {"FRONT": 0.22, "REAR": 0.24, "SIDE": 0.20, "ROOF": 0.15, "UNDERCARRIAGE": 0.12, "INTERIOR": 0.18},
    "TOTAL":     {"FRONT": 0.15, "REAR": 0.16, "SIDE": 0.14, "ROOF": 0.10, "UNDERCARRIAGE": 0.08, "INTERIOR": 0.12},
}

# Vendor fee schedules (POC). Real fees vary by vendor contract terms.
_VENDOR_FEES = {
    "MOCK":   {"flat": 75,   "pct_of_gross": 0.085},
    "COPART": {"flat": 95,   "pct_of_gross": 0.090},
    "IAA":    {"flat": 95,   "pct_of_gross": 0.090},
}

# Mock yard network — major metros, used to pick the nearest yard by ZIP prefix.
_MOCK_YARDS = {
    "COPART": [
        {"yard_id": "CPRT-HOU-N08", "name": "Copart Houston North", "address": "8521 Will Clayton Pkwy, Humble, TX 77338", "zip_prefix": ("77", "78", "79")},
        {"yard_id": "CPRT-DFW-W12", "name": "Copart Dallas West",   "address": "204 Tom Berry Dr, Grand Prairie, TX 75051", "zip_prefix": ("75", "76")},
        {"yard_id": "CPRT-ATL-S04", "name": "Copart Atlanta South", "address": "1090 Bohannon Rd, Fairburn, GA 30213",      "zip_prefix": ("30", "31", "39")},
        {"yard_id": "CPRT-LAX-E07", "name": "Copart Los Angeles",   "address": "13035 Pierce St, Pacoima, CA 91331",        "zip_prefix": ("90", "91", "92", "93")},
        {"yard_id": "CPRT-MIA-N03", "name": "Copart Miami North",   "address": "13301 NW 79th Ave, Hialeah, FL 33018",      "zip_prefix": ("33", "34")},
        {"yard_id": "CPRT-NYC-J02", "name": "Copart NY Newburgh",   "address": "9 Schneider Ln, Newburgh, NY 12550",        "zip_prefix": ("10", "11", "12")},
    ],
    "IAA": [
        {"yard_id": "IAA-HOU-S05",  "name": "IAA Houston South",     "address": "2535 W Mt Houston Rd, Houston, TX 77038",   "zip_prefix": ("77", "78", "79")},
        {"yard_id": "IAA-DFW-N02",  "name": "IAA Dallas North",      "address": "204 N Loop 12, Irving, TX 75061",           "zip_prefix": ("75", "76")},
        {"yard_id": "IAA-ATL-C09",  "name": "IAA Atlanta Central",   "address": "1930 Lakewood Way SW, Atlanta, GA 30315",   "zip_prefix": ("30", "31", "39")},
        {"yard_id": "IAA-LAX-W14",  "name": "IAA Los Angeles",       "address": "8910 Bissonet, South Gate, CA 90280",       "zip_prefix": ("90", "91", "92", "93")},
        {"yard_id": "IAA-MIA-W08",  "name": "IAA Miami West",        "address": "11400 NW 32nd Ave, Miami, FL 33167",        "zip_prefix": ("33", "34")},
        {"yard_id": "IAA-NYC-K06",  "name": "IAA New York",          "address": "601 Edward H. Ross Dr, Elmwood Park, NJ 07407", "zip_prefix": ("10", "11", "12", "07")},
    ],
    "MOCK": [
        {"yard_id": "MOCK-YARD-01", "name": "Mock Salvage Yard",     "address": "1 Demo Plaza, Mockville, USA",              "zip_prefix": tuple()},
    ],
}


def _pick_yard(vendor: str, zip_code: str) -> Dict[str, str]:
    yards = _MOCK_YARDS.get(vendor, _MOCK_YARDS["MOCK"])
    if not zip_code or len(zip_code) < 2:
        return yards[0]
    prefix = zip_code[:2]
    for y in yards:
        if prefix in y.get("zip_prefix", ()):
            return y
    return yards[0]


def _compute_return(req: SalvageAssignmentRequest, vendor: str) -> Dict[str, float]:
    sev = req.damage_severity if req.damage_severity in _RECOVERY_PCT_TABLE else "MODERATE"
    area = req.primary_damage_area if req.primary_damage_area in _RECOVERY_PCT_TABLE[sev] else "FRONT"
    base_pct = _RECOVERY_PCT_TABLE[sev][area]

    # Title brand adjustment
    if req.title_brand == "NON_REPAIRABLE":
        base_pct *= 0.55                              # parts-only value
    elif req.title_brand == "FLOOD":
        base_pct *= 0.65
    elif req.title_brand == "HAIL":
        base_pct *= 1.05                              # often cosmetic, higher recovery

    # Drivability bonus
    if req.drivable:
        base_pct *= 1.08

    # Photo / disclosure premium (well-documented lots sell better)
    if req.photo_count >= 12 and req.prior_damage_disclosed:
        base_pct *= 1.04

    gross = round(req.acv_usd * base_pct, 2)
    fees_schedule = _VENDOR_FEES.get(vendor, _VENDOR_FEES["MOCK"])
    fees = round(fees_schedule["flat"] + gross * fees_schedule["pct_of_gross"], 2)
    net = round(max(gross - fees, 0.0), 2)
    pct_net = round(net / max(req.acv_usd, 1.0), 4)
    return {
        "expected_gross_return_usd": gross,
        "vendor_fees_usd": fees,
        "expected_net_return_usd": net,
        "salvage_recovery_pct": pct_net,
        "base_recovery_pct": round(base_pct, 4),
    }


def _expected_sale_date(pickup_days: int) -> str:
    # Typical: pickup + 21–35 days to auction
    import datetime as dt
    target = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=pickup_days + 28)
    return target.date().isoformat()


# ───────────────────────────────────────────────────────────────────────────
# Adapter implementations
# ───────────────────────────────────────────────────────────────────────────

class MockSalvageAdapter:
    NAME = "MOCK"

    def vendor_name(self) -> str:
        return self.NAME

    def assign(self, req: SalvageAssignmentRequest) -> SalvageAssignmentResponse:
        yard = _pick_yard("MOCK", req.location_zip)
        ret = _compute_return(req, "MOCK")
        pickup_eta = 2 if req.drivable else 4
        lot_id = f"MOCK-LOT-{int(time.time())}-{uuid.uuid4().hex[:5].upper()}"
        return SalvageAssignmentResponse(
            vendor=self.NAME,
            vendor_lot_id=lot_id,
            yard_location=yard["name"],
            yard_address=yard["address"],
            pickup_eta_days=pickup_eta,
            expected_sale_date=_expected_sale_date(pickup_eta),
            expected_gross_return_usd=ret["expected_gross_return_usd"],
            vendor_fees_usd=ret["vendor_fees_usd"],
            expected_net_return_usd=ret["expected_net_return_usd"],
            salvage_recovery_pct=ret["salvage_recovery_pct"],
            assignment_status="ASSIGNED",
            confidence=0.90,
            rationale=(
                f"severity={req.damage_severity}, area={req.primary_damage_area}, "
                f"brand={req.title_brand}, drivable={req.drivable}, "
                f"recovery={ret['base_recovery_pct']*100:.1f}% of ACV ${req.acv_usd:,.0f}"
            ),
            raw_vendor_payload={"mock": True, "yard": yard},
        )

    def status(self, vendor_lot_id: str) -> Dict[str, Any]:
        # Deterministic mock: progresses through states based on a stable hash
        # of the lot id. The previous `int(..., 16)` raised ValueError on any
        # lot whose tail contained non-hex letters (G–Z), 500-ing the status
        # endpoint. Use SHA-256 mod N for a total function over any string.
        import hashlib
        h = int(hashlib.sha256((vendor_lot_id or "").encode()).hexdigest(), 16)
        states = ["ASSIGNED", "PICKED_UP", "AT_YARD", "TITLE_PROCESSED", "LISTED", "SOLD"]
        return {"vendor_lot_id": vendor_lot_id, "status": states[h % len(states)], "as_of": time.time()}

    def cancel(self, vendor_lot_id: str, reason: str) -> Dict[str, Any]:
        return {"vendor_lot_id": vendor_lot_id, "status": "CANCELLED", "reason": reason}


class CopartAdapter:
    """Copart partner API adapter — interface shell.

    Production: wire to Copart's Salvage Bidder & Member APIs. For carriers,
    Copart provides an EDI 906/810/820 flow and a REST partner endpoint for
    high-volume assignments. Keep the request/response contract identical to
    SalvageAssignmentRequest/Response so the agent code remains adapter-agnostic.
    """
    NAME = "COPART"

    def __init__(self) -> None:
        self.base_url = os.getenv("COPART_API_BASE_URL", "")
        self.api_key = os.getenv("COPART_API_KEY", "")
        self._fallback = MockSalvageAdapter()

    def vendor_name(self) -> str:
        return self.NAME

    def assign(self, req: SalvageAssignmentRequest) -> SalvageAssignmentResponse:
        if not (self.base_url and self.api_key):
            # Shell mode: simulate Copart-shaped response via mock math + Copart fees/yards.
            yard = _pick_yard("COPART", req.location_zip)
            ret = _compute_return(req, "COPART")
            pickup_eta = 3 if req.drivable else 5
            lot_id = f"CPRT-{int(time.time())}-{uuid.uuid4().hex[:5].upper()}"
            return SalvageAssignmentResponse(
                vendor=self.NAME, vendor_lot_id=lot_id,
                yard_location=yard["name"], yard_address=yard["address"],
                pickup_eta_days=pickup_eta,
                expected_sale_date=_expected_sale_date(pickup_eta),
                expected_gross_return_usd=ret["expected_gross_return_usd"],
                vendor_fees_usd=ret["vendor_fees_usd"],
                expected_net_return_usd=ret["expected_net_return_usd"],
                salvage_recovery_pct=ret["salvage_recovery_pct"],
                assignment_status="ASSIGNED",
                confidence=0.88,
                rationale=f"copart shell mode — yard={yard['yard_id']}, recovery={ret['base_recovery_pct']*100:.1f}%",
                raw_vendor_payload={"shell": True, "yard": yard, "would_call": f"{self.base_url}/v1/assignments"},
            )
        # TODO production: requests.post(f"{self.base_url}/v1/assignments", ...)
        raise NotImplementedError("Copart live API integration is a production engineering task.")

    def status(self, vendor_lot_id: str) -> Dict[str, Any]:
        return self._fallback.status(vendor_lot_id)

    def cancel(self, vendor_lot_id: str, reason: str) -> Dict[str, Any]:
        return self._fallback.cancel(vendor_lot_id, reason)


class IAAAdapter:
    """IAA (Insurance Auto Auctions) partner API adapter — interface shell.

    Production: wire to IAA's Buyer & Seller APIs. IAA also provides EDI and
    a REST partner endpoint. Many carriers split between Copart and IAA based
    on yard density and vehicle class — A11's vendor='auto' mode optimises
    this selection automatically.
    """
    NAME = "IAA"

    def __init__(self) -> None:
        self.base_url = os.getenv("IAA_API_BASE_URL", "")
        self.api_key = os.getenv("IAA_API_KEY", "")
        self._fallback = MockSalvageAdapter()

    def vendor_name(self) -> str:
        return self.NAME

    def assign(self, req: SalvageAssignmentRequest) -> SalvageAssignmentResponse:
        if not (self.base_url and self.api_key):
            yard = _pick_yard("IAA", req.location_zip)
            ret = _compute_return(req, "IAA")
            pickup_eta = 2 if req.drivable else 4
            lot_id = f"IAA-{int(time.time())}-{uuid.uuid4().hex[:5].upper()}"
            return SalvageAssignmentResponse(
                vendor=self.NAME, vendor_lot_id=lot_id,
                yard_location=yard["name"], yard_address=yard["address"],
                pickup_eta_days=pickup_eta,
                expected_sale_date=_expected_sale_date(pickup_eta),
                expected_gross_return_usd=ret["expected_gross_return_usd"],
                vendor_fees_usd=ret["vendor_fees_usd"],
                expected_net_return_usd=ret["expected_net_return_usd"],
                salvage_recovery_pct=ret["salvage_recovery_pct"],
                assignment_status="ASSIGNED",
                confidence=0.88,
                rationale=f"iaa shell mode — yard={yard['yard_id']}, recovery={ret['base_recovery_pct']*100:.1f}%",
                raw_vendor_payload={"shell": True, "yard": yard, "would_call": f"{self.base_url}/v1/assignments"},
            )
        raise NotImplementedError("IAA live API integration is a production engineering task.")

    def status(self, vendor_lot_id: str) -> Dict[str, Any]:
        return self._fallback.status(vendor_lot_id)

    def cancel(self, vendor_lot_id: str, reason: str) -> Dict[str, Any]:
        return self._fallback.cancel(vendor_lot_id, reason)


# ───────────────────────────────────────────────────────────────────────────
# Vendor resolver — supports 'auto' for best-net-return optimisation
# ───────────────────────────────────────────────────────────────────────────

class _AutoSalvageAdapter:
    """Pseudo-adapter for 'auto' mode that runs shadow quotes against all
    configured vendors via `best_vendor_for` and picks the highest net return.
    Exposes the same surface as a concrete adapter so callers don't have to
    know whether they got `MockSalvageAdapter` or the auto-selector."""

    NAME = "AUTO"

    def vendor_name(self) -> str:
        return self.NAME

    def assign(self, req: "SalvageAssignmentRequest") -> "SalvageAssignmentResponse":
        return best_vendor_for(req)

    def status(self, vendor_lot_id: str) -> Dict[str, Any]:
        # The lot id encodes the actual vendor (MOCK-LOT-…, COPART-LOT-…,
        # IAA-LOT-…). Route the status request to the adapter that owns it.
        prefix = (vendor_lot_id or "").split("-", 1)[0].lower()
        if prefix in ("copart",):
            return CopartAdapter().status(vendor_lot_id)
        if prefix in ("iaa",):
            return IAAAdapter().status(vendor_lot_id)
        return MockSalvageAdapter().status(vendor_lot_id)

    def cancel(self, vendor_lot_id: str, reason: str) -> Dict[str, Any]:
        prefix = (vendor_lot_id or "").split("-", 1)[0].lower()
        if prefix in ("copart",):
            return CopartAdapter().cancel(vendor_lot_id, reason)
        if prefix in ("iaa",):
            return IAAAdapter().cancel(vendor_lot_id, reason)
        return MockSalvageAdapter().cancel(vendor_lot_id, reason)


def get_salvage_adapter(vendor: Optional[str] = None) -> Any:
    """Return a concrete adapter for the given vendor name. 'auto' returns a
    shadow-quote selector that picks the vendor with the highest net return
    per request (advertised behavior in README; previously returned Mock)."""
    from fnol_settings import settings
    name = (vendor or settings.salvage_vendor).lower()
    if name == "copart":
        return CopartAdapter()
    if name == "iaa":
        return IAAAdapter()
    if name == "mock":
        return MockSalvageAdapter()
    if name == "auto":
        return _AutoSalvageAdapter()
    raise ValueError(f"unknown salvage vendor: {vendor!r}")


def best_vendor_for(req: SalvageAssignmentRequest, candidates: Optional[List[str]] = None) -> SalvageAssignmentResponse:
    """Run shadow quotes against multiple vendors, pick highest net return.

    This is the value proposition of A11's 'auto' mode — the carrier never has
    to choose; A11 picks the vendor that maximises net recovery for this
    specific vehicle in this specific zip with this specific damage profile.
    """
    candidates = candidates or ["COPART", "IAA", "MOCK"]
    quotes: List[SalvageAssignmentResponse] = []
    for vname in candidates:
        try:
            adapter = get_salvage_adapter(vname.lower())
            quotes.append(adapter.assign(req))
        except Exception:
            continue
    if not quotes:
        return MockSalvageAdapter().assign(req)
    best = max(quotes, key=lambda q: q.expected_net_return_usd)
    best.raw_vendor_payload = {
        **best.raw_vendor_payload,
        "shadow_quotes": [{"vendor": q.vendor, "net_usd": q.expected_net_return_usd} for q in quotes],
        "selection_basis": "highest_expected_net_return",
    }
    best.rationale = f"selected from {len(quotes)} quotes — " + best.rationale
    return best


def health() -> Dict[str, Any]:
    from fnol_settings import settings
    return {
        "configured_vendor": settings.salvage_vendor,
        "copart_api_configured": bool(settings.copart_api_key),
        "iaa_api_configured": bool(settings.iaa_api_key),
        "supports_auto_selection": True,
    }


if __name__ == "__main__":
    # Smoke test
    req = SalvageAssignmentRequest(
        claim_id="CLM-DEMO-001",
        vin="1HGCV1F30LA123456",
        year=2020, make="Honda", model="Accord", trim="EX",
        mileage=58_000,
        acv_usd=18_500,
        damage_severity="SEVERE",
        drivable=False,
        primary_damage_area="FRONT",
        title_brand="SALVAGE",
        location_zip="77338",
        photo_count=14,
        prior_damage_disclosed=True,
        notes="single-vehicle, deployed airbags, frame compromised",
    )
    print("=== Mock adapter ===")
    print(asdict(MockSalvageAdapter().assign(req)))
    print()
    print("=== Copart adapter (shell mode) ===")
    print(asdict(CopartAdapter().assign(req)))
    print()
    print("=== IAA adapter (shell mode) ===")
    print(asdict(IAAAdapter().assign(req)))
    print()
    print("=== best_vendor_for (auto select) ===")
    best = best_vendor_for(req)
    print(f"WINNER: {best.vendor} @ ${best.expected_net_return_usd:,.2f} net "
          f"({best.salvage_recovery_pct*100:.1f}% of ACV)")
