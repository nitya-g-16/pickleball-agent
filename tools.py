"""Pickleball-before-dark tools. All data sources are free and need no API key."""

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

HEADERS = {"User-Agent": "pickleball-daylight-agent/1.0 (class project)"}
NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
FORECAST = "https://api.open-meteo.com/v1/forecast"
ROUTING = "https://routing.openstreetmap.de/{profile}/route/v1/driving/{a};{b}"
PROFILES = {"walking": "routed-foot", "biking": "routed-bike", "driving": "routed-car"}
# Used only if the routing server fails: rough city speeds in km/h
FALLBACK_KMH = {"walking": 5, "biking": 15, "driving": 25}
NUM_SITES = 3
CANDIDATES = 6  # nearest sites (straight line) that get a real travel distance before picking the final 3
MAX_RADIUS_KM = 10
UNNAMED = "Unnamed pickleball courts"
_name_cache = {}

# NYC bounding box
NYC_LAT_MIN = 40.49
NYC_LAT_MAX = 40.92
NYC_LON_MIN = -74.27
NYC_LON_MAX = -73.65

# Errors from the web services (bad JSON, timeouts, missing fields)
SERVICE_ERRORS = (requests.RequestException, KeyError, IndexError, ValueError)


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _clock(dt: datetime) -> str:
    """12-hour clock like '6:29 PM'."""
    return dt.strftime("%I:%M %p").lstrip("0")


def _resolve(place: str) -> tuple[float, float, str]:
    """Turn 'lat,lon' or a place name/address into (lat, lon, label).

    Raises LookupError if the place can't be found. Network or JSON problems
    raise the usual requests/ValueError errors, which callers handle separately.
    """
    try:
        lat, lon = [float(x) for x in place.split(",")]
        return lat, lon, place
    except ValueError:
        pass
    hits = requests.get(
        NOMINATIM,
        params={"q": place, "format": "json", "limit": 1},
        headers=HEADERS,
        timeout=10,
    ).json()
    if not hits:
        raise LookupError(
            f"Could not find '{place}'. Try a more specific address or add the city, e.g. 'Riverside Park, New York'."
        )
    return float(hits[0]["lat"]), float(hits[0]["lon"]), place


def _km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    a = (
        0.5
        - math.cos((lat2 - lat1) * p) / 2
        + math.cos(lat1 * p)
        * math.cos(lat2 * p)
        * (1 - math.cos((lon2 - lon1) * p))
        / 2
    )
    return 12742 * math.asin(math.sqrt(max(0, a)))

def _pt(el) -> tuple:
    c = el if "lat" in el else el.get("center", {})
    return c.get("lat"), c.get("lon")


def _overpass(query: str) -> list:
    """Try each Overpass server in turn. Returns elements, or None if all fail."""
    for url in OVERPASS_URLS:
        try:
            return requests.post(url, data={"data": query}, headers=HEADERS, timeout=25).json()["elements"]
        except SERVICE_ERRORS:
            continue
    return None


def _access_info(tags: dict) -> dict:
    """Get public/private and reservation information from OpenStreetMap tags."""
    access = tags.get("access", "").lower()

    if access in {"private", "no"}:
        public_status = "private"
    elif access == "permit":
        public_status = "permit required"
    elif access == "members":
        public_status = "members only"
    elif access in {"yes", "permissive"}:
        public_status = "public"
    else:
        public_status = "unknown"

    reservation = (
        tags.get("reservation")
        or tags.get("booking")
        or "unknown"
    )

    booking_url = (
        tags.get("website")
        or tags.get("contact:website")
        or tags.get("url")
        or "unknown"
    )

    return {
        "access": public_status,
        "reservation": reservation,
        "booking_url": booking_url,
        "opening_hours": tags.get("opening_hours", "unknown"),
        "fee": tags.get("fee", "unknown"),
    }


