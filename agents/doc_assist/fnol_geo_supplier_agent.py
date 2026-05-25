"""
FNOL Intelligence Platform — S1-D Geo-Based Supplier Assignment Agent
=====================================================================
V3 New Sub-Agent · Runs after S1-C (Automated Line Creation, Stage 01-C).
Blueprint: 00_-_Claims_FNOL_Auto_Blueprint_V3.html §Stage 01-D

Responsibilities
----------------
1. DRP Shop Geo-Matching       — haversine distance from loss/vehicle GPS to each
                                  DRP shop; ranks by distance + capacity + specialty.
2. EV / Specialty Filtering    — EV battery/HV damage → EV-certified shops only;
                                  heavy truck / exotic / classic → specialty flags.
3. Capacity Check              — queue depth > 85% → skip to next nearest available.
4. Radius Expansion            — no shop in 25 mi → expand to 50 mi → fallback:
                                  non-DRP appraisal authorized, adjuster notified.
5. Tow Dispatch                — drivable=false + towRequired → tow vendor dispatched
                                  to loss location; destination = selected DRP;
                                  GPS tracking link sent to claimant.
6. Field Adjuster Assignment   — photoQualityScore < 0.60 or photoCount < 4 →
                                  territory-matched field appraiser; inspection within
                                  2 business days; appointment confirmation sent.
7. Claimant Notification       — SMS + email to claimant with shop name, address,
                                  directions deep-link, drop date.
8. Claim Packet Dispatch       — VIN + photos + damage areas + coverage summary →
                                  DRP shop intake API (where available).
9. LLM Assignment Rationale    — LLM writes assignment memo for adjuster diary.

Decision Rules (per Blueprint V3 §Stage 01-D)
----------------------------------------------
  vehicleType=EV + battery/HV damage → EV-certified DRP only; 50-mi fallback
  drivable=false + towRequired        → tow dispatch; GPS link to claimant
  no DRP in 25 mi                     → expand 50 mi; else non-DRP + adjuster
  shop capacity > 85%                 → next nearest available
  photoQualityScore<0.60 or count<4   → field appraiser assigned; 2-day window

SLA: < 3 min (89% automation rate — per Blueprint V3)

Public API
----------
  assign_supplier(claim_id, request) -> GeoAssignmentResult
  get_assignment(claim_id) -> Optional[Dict]
  health() -> Dict
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fnol_llm_adapter import complete as llm_complete, resolve_provider
from fnol_runtime import BoundedStore
from fnol_settings import settings

log = logging.getLogger("fnol.geo_supplier")

AGENT_ID        = "S1-D"
AGENT_NAME      = "Geo-Based Supplier Assignment — Repair & Field Adjuster"
AGENT_VERSION   = "1.0.0"
STAGE_SLA_SEC   = 180
AUTOMATION_RATE = 0.89

# ───────────────────────────────────────────────────────────────────────────
# Enumerations
# ───────────────────────────────────────────────────────────────────────────

class VehicleType:
    STANDARD    = "STANDARD"
    EV          = "EV"
    HEAVY_TRUCK = "HEAVY_TRUCK"
    EXOTIC      = "EXOTIC"
    CLASSIC     = "CLASSIC"
    MOTORCYCLE  = "MOTORCYCLE"
    RV          = "RV"

class NotificationChannel:
    SMS   = "SMS"
    EMAIL = "EMAIL"
    APP   = "APP"
    PHONE = "PHONE"

class AssignmentStatus:
    DRP_ASSIGNED        = "DRP_ASSIGNED"
    DRP_NON_NETWORK     = "DRP_NON_NETWORK"    # radius expansion fallback
    FIELD_ADJUSTER_ONLY = "FIELD_ADJUSTER_ONLY"
    TOW_DISPATCHED      = "TOW_DISPATCHED"
    NOTIFICATION_SENT   = "NOTIFICATION_SENT"
    PACKET_DISPATCHED   = "PACKET_DISPATCHED"
    MANUAL_REQUIRED     = "MANUAL_REQUIRED"

# ───────────────────────────────────────────────────────────────────────────
# Mock DRP Network  (lat/lon for major metro areas)
# ───────────────────────────────────────────────────────────────────────────

_DRP_NETWORK: List[Dict[str, Any]] = [
    # Florida
    {"drp_id":"DRP-FL-001","name":"AutoNation Collision Doral","address":"8200 NW 12th St, Doral, FL 33126","phone":"(305) 592-8200","lat":25.8178,"lon":-80.3569,"state":"FL","capacity_pct":0.62,"ev_certified":True,"heavy_truck":False,"exotic":False,"classic":False,"queue_days":4,"open_hours":"M-F 7am-6pm, Sa 8am-2pm","intake_api":True},
    {"drp_id":"DRP-FL-002","name":"Gerber Collision Ft Lauderdale","address":"3250 N Andrews Ave, Ft Lauderdale, FL 33309","phone":"(954) 563-7100","lat":26.1537,"lon":-80.1425,"state":"FL","capacity_pct":0.78,"ev_certified":False,"heavy_truck":False,"exotic":False,"classic":False,"queue_days":6,"open_hours":"M-F 7:30am-5:30pm","intake_api":False},
    {"drp_id":"DRP-FL-003","name":"Classic Collision Aventura","address":"20800 Biscayne Blvd, Aventura, FL 33180","phone":"(305) 933-0500","lat":25.9568,"lon":-80.1430,"state":"FL","capacity_pct":0.55,"ev_certified":True,"heavy_truck":False,"exotic":True,"classic":True,"queue_days":3,"open_hours":"M-F 8am-5pm","intake_api":True},
    {"drp_id":"DRP-FL-004","name":"Maaco Collision Hialeah","address":"1450 W 16th St, Hialeah, FL 33010","phone":"(305) 883-6262","lat":25.8644,"lon":-80.2988,"state":"FL","capacity_pct":0.91,"ev_certified":False,"heavy_truck":True,"exotic":False,"classic":False,"queue_days":8,"open_hours":"M-F 8am-5pm","intake_api":False},
    # Texas
    {"drp_id":"DRP-TX-001","name":"Service King Houston Loop","address":"9811 Westheimer Rd, Houston, TX 77042","phone":"(713) 952-6800","lat":29.7369,"lon":-95.5558,"state":"TX","capacity_pct":0.70,"ev_certified":True,"heavy_truck":False,"exotic":False,"classic":False,"queue_days":5,"open_hours":"M-F 7am-6pm","intake_api":True},
    {"drp_id":"DRP-TX-002","name":"Caliber Collision Dallas N","address":"5901 Lemmon Ave, Dallas, TX 75209","phone":"(214) 352-6400","lat":32.8221,"lon":-96.8346,"state":"TX","capacity_pct":0.65,"ev_certified":True,"heavy_truck":False,"exotic":True,"classic":False,"queue_days":4,"open_hours":"M-F 7:30am-5:30pm","intake_api":True},
    # California
    {"drp_id":"DRP-CA-001","name":"Hendrick Collision LA","address":"2603 Lincoln Blvd, Santa Monica, CA 90405","phone":"(310) 452-8700","lat":34.0195,"lon":-118.4912,"state":"CA","capacity_pct":0.80,"ev_certified":True,"heavy_truck":False,"exotic":True,"classic":True,"queue_days":7,"open_hours":"M-F 7am-6pm","intake_api":True},
    {"drp_id":"DRP-CA-002","name":"Fix Auto San Jose","address":"2465 El Camino Real, Santa Clara, CA 95051","phone":"(408) 246-8900","lat":37.3541,"lon":-121.9552,"state":"CA","capacity_pct":0.58,"ev_certified":True,"heavy_truck":False,"exotic":False,"classic":False,"queue_days":3,"open_hours":"M-F 7:30am-5:30pm","intake_api":False},
    # New York
    {"drp_id":"DRP-NY-001","name":"Copart Collision NY","address":"4840 Northern Blvd, Long Island City, NY 11101","phone":"(718) 786-0200","lat":40.7505,"lon":-73.9380,"state":"NY","capacity_pct":0.72,"ev_certified":True,"heavy_truck":True,"exotic":False,"classic":False,"queue_days":5,"open_hours":"M-F 8am-5pm","intake_api":True},
    # Illinois
    {"drp_id":"DRP-IL-001","name":"Gerber Collision Chicago","address":"900 W Fullerton Ave, Chicago, IL 60614","phone":"(773) 472-1600","lat":41.9247,"lon":-87.6534,"state":"IL","capacity_pct":0.68,"ev_certified":True,"heavy_truck":False,"exotic":False,"classic":False,"queue_days":5,"open_hours":"M-F 7am-6pm","intake_api":True},
    # Georgia
    {"drp_id":"DRP-GA-001","name":"Classic Collision Atlanta","address":"1800 Peachtree St NW, Atlanta, GA 30309","phone":"(404) 352-7700","lat":33.8023,"lon":-84.3897,"state":"GA","capacity_pct":0.60,"ev_certified":True,"heavy_truck":False,"exotic":True,"classic":False,"queue_days":4,"open_hours":"M-F 7:30am-5:30pm","intake_api":True},
]

# ── Field adjuster roster ───────────────────────────────────────────────────
_FIELD_ADJUSTER_ROSTER: List[Dict[str, Any]] = [
    {"adjuster_id":"ADJ-001","name":"Michael Torres","phone":"(305) 555-0101","territory":"FL-SE","lat_center":25.8,"lon_center":-80.2,"radius_mi":60,"ev_certified":True,"exotic_cert":True,"current_load":3,"max_load":8},
    {"adjuster_id":"ADJ-002","name":"Sandra Rivera","phone":"(954) 555-0202","territory":"FL-BROWARD","lat_center":26.1,"lon_center":-80.1,"radius_mi":40,"ev_certified":False,"exotic_cert":False,"current_load":5,"max_load":8},
    {"adjuster_id":"ADJ-003","name":"James Okafor","phone":"(713) 555-0303","territory":"TX-HOUSTON","lat_center":29.7,"lon_center":-95.4,"radius_mi":70,"ev_certified":True,"exotic_cert":False,"current_load":4,"max_load":8},
    {"adjuster_id":"ADJ-004","name":"Priya Nair","phone":"(213) 555-0404","territory":"CA-LA","lat_center":34.0,"lon_center":-118.2,"radius_mi":50,"ev_certified":True,"exotic_cert":True,"current_load":6,"max_load":8},
    {"adjuster_id":"ADJ-005","name":"Derek Walsh","phone":"(312) 555-0505","territory":"IL-CHICAGO","lat_center":41.8,"lon_center":-87.6,"radius_mi":55,"ev_certified":True,"exotic_cert":False,"current_load":2,"max_load":8},
    {"adjuster_id":"ADJ-006","name":"Tanya Brooks","phone":"(404) 555-0606","territory":"GA-ATLANTA","lat_center":33.7,"lon_center":-84.3,"radius_mi":65,"ev_certified":True,"exotic_cert":True,"current_load":3,"max_load":8},
]

# ── Tow vendors ─────────────────────────────────────────────────────────────
_TOW_VENDORS: List[Dict[str, Any]] = [
    {"vendor_id":"TOW-001","name":"A&E Towing Services","phone":"(800) 555-1111","coverage_states":["FL","GA"],"avg_eta_min":35},
    {"vendor_id":"TOW-002","name":"Agero Roadside","phone":"(800) 555-2222","coverage_states":["FL","TX","CA","NY","IL","GA","OH","NC"],"avg_eta_min":28},
    {"vendor_id":"TOW-003","name":"Copart Transport","phone":"(800) 555-3333","coverage_states":["FL","TX","CA","NY","IL","GA","OH","NC","AZ","CO"],"avg_eta_min":45},
    {"vendor_id":"TOW-004","name":"Urgently Tow","phone":"(800) 555-4444","coverage_states":["CA","TX","NY","IL","WA","CO","AZ"],"avg_eta_min":22},
]

# ───────────────────────────────────────────────────────────────────────────
# Data Structures
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class GeoLocation:
    lat: float
    lon: float
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None

@dataclass
class DRPShopResult:
    drp_id: str
    name: str
    address: str
    phone: str
    distance_mi: float
    estimated_drop_date: str
    queue_days: int
    capacity_pct: float
    ev_certified: bool
    intake_api_available: bool
    directions_url: str
    selection_reason: str
    fallback_used: bool = False            # True if non-DRP fallback
    fallback_reason: Optional[str] = None

@dataclass
class TowDispatch:
    vendor_id: str
    vendor_name: str
    vendor_phone: str
    pickup_location: str
    destination_shop: str
    eta_minutes: int
    gps_tracking_url: str
    dispatched_at: str

@dataclass
class FieldAdjusterAssignment:
    adjuster_id: str
    name: str
    phone: str
    territory: str
    distance_mi: float
    inspection_date: str
    appointment_confirmed: bool
    ev_certified: bool
    exotic_cert: bool
    assignment_reason: str

@dataclass
class ClaimPacket:
    dispatched: bool
    dispatch_method: str             # API | EMAIL | FAX | MANUAL
    destination_shop_id: str
    contents: List[str] = field(default_factory=list)
    dispatched_at: Optional[str] = None
    error: Optional[str] = None

@dataclass
class AssignmentNotification:
    channel: str
    recipient: str
    message_summary: str
    sent_at: str
    delivery_status: str = "SENT"

@dataclass
class GeoAssignmentRequest:
    claim_id: str
    loss_location: GeoLocation
    vehicle_location: Optional[GeoLocation] = None
    vehicle_type: str = VehicleType.STANDARD
    drivable: bool = True
    tow_required: bool = False
    damage_areas: List[str] = field(default_factory=list)
    photo_quality_score: Optional[float] = None
    photo_count: Optional[int] = None
    jurisdiction_state: Optional[str] = None
    claimant_name: Optional[str] = None
    claimant_phone: Optional[str] = None
    claimant_email: Optional[str] = None
    preferred_channel: str = NotificationChannel.SMS
    vin: Optional[str] = None
    coverage_summary: Optional[str] = None

@dataclass
class GeoAssignmentResult:
    result_id: str
    claim_id: str
    stage_id: str = AGENT_ID
    agent_version: str = AGENT_VERSION
    status: str = "ok"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    drp_shop: Optional[DRPShopResult] = None
    tow_dispatch: Optional[TowDispatch] = None
    field_adjuster: Optional[FieldAdjusterAssignment] = None
    claim_packet: Optional[ClaimPacket] = None
    notifications: List[AssignmentNotification] = field(default_factory=list)
    adjuster_tasks: List[Dict[str, Any]] = field(default_factory=list)

    # Flags
    field_inspection_required: bool = False
    non_network_fallback: bool = False
    ev_specialty_required: bool = False
    search_radius_expanded: bool = False

    elapsed_ms: Optional[int] = None
    sla_met: bool = True
    llm_provider: str = field(default_factory=resolve_provider)
    assignment_rationale: Optional[str] = None
    errors: List[str] = field(default_factory=list)

# ───────────────────────────────────────────────────────────────────────────
# Geo Engine
# ───────────────────────────────────────────────────────────────────────────

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in miles."""
    R = 3_958.8  # Earth radius miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def _is_ev_damage(damage_areas: List[str]) -> bool:
    ev_keywords = {"battery_pack","high_voltage","hv_system","battery","ev_floor","charging_port"}
    return bool(set(a.lower().replace(" ","_") for a in damage_areas) & ev_keywords)

