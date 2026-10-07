import json
import os
import uuid
from pathlib import Path

import litellm
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tools import TOOLS, run_tool

# --- Config ---

SYSTEM_PROMPT = (
    "You are Pickle, an after-work pickleball planner for New York. "
    "Whenever the user gives a location, your first action must be to call find_pickleball_courts "
    "for it, even if it is late at night. Never answer a location with get_sun_times alone. That one "
    "call returns the three nearest courts plus the current local time, sunset times and is_dark_now. "
    "Use can_i_play_before_dark only when the user says how they will travel or asks if they can "
    "make it before dark. Use get_sun_times only for questions that are just about sunrise or sunset. "
    "Pass a court's lat_lon as court_location. If the user has not given a location, ask for one. "
    "The page shows each court on a map card, so do not list them again. Say which court is "
    "closest and how far it is. Then, if is_dark_now is true, say it is late out and already dark, "
    "give the time sunset was, and give tomorrow's sunset. If it is still light, say roughly how "
    "much daylight is left. Mention lights only if known. If a travel time is an estimate, say it "
    "is approximate. Write plain text only, with no markdown. Keep replies to 2 or 3 sentences."
)

MAX_TOOL_ROUNDS = 5

# --- The Harness ---


def run_agent(messages: list[dict]) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        reply = litellm.completion(
            model="vertex_ai/gemini-3.5-flash-lite",
            vertex_location="global",
            messages=messages,
            tools=TOOLS,
        ).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            return reply.content, tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            args = json.loads(call.function.arguments)
            result = run_tool(call.function.name, args)
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# --- Session Store ---

# session_id -> list of messages. In-memory, single process.
sessions: dict[str, list] = {}

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

tools.py

python
"""Pickleball-before-dark tools. All data sources are free and need no API key."""
 
import json
import math
import time
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
MAX_RADIUS_KM = 10
UNNAMED = "Unnamed pickleball courts"
_name_cache = {}

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
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    return 12742 * math.asin(math.sqrt(a))


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
            sites.append({
                "name": tags.get("name", UNNAMED),
                "lat_lon": f"{pt[0]},{pt[1]}",
                "distance_km": round(_km(lat, lon, *pt), 2),
                "courts_at_site": 1,
                "has_lights": tags.get("lit", "unknown"),
                "surface": tags.get("surface", "unknown"),
                "_pt": pt,
            })
    for site in sites:
        site.pop("_pt")
    return sites


def _nearby_name(lat: float, lon: float):
    """Name an unnamed court site after the park or street it is in. None if lookup fails."""
    key = (round(lat, 4), round(lon, 4))
    if key in _name_cache:
        return _name_cache[key]
    try:
        time.sleep(1.1)  # Nominatim allows 1 request per second
        d = requests.get(
            NOMINATIM_REVERSE,
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 17, "addressdetails": 1},
            headers=HEADERS, timeout=10,
        ).json()
        a = d.get("address", {})
        spot = a.get("park") or a.get("leisure") or d.get("name")
        road = a.get("road")
        if spot:
            name = f"{spot} courts"
        elif road:
            name = f"Courts on {road}"
        else:
            return None
    except (requests.RequestException, ValueError, AttributeError):
        return None
    _name_cache[key] = name
    return name
 
 
def find_pickleball_courts(area: str, radius_km: float = 3) -> str:
    """Find the 3 nearest pickleball sites, widening the search up to 10 km if needed."""
    try:
        lat, lon, label = _resolve(area)
    except LookupError as e:
        return _err(str(e))
    except SERVICE_ERRORS:
        return _err("The place lookup service failed. Try again in a moment.")

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

    for site in sites[:NUM_SITES]:
        if site["name"] == UNNAMED:
            lat_s, lon_s = site["lat_lon"].split(",")
            site["name"] = _nearby_name(float(lat_s), float(lon_s)) or "Pickleball courts"
    return json.dumps({"searched_near": label, "search_radius_km": radius_km, "sites": sites[:NUM_SITES]})
 
 
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

    # Real route if the routing server works, otherwise a straight-line estimate.
    try:
        route = requests.get(
            ROUTING.format(profile=PROFILES[mode], a=f"{slon},{slat}", b=f"{clon},{clat}"),
            headers=HEADERS, timeout=15,
        ).json()["routes"][0]
        minutes, dist_km, estimated = route["duration"] / 60, route["distance"] / 1000, False
    except SERVICE_ERRORS:
        dist_km = _km(slat, slon, clat, clon) * 1.3  # streets are longer than a straight line
        minutes, estimated = dist_km / FALLBACK_KMH[mode] * 60, True

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
          "Find the 3 nearest pickleball sites (groups of courts) to a place, closest first, with a name (the park or street when OpenStreetMap has none), distance, number of courts, lights and surface. Automatically widens the search up to 10 km if fewer than 3 are nearby. Returns each site's lat_lon, which can be passed to can_i_play_before_dark.",
          {"area": {"type": "string", "description": "Neighborhood, park or address, e.g. 'Upper West Side, New York'"},
           "radius_km": {"type": "number", "description": "Starting search radius in km (0.5 to 10). Default 3."}},
          ["area"]),
    _tool("get_sun_times",
          "Get the CURRENT local time at a place, today's sunrise and sunset, tomorrow's sunset, and whether it is dark right now. Call this first to know what time it is.",
          {"location": {"type": "string", "description": "Place name or 'lat,lon'"}},
          ["location"]),
    _tool("can_i_play_before_dark",
          "Calculate travel time to a court and how many minutes of daylight remain after arriving. Reports if it is already dark. Use for 'can I get a game in before dark?'.",
          {"start_location": {"type": "string", "description": "Where the user is leaving from (address, place or 'lat,lon')"},
           "court_location": {"type": "string", "description": "The court: a name, address, or the 'lat,lon' from find_pickleball_courts"},
           "mode": {"type": "string", "enum": list(PROFILES), "description": "How the user travels. Default walking."},
           "leave_in_minutes": {"type": "integer", "description": "Minutes from now until they leave. Default 0."}},
          ["start_location", "court_location"]),
]
 
TOOL_MAP = {"find_pickleball_courts": find_pickleball_courts, "get_sun_times": get_sun_times,
            "can_i_play_before_dark": can_i_play_before_dark}
 
 
def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return _err(f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}")
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return _err(f"Bad arguments for {name}: {e}")