def _group_sites(elements: list, lat: float, lon: float) -> list:
    """Group courts within 150 m into one site, nearest site first."""
    sites = []
    located = [e for e in elements if _pt(e)[0] is not None]
    for el in sorted(located, key=lambda e: _km(lat, lon, *_pt(e))):
        pt = _pt(el)
        tags = el.get("tags", {})

        for site in sites:
            if _km(*site["_pt"], *pt) < 0.15:
                site["courts_at_site"] += 1
                break
        else:
            access_info = _access_info(tags)

            sites.append({
                "name": tags.get("name", UNNAMED),
                "lat_lon": f"{pt[0]},{pt[1]}",
                "distance_km": round(_km(lat, lon, *pt), 2),
                "courts_at_site": 1,
                "has_lights": tags.get("lit", "unknown"),
                "surface": tags.get("surface", "unknown"),
                "access": access_info["access"],
                "reservation": access_info["reservation"],
                "booking_url": access_info["booking_url"],
                "opening_hours": access_info["opening_hours"],
                "fee": access_info["fee"],
                "_pt": pt,
            })
    for site in sites:
        site.pop("_pt")
    return sites


def _park_name(lat: float, lon: float):
    """Name of the park a point is inside (or right next to), from OpenStreetMap. None if not found."""
    queries = [
        f'[out:json][timeout:15];is_in({lat},{lon})->.a;wr(pivot.a)["leisure"="park"]["name"];out tags;',
        f'[out:json][timeout:15];nwr(around:150,{lat},{lon})["leisure"="park"]["name"];out tags;',
    ]
    for q in queries:
        for el in _overpass(q) or []:
            name = el.get("tags", {}).get("name")
            if name:
                return name
    return None


def _nearby_name(lat: float, lon: float):
    """Name an unnamed court site after its park, or else the street. None if all lookups fail."""
    key = (round(lat, 4), round(lon, 4))
    if key in _name_cache:
        return _name_cache[key]

    name = None
    park = _park_name(lat, lon)
    if park:
        name = f"{park} courts"
    else:
        try:
            time.sleep(1.1)  # Nominatim allows 1 request per second
            d = requests.get(
                NOMINATIM_REVERSE,
                params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 17, "addressdetails": 1},
                headers=HEADERS, timeout=10,
            ).json()
            a = d.get("address", {})
            spot = a.get("park") or a.get("leisure")
            road = a.get("road")
            if spot:
                name = f"{spot} courts"
            elif road:
                name = f"Courts near {road}"
        except (requests.RequestException, ValueError, AttributeError):
            pass

    if name:
        _name_cache[key] = name
    return name


def find_pickleball_courts(area: str, radius_km: float = 3, mode: str = "walking") -> str:
    """Find the 3 pickleball sites with the shortest travel distance, widening the search up to 10 km if needed."""
    if mode not in PROFILES:
        return _err(f"mode must be one of {list(PROFILES)}, got '{mode}'.")
    try:
        lat, lon, label = _resolve(area)
    except LookupError as e:
        return _err(str(e))
    except SERVICE_ERRORS:
        return _err("The place lookup service failed. Try again in a moment.")

    # Pickle is currently limited to New York City.
    if not (NYC_LAT_MIN <= lat <= NYC_LAT_MAX and NYC_LON_MIN <= lon <= NYC_LON_MAX):
        return _err(
            "Pickle is currently limited to New York City. Please enter an NYC neighborhood, park, or address."
        )

    radius_km = min(max(radius_km, 0.5), MAX_RADIUS_KM)
    sites = []
    while True:
        query = f'[out:json][timeout:20];nwr["sport"="pickleball"](around:{int(radius_km * 1000)},{lat},{lon});out center 300;'
        elements = _overpass(query)
        if elements is None:
            if not sites:
                return _err("The court database is busy right now. Wait a few seconds and try again.")
            break
        sites = _group_sites(elements, lat, lon)
        if len(sites) >= NUM_SITES or radius_km >= MAX_RADIUS_KM:
            break
        radius_km = min(radius_km * 2, MAX_RADIUS_KM)

    if not sites:
        return _err(f"No pickleball courts found within {MAX_RADIUS_KM} km of {label}. OpenStreetMap coverage is incomplete; try a nearby neighborhood or borough instead.")

    # Straight-line distance only shortlists candidates. Rank them by real travel distance.
    candidates = sites[:CANDIDATES]

    def add_travel(site):
        slat, slon = (float(x) for x in site["lat_lon"].split(","))
        minutes, km, estimated = _route(mode, lat, lon, slat, slon)
        site["straight_line_km"] = site["distance_km"]
        site["distance_km"] = round(km, 2)
        site["travel_minutes"] = round(minutes)
        site["travel_is_estimate"] = estimated

    with ThreadPoolExecutor(max_workers=CANDIDATES) as pool:
        list(pool.map(add_travel, candidates))
    sites = sorted(candidates, key=lambda s: s["distance_km"])

    for site in sites[:NUM_SITES]:
        if site["name"] == UNNAMED:
            lat_s, lon_s = site["lat_lon"].split(",")
            site["name"] = _nearby_name(float(lat_s), float(lon_s)) or "Pickleball courts"

    result = {"searched_near": label, "travel_mode": mode, "ranked_by": f"{mode} travel distance, not straight-line",
              "search_radius_km": radius_km, "sites": sites[:NUM_SITES]}
    try:
        s = _sun(lat, lon)
        result.update({
            "current_local_time": _clock(s["now"]),
            "sunset_today": _clock(s["sunset"]),
            "sunset_tomorrow": _clock(s["tomorrow_sunset"]),
            "is_dark_now": s["now"] >= s["sunset"] or s["now"] < s["sunrise"],
        })
    except SERVICE_ERRORS:
        pass
    return json.dumps(result)