def _business_days_from_now(days: int) -> str:
    """Return date N business days from today."""
    d = datetime.now(timezone.utc)
    added = 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d.strftime("%Y-%m-%d")

def _maps_url(dest_addr: str, origin_lat: float, origin_lon: float) -> str:
    enc = dest_addr.replace(" ", "+").replace(",", "%2C")
    return f"https://www.google.com/maps/dir/{origin_lat},{origin_lon}/{enc}"

def _find_drp(
    lat: float, lon: float,
    vehicle_type: str,
    damage_areas: List[str],
    state: Optional[str],
    max_radius_mi: float = 25.0,
) -> Tuple[Optional[Dict], bool, bool, float]:
    """
    Returns (best_shop, fallback_used, radius_expanded, search_radius).
    Implements: EV filter, capacity check (>85% skip), radius expansion.
    """
    ev_damage = _is_ev_damage(damage_areas)
    need_ev = (vehicle_type == VehicleType.EV) and ev_damage
    need_exotic = vehicle_type in (VehicleType.EXOTIC, VehicleType.CLASSIC)
    need_heavy = vehicle_type == VehicleType.HEAVY_TRUCK

    def shop_eligible(s: Dict) -> bool:
        if need_ev and not s["ev_certified"]:
            return False
        if need_exotic and not s.get("exotic", False):
            return False
        if need_heavy and not s.get("heavy_truck", False):
            return False
        return True

    def rank_shops(radius: float) -> List[Tuple[float, Dict]]:
        result = []
        for s in _DRP_NETWORK:
            d = _haversine(lat, lon, s["lat"], s["lon"])
            if d > radius:
                continue
            if not shop_eligible(s):
                continue
            if s["capacity_pct"] > 0.85:
                continue   # Rule: skip over-capacity shops
            result.append((d, s))
        return sorted(result, key=lambda x: (x[0], x[1]["capacity_pct"]))

    # Try 25 mi
    shops = rank_shops(25.0)
    if shops:
        return shops[0][1], False, False, 25.0

    # Expand to 50 mi
    shops = rank_shops(50.0)
    if shops:
        return shops[0][1], False, True, 50.0

    # Non-DRP fallback — return nearest shop ignoring capacity/specialty
    nearest = sorted(
        [(_haversine(lat, lon, s["lat"], s["lon"]), s) for s in _DRP_NETWORK],
        key=lambda x: x[0]
    )
    if nearest:
        return nearest[0][1], True, True, 99.0
    return None, True, True, 99.0

def _find_field_adjuster(
    lat: float, lon: float,
    vehicle_type: str,
    ev_damage: bool,
) -> Optional[Dict]:
    need_ev = (vehicle_type == VehicleType.EV) and ev_damage
    need_exotic = vehicle_type in (VehicleType.EXOTIC, VehicleType.CLASSIC)

    candidates = []
    for adj in _FIELD_ADJUSTER_ROSTER:
        d = _haversine(lat, lon, adj["lat_center"], adj["lon_center"])
        if d > adj["radius_mi"]:
            continue
        if need_ev and not adj["ev_certified"]:
            continue
        if need_exotic and not adj["exotic_cert"]:
            continue
        if adj["current_load"] >= adj["max_load"]:
            continue
        # Score: distance (lower=better) + load ratio (lower=better)
        score = (d / adj["radius_mi"]) * 0.6 + (adj["current_load"] / adj["max_load"]) * 0.4
        candidates.append((score, adj))

    if not candidates:
        # Return any adjuster in territory
        candidates = sorted(
            [(_haversine(lat, lon, a["lat_center"], a["lon_center"]), a)
             for a in _FIELD_ADJUSTER_ROSTER],
            key=lambda x: x[0]
        )
        if candidates:
            return candidates[0][1]
        return None

    return sorted(candidates, key=lambda x: x[0])[0][1]