def _sun(lat: float, lon: float) -> dict:
    """Sunrise/sunset for today and tomorrow, plus the current local time at that spot."""
    d = requests.get(
        FORECAST,
        params={"latitude": lat, "longitude": lon, "daily": "sunrise,sunset", "timezone": "auto", "forecast_days": 2},
        timeout=10,
    ).json()
    tz = timezone(timedelta(seconds=d["utc_offset_seconds"]))
    return {
        "now": datetime.now(tz).replace(tzinfo=None),
        "sunrise": datetime.fromisoformat(d["daily"]["sunrise"][0]),
        "sunset": datetime.fromisoformat(d["daily"]["sunset"][0]),
        "tomorrow_sunset": datetime.fromisoformat(d["daily"]["sunset"][1]),
    }


def get_sun_times(location: str) -> str:
    """Get sunrise, sunset and the current local time, and whether it is dark right now."""
    try:
        lat, lon, label = _resolve(location)
        s = _sun(lat, lon)
    except LookupError as e:
        return _err(str(e))
    except SERVICE_ERRORS as e:
        return _err(f"Sun time service failed ({type(e).__name__}). Try again.")
    dark = s["now"] >= s["sunset"] or s["now"] < s["sunrise"]
    return json.dumps({
        "location": label,
        "current_local_time": _clock(s["now"]),
        "sunrise_today": _clock(s["sunrise"]),
        "sunset_today": _clock(s["sunset"]),
        "sunset_tomorrow": _clock(s["tomorrow_sunset"]),
        "is_dark_now": dark,
        "minutes_until_sunset": 0 if dark else int((s["sunset"] - s["now"]).total_seconds() // 60),
    })


def _route(mode: str, slat, slon, clat, clon) -> tuple[float, float, bool]:
    """(minutes, km, is_estimate). Real route if the routing server works, otherwise a straight-line estimate."""
    try:
        route = requests.get(
            ROUTING.format(profile=PROFILES[mode], a=f"{slon},{slat}", b=f"{clon},{clat}"),
            headers=HEADERS, timeout=15,
        ).json()["routes"][0]
        return route["duration"] / 60, route["distance"] / 1000, False
    except SERVICE_ERRORS:
        dist_km = _km(slat, slon, clat, clon) * 1.3
        return dist_km / FALLBACK_KMH[mode] * 60, dist_km, True


def get_distance_to_court(start_location: str, court_location: str, mode: str = "walking") -> str:
    """Distance and travel time between where the user is and a court."""
    if mode not in PROFILES:
        return _err(f"mode must be one of {list(PROFILES)}, got '{mode}'.")
    try:
        slat, slon, slabel = _resolve(start_location)
        clat, clon, clabel = _resolve(court_location)
    except LookupError as e:
        return _err(str(e))
    except SERVICE_ERRORS:
        return _err("Could not look up the locations. Check both are real places, then retry.")
    minutes, dist_km, estimated = _route(mode, slat, slon, clat, clon)
    return json.dumps({
        "from": slabel, "to": clabel, "mode": mode,
        "distance_km": round(dist_km, 2),
        "distance_miles": round(dist_km * 0.621371, 2),
        "straight_line_km": round(_km(slat, slon, clat, clon), 2),
        "travel_minutes": round(minutes),
        "travel_time_is_estimate": estimated,
    })


def can_i_play_before_dark(start_location: str, court_location: str, mode: str = "walking", leave_in_minutes: int = 0) -> str:
    """Work out how much daylight is left once you travel to a court."""
    if mode not in PROFILES:
        return _err(f"mode must be one of {list(PROFILES)}, got '{mode}'.")
    try:
        slat, slon, slabel = _resolve(start_location)
        clat, clon, clabel = _resolve(court_location)
        sun = _sun(clat, clon)
    except LookupError as e:
        return _err(str(e))
    except SERVICE_ERRORS:
        return _err("Could not look up the locations or sunset. Check both are real places, then retry.")

    minutes, dist_km, estimated = _route(mode, slat, slon, clat, clon)

    now, sunset = sun["now"], sun["sunset"]
    arrive = now + timedelta(minutes=leave_in_minutes + minutes)
    left = int((sunset - arrive).total_seconds() // 60)

    if now >= sunset:
        verdict = f"It is already dark: sunset was at {_clock(sunset)} and it is now {_clock(now)}. Tomorrow's sunset is at {_clock(sun['tomorrow_sunset'])}."
    elif left <= 0:
        verdict = f"You would arrive at {_clock(arrive)}, after sunset at {_clock(sunset)}."
    else:
        verdict = f"About {left} minutes of daylight on court."

    return json.dumps({
        "from": slabel, "to": clabel, "mode": mode,
        "travel_minutes": round(minutes),
        "distance_km": round(dist_km, 1),
        "travel_time_is_estimate": estimated,
        "current_local_time": _clock(now),
        "arrive_at_local": _clock(arrive),
        "sunset_today": _clock(sunset),
        "daylight_minutes_on_court": max(left, 0),
        "verdict": verdict,
        "note": "Courts may have lights; check has_lights from find_pickleball_courts.",
    })


def _tool(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS = [
    _tool("find_pickleball_courts",
          "Find the 3 pickleball sites (groups of courts) with the shortest TRAVEL distance (by street route, not straight-line) from a place, closest first, with a name, distance_km, travel_minutes, number of courts, lights, surface, public/private access, reservation or scheduling information, booking URL, opening hours, and fee when available. Do not guess unknown information. Also returns the current local time, today's and tomorrow's sunset, and is_dark_now. Automatically widens the search up to 10 km if fewer than 3 are nearby. Returns each site's lat_lon, which can be passed to can_i_play_before_dark.",
          {"area": {"type": "string", "description": "Neighborhood, park or address, e.g. 'Upper West Side, New York'"},
           "radius_km": {"type": "number", "description": "Starting search radius in km (0.5 to 10). Default 3."},
           "mode": {"type": "string", "enum": list(PROFILES), "description": "How the user travels, used to rank courts by travel distance. Default walking."}},
          ["area"]),
    _tool("get_sun_times",
          "Get the CURRENT local time at a place, today's sunrise and sunset, tomorrow's sunset, and whether it is dark right now. Call this first to know what time it is.",
          {"location": {"type": "string", "description": "Place name or 'lat,lon'"}},
          ["location"]),
    _tool("get_distance_to_court",
          "Get the travel distance (km and miles) and travel time between where the user is and a specific court. Use when the user asks how far a court is from them or from an address. Does not look at sunset; use can_i_play_before_dark for that.",
          {"start_location": {"type": "string", "description": "Where the user is (address, place or 'lat,lon')"},
           "court_location": {"type": "string", "description": "The court: a name, address, or the 'lat,lon' from find_pickleball_courts"},
           "mode": {"type": "string", "enum": list(PROFILES), "description": "How the user travels. Default walking."}},
          ["start_location", "court_location"]),
    _tool("can_i_play_before_dark",
          "Calculate travel time to a court and how many minutes of daylight remain after arriving. Reports if it is already dark. Use for 'can I get a game in before dark?'.",
          {"start_location": {"type": "string", "description": "Where the user is leaving from (address, place or 'lat,lon'"},
           "court_location": {"type": "string", "description": "The court: a name, address, or the 'lat,lon' from find_pickleball_courts"},
           "mode": {"type": "string", "enum": list(PROFILES), "description": "How the user travels. Default walking"},
           "leave_in_minutes": {"type": "integer", "description": "Minutes from now until they leave. Default 0."}},
          ["start_location", "court_location"]),
]


TOOL_MAP = {"find_pickleball_courts": find_pickleball_courts, "get_sun_times": get_sun_times,
            "can_i_play_before_dark": can_i_play_before_dark,
            "get_distance_to_court": get_distance_to_court}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return _err(f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}")
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return _err(f"Bad arguments for {name}: {e}")