def _find_tow_vendor(state: Optional[str]) -> Optional[Dict]:
    st = (state or "").upper()
    matches = [v for v in _TOW_VENDORS if st in v["coverage_states"]]
    if matches:
        return sorted(matches, key=lambda v: v["avg_eta_min"])[0]
    return _TOW_VENDORS[1]  # Agero as nationwide fallback

def _llm_assignment_rationale(
    result: GeoAssignmentResult,
    request: GeoAssignmentRequest,
) -> str:
    shop = result.drp_shop
    adj = result.field_adjuster
    prompt = (
        f"Claim {request.claim_id}. Geo-based supplier assignment complete.\n"
        f"Vehicle type: {request.vehicle_type}. Drivable: {request.drivable}. "
        f"Tow required: {request.tow_required}.\n"
        f"Loss location: {request.loss_location.address or f'{request.loss_location.lat:.4f}, {request.loss_location.lon:.4f}'}.\n"
        + (f"DRP shop assigned: {shop.name} ({shop.distance_mi:.1f} mi away, "
           f"est. drop date {shop.estimated_drop_date}, "
           f"capacity {shop.capacity_pct*100:.0f}%). {shop.selection_reason}\n" if shop else "No DRP shop available.\n")
        + (f"Field adjuster: {adj.name}, territory {adj.territory}, "
           f"inspection {adj.inspection_date}.\n" if adj else "")
        + (f"Non-network fallback: {result.non_network_fallback}. "
           f"Search radius expanded: {result.search_radius_expanded}.\n")
        + "Write a 2-sentence adjuster diary note on the assignment rationale. Professional tone."
    )
    try:
        resp = llm_complete(
            system="You are a senior P&C claims adjuster writing concise diary notes.",
            user=prompt,
            max_tokens=140,
        )
        return resp.get("content", "")
    except Exception:
        shop_note = f"DRP shop {shop.name} assigned ({shop.distance_mi:.1f} mi, est. drop {shop.estimated_drop_date})." if shop else "No DRP shop in network radius."
        adj_note = f" Field appraiser {adj.name} assigned for {adj.inspection_date} inspection." if adj else ""
        return shop_note + adj_note + " Claimant notified via preferred channel."

# ───────────────────────────────────────────────────────────────────────────
# Stores
# ───────────────────────────────────────────────────────────────────────────
_RESULT_STORE: BoundedStore = BoundedStore(max_size=2048, ttl_seconds=86400)
_CLAIM_IDX:    BoundedStore = BoundedStore(max_size=2048, ttl_seconds=86400)

# ───────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ───────────────────────────────────────────────────────────────────────────

def assign_supplier(
    claim_id: str,
    request: GeoAssignmentRequest,
) -> GeoAssignmentResult:
    t0 = time.time()
    now = datetime.now(timezone.utc).isoformat()
    result_id = f"GEO-{claim_id}-{uuid.uuid4().hex[:8].upper()}"

    result = GeoAssignmentResult(result_id=result_id, claim_id=claim_id, created_at=now)

    loc = request.vehicle_location or request.loss_location
    lat, lon = loc.lat, loc.lon
    ev_dmg = _is_ev_damage(request.damage_areas)
    result.ev_specialty_required = (request.vehicle_type == VehicleType.EV) and ev_dmg

    # ── DRP shop selection ────────────────────────────────────────────────
    shop_dict, fallback, expanded, radius = _find_drp(
        lat, lon, request.vehicle_type, request.damage_areas, request.jurisdiction_state
    )
    result.search_radius_expanded = expanded
    result.non_network_fallback = fallback

    if shop_dict:
        dist = _haversine(lat, lon, shop_dict["lat"], shop_dict["lon"])
        drop_date = _business_days_from_now(shop_dict["queue_days"])
        reason = (
            "Nearest available DRP shop" if not fallback and not expanded else
            "Radius expanded to 50 mi — nearest qualifying shop" if expanded and not fallback else
            "Non-DRP fallback — no network shop within 50 mi"
        )
        if result.ev_specialty_required and shop_dict.get("ev_certified"):
            reason = "EV-certified DRP shop selected — high-voltage damage detected"
        if shop_dict["capacity_pct"] > 0.85:
            reason += " (capacity check bypassed — non-network)"

        result.drp_shop = DRPShopResult(
            drp_id=shop_dict["drp_id"],
            name=shop_dict["name"],
            address=shop_dict["address"],
            phone=shop_dict["phone"],
            distance_mi=round(dist, 1),
            estimated_drop_date=drop_date,
            queue_days=shop_dict["queue_days"],
            capacity_pct=shop_dict["capacity_pct"],
            ev_certified=shop_dict["ev_certified"],
            intake_api_available=shop_dict.get("intake_api", False),
            directions_url=_maps_url(shop_dict["address"], lat, lon),
            selection_reason=reason,
            fallback_used=fallback,
            fallback_reason="No DRP shop within 50 mi — non-network appraiser authorized" if fallback else None,
        )

        # Non-DRP fallback → adjuster task + diary
        if fallback:
            result.adjuster_tasks.append({
                "task_id": f"TASK-{uuid.uuid4().hex[:8].upper()}",
                "task_type": "NON_NETWORK_APPRAISAL_REVIEW",
                "priority": "MEDIUM",
                "description": f"No DRP shop within 50 mi of loss location. Non-DRP appraisal authorized. Non-network rate schedule applies. Adjuster review required.",
                "assigned_to": "PROPERTY_ADJUSTER",
                "due_hours_from_now": 24,
                "sor_ref": f"DC-{uuid.uuid4().hex[:6].upper()}",
                "created_at": now,
            })
            result.status = "warning"

        # Capacity > 85% skip notice
        if shop_dict["capacity_pct"] > 0.85:
            result.adjuster_tasks.append({
                "task_id": f"TASK-{uuid.uuid4().hex[:8].upper()}",
                "task_type": "SHOP_CAPACITY_NOTICE",
                "priority": "LOW",
                "description": f"Shop {shop_dict['name']} at {shop_dict['capacity_pct']*100:.0f}% capacity — next available selected. Queue: {shop_dict['queue_days']} days.",
                "assigned_to": "FILE_ADJUSTER",
                "due_hours_from_now": 48,
                "sor_ref": f"DC-{uuid.uuid4().hex[:6].upper()}",
                "created_at": now,
            })

    # ── Tow dispatch ───────────────────────────────────────────────────────
    if not request.drivable or request.tow_required:
        vendor = _find_tow_vendor(request.jurisdiction_state)
        if vendor and result.drp_shop:
            tracking_id = uuid.uuid4().hex[:12].upper()
            result.tow_dispatch = TowDispatch(
                vendor_id=vendor["vendor_id"],
                vendor_name=vendor["name"],
                vendor_phone=vendor["phone"],
                pickup_location=request.loss_location.address or f"{request.loss_location.lat:.5f},{request.loss_location.lon:.5f}",
                destination_shop=result.drp_shop.address,
                eta_minutes=vendor["avg_eta_min"],
                gps_tracking_url=f"https://track.tow.internal/{tracking_id}",
                dispatched_at=now,
            )

    # ── Field adjuster assignment ──────────────────────────────────────────
    need_field = (
        (request.photo_quality_score is not None and request.photo_quality_score < 0.60) or
        (request.photo_count is not None and request.photo_count < 4) or
        request.vehicle_type in (VehicleType.EXOTIC, VehicleType.CLASSIC, VehicleType.HEAVY_TRUCK)
    )
    result.field_inspection_required = need_field

    if need_field:
        adj = _find_field_adjuster(lat, lon, request.vehicle_type, ev_dmg)
        if adj:
            insp_date = _business_days_from_now(2)
            reason = (
                "Exotic/classic vehicle — specialty appraiser required" if request.vehicle_type in (VehicleType.EXOTIC, VehicleType.CLASSIC) else
                f"Photo quality score {request.photo_quality_score:.2f} < 0.60 threshold" if request.photo_quality_score and request.photo_quality_score < 0.60 else
                f"Only {request.photo_count} photos submitted (minimum 4 required)"
            )
            result.field_adjuster = FieldAdjusterAssignment(
                adjuster_id=adj["adjuster_id"],
                name=adj["name"],
                phone=adj["phone"],
                territory=adj["territory"],
                distance_mi=round(_haversine(lat, lon, adj["lat_center"], adj["lon_center"]), 1),
                inspection_date=insp_date,
                appointment_confirmed=True,
                ev_certified=adj["ev_certified"],
                exotic_cert=adj["exotic_cert"],
                assignment_reason=reason,
            )

    # ── Claim packet dispatch ──────────────────────────────────────────────
    packet_contents = ["VIN", "damage_areas", "coverage_summary"]
    if request.vin:
        packet_contents.append("vin_decode_result")
    if request.photo_quality_score:
        packet_contents.append("photo_quality_report")

    dispatch_method = "API" if (result.drp_shop and result.drp_shop.intake_api_available) else "EMAIL"
    result.claim_packet = ClaimPacket(
        dispatched=True,
        dispatch_method=dispatch_method,
        destination_shop_id=result.drp_shop.drp_id if result.drp_shop else "NONE",
        contents=packet_contents,
        dispatched_at=now,
    )

    # ── Notifications ──────────────────────────────────────────────────────
    channel = request.preferred_channel or NotificationChannel.SMS
    if request.claimant_name or request.claimant_phone:
        shop_msg = (
            f"Your vehicle has been assigned to {result.drp_shop.name} "
            f"({result.drp_shop.distance_mi:.1f} mi away). "
            f"Estimated drop date: {result.drp_shop.estimated_drop_date}. "
            f"Directions: {result.drp_shop.directions_url[:60]}…"
        ) if result.drp_shop else "Your claim is being processed. An adjuster will contact you."
        result.notifications.append(AssignmentNotification(
            channel=channel,
            recipient=request.claimant_name or "Claimant",
            message_summary=shop_msg,
            sent_at=now,
        ))
    if result.tow_dispatch:
        result.notifications.append(AssignmentNotification(
            channel=channel,
            recipient=request.claimant_name or "Claimant",
            message_summary=f"Tow truck dispatched. {result.tow_dispatch.vendor_name}. ETA: {result.tow_dispatch.eta_minutes} min. Track: {result.tow_dispatch.gps_tracking_url}",
            sent_at=now,
        ))
    if result.drp_shop:
        result.notifications.append(AssignmentNotification(
            channel="API" if result.drp_shop.intake_api_available else "EMAIL",
            recipient=result.drp_shop.name,
            message_summary=f"Claim packet received for VIN {request.vin or 'unknown'}. Claimant drop scheduled {result.drp_shop.estimated_drop_date}.",
            sent_at=now,
        ))

    # ── LLM rationale ─────────────────────────────────────────────────────
    result.assignment_rationale = _llm_assignment_rationale(result, request)

    elapsed_ms = int((time.time() - t0) * 1000)
    result.elapsed_ms = elapsed_ms
    result.sla_met = elapsed_ms <= STAGE_SLA_SEC * 1000
    if result.status == "ok" and not result.drp_shop:
        result.status = "warning"

    # ── Persist ────────────────────────────────────────────────────────────
    d = asdict(result)
    _RESULT_STORE.set(result_id, d)
    _CLAIM_IDX.set(claim_id, result_id)

    log.info("S1-D complete: claim=%s shop=%s tow=%s field=%s elapsed=%dms",
             claim_id,
             result.drp_shop.name if result.drp_shop else "NONE",
             bool(result.tow_dispatch), bool(result.field_adjuster), elapsed_ms)
    return result


def get_assignment(claim_id: str) -> Optional[Dict[str, Any]]:
    rid = _CLAIM_IDX.get(claim_id)
    if not rid:
        return None
    return _RESULT_STORE.get(rid)


def health() -> Dict[str, Any]:
    return {
        "agent": AGENT_NAME, "agent_id": AGENT_ID, "version": AGENT_VERSION,
        "status": "ok", "sla_seconds": STAGE_SLA_SEC, "automation_rate": AUTOMATION_RATE,
        "llm_provider": resolve_provider(),
        "network": {"drp_shops": len(_DRP_NETWORK), "field_adjusters": len(_FIELD_ADJUSTER_ROSTER), "tow_vendors": len(_TOW_VENDORS)},
        "stores": {"results": len(_RESULT_STORE.keys())},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
