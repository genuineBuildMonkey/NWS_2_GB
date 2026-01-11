#!/usr/bin/env python3

"""
NWS -> GoodBarber POC
- Poll NWS active alerts every 60s (by state)
- For new alerts, obtain a Polygon (alert.geometry or first affectedZone Polygon)
- Aggressively simplify polygon to <=20 points (prefer ~8)
- Log into GoodBarber dashboard (/manage/)
- GET /manage/users/push/send/ to harvest hidden token(s)
- POST /manage/users/push/send/ with:
    - zones=[[{lat,lng}...]]
    - sound=03 (Bells)
    - target=select (so zones applies)
Notes:
- Honeypot fields named "address" exist: never populate; omit or keep empty.
- GoodBarber uses a dynamic hidden field (random name/value) that must be POSTed.
  We harvest *all* hidden inputs from the push page and include them, overriding what we need.
"""

import json
import math
import os
import pickle
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

# -----------------------------
# CONFIG (EDIT THESE)
# -----------------------------

DASHBOARD_BASE = "https://nwsgbnoti.blizapps.com"
GB_LOGIN_PATH = "/manage/"
GB_PUSH_SEND_PATH = "/manage/users/push/send/"
GB_PUSH_HISTORY_PATH = "/manage/users/push/history/"

# Credentials: keep out of source control
# export GB_LOGIN="..." GB_PASSWORD="..."
#GB_LOGIN = os.environ.get("GB_LOGIN", "")
#GB_PASSWORD = os.environ.get("GB_PASSWORD", "")
GB_LOGIN = 'you@mail.com' 
GB_PASSWORD = 'password goes here'


# Cookie cache file (so you don't log in every loop)
COOKIE_JAR_FILE = "goodbarber_cookies.pkl"

# NWS polling
STATES = ["WY", "NM", "FL"]
POLL_INTERVAL = 60 # call every this many seconds 

# Polygon simplification
MAX_POINTS = 20
PREFERRED_POINTS = 8

# NWS API
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
NWS_HEADERS = {
    "User-Agent": "nws-goodbarber-poc/0.1 (contact: you@example.com)",
    "Accept": "application/geo+json,application/json;q=0.9",
}

# GoodBarber HTTP headers (match browser-ish basics)
GB_HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/146.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": DASHBOARD_BASE,
}

# -----------------------------
# UTIL
# -----------------------------

def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def today_strings_local():
    """
    GoodBarber expects:
      picker-date: MM/DD/YYYY
      date: YYYY-MM-DD
      heure: HH:MM
      hour-heure: HH
      minutes-heure: MM
    For pushDate=now, these may not matter, but we send them anyway (mirrors observed POST).
    """
    # Use local time per server expectation; for a POC, system local is fine.
    # If you need a specific tz, do it explicitly.
    local = datetime.now()
    picker_date = local.strftime("%m/%d/%Y")
    iso_date = local.strftime("%Y-%m-%d")
    hh = local.strftime("%H")
    mm = local.strftime("%M")
    heure = f"{hh}:{mm}"
    return picker_date, iso_date, heure, hh, mm

def abs_url(path: str) -> str:
    return DASHBOARD_BASE.rstrip("/") + path

# -----------------------------
# NWS FETCH + POLYGON SIMPLIFY
# -----------------------------

def fetch_json(session, url, params=None, headers=None, etag=None, last_modified=None, timeout=20):
    h = dict(headers or {})
    if etag:
        h["If-None-Match"] = etag
    if last_modified:
        h["If-Modified-Since"] = last_modified
    resp = session.get(url, params=params, headers=h, timeout=timeout)
    if resp.status_code == 304:
        return None, etag, last_modified
    resp.raise_for_status()
    return resp.json(), resp.headers.get("ETag"), resp.headers.get("Last-Modified")

def rdp(points, epsilon):
    """Ramer–Douglas–Peucker on (lon,lat) in degrees; POC quality."""
    if len(points) < 3:
        return points

    x1, y1 = points[0]
    x2, y2 = points[-1]

    def perp_dist(px, py):
        dx = x2 - x1
        dy = y2 - y1
        if dx == 0 and dy == 0:
            return math.hypot(px - x1, py - y1)
        t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        projx = x1 + t * dx
        projy = y1 + t * dy
        return math.hypot(px - projx, py - projy)

    max_dist = -1.0
    idx = -1
    for i in range(1, len(points) - 1):
        d = perp_dist(points[i][0], points[i][1])
        if d > max_dist:
            max_dist = d
            idx = i

    if max_dist > epsilon:
        left = rdp(points[: idx + 1], epsilon)
        right = rdp(points[idx:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]

def shrink_ring_to_max(points_lonlat, max_points, preferred_points):
    ring = points_lonlat[:]
    if ring[0] != ring[-1]:
        ring.append(ring[0])

    epsilon = 0.001
    best = ring

    for _ in range(30):
        simplified = rdp(ring, epsilon)
        if simplified[0] != simplified[-1]:
            simplified.append(simplified[0])

        if len(simplified) < len(best):
            best = simplified

        if len(simplified) <= max_points:
            best = simplified
            break

        epsilon *= 1.7

    if len(best) > max_points:
        step = math.ceil(len(best) / max_points)
        best = best[::step]
        if best[0] != best[-1]:
            best.append(best[0])

    if preferred_points and len(best) > preferred_points:
        epsilon2 = epsilon
        for _ in range(12):
            epsilon2 *= 1.4
            simplified2 = rdp(ring, epsilon2)
            if simplified2[0] != simplified2[-1]:
                simplified2.append(simplified2[0])
            if len(simplified2) <= max_points and len(simplified2) < len(best):
                best = simplified2
                epsilon = epsilon2
            if len(best) <= preferred_points:
                break

    return best, epsilon

def geojson_polygon_to_goodbarber_zones(geom):
    """
    Returns:
      zones_payload (Python object) shaped like [[{lat,lng}...]]
      original_n, simplified_n, epsilon_used
    """
    if not geom or geom.get("type") != "Polygon":
        return None, None, None, None

    outer = (geom.get("coordinates") or [None])[0]
    if not outer or len(outer) < 4:
        return None, None, None, None

    ring = [(lon, lat) for lon, lat in outer]
    original_n = len(ring)

    simplified_ring, eps = shrink_ring_to_max(ring, MAX_POINTS, PREFERRED_POINTS)
    simplified_n = len(simplified_ring)

    gb_points = [{"lat": lat, "lng": lon} for (lon, lat) in simplified_ring]
    return [[gb_points]], original_n, simplified_n, eps

def find_first_zone_polygon(session, affected_zones):
    for zurl in affected_zones or []:
        try:
            zdata, _, _ = fetch_json(session, zurl, headers=NWS_HEADERS)
            if not zdata:
                continue

            geom = zdata.get("geometry")
            if geom and geom.get("type") == "Polygon":
                return zurl, geom

            feats = zdata.get("features")
            if feats and feats[0].get("geometry", {}).get("type") == "Polygon":
                return zurl, feats[0]["geometry"]
        except Exception:
            continue
    return None, None

def choose_polygon_for_alert(session, alert_feature):
    """
    Prefer alert.geometry when present.
    Fallback to first affectedZone polygon.
    """
    geom = (alert_feature.get("geometry") or {})
    if geom and geom.get("type") == "Polygon":
        return "alert.geometry", geom

    props = alert_feature.get("properties") or {}
    affected = props.get("affectedZones") or []
    zurl, zgeom = find_first_zone_polygon(session, affected)
    if zgeom:
        return zurl, zgeom

    return None, None

# -----------------------------
# GOODBARBER LOGIN + PUSH SEND
# -----------------------------

@dataclass
class GBHiddenInputs:
    values: dict

_HIDDEN_INPUT_RE = re.compile(
    r'<input[^>]+type=["\']hidden["\'][^>]*>',
    flags=re.IGNORECASE,
)
_NAME_RE = re.compile(r'name=["\']([^"\']+)["\']', flags=re.IGNORECASE)
_VALUE_RE = re.compile(r'value=["\']([^"\']*)["\']', flags=re.IGNORECASE)

def parse_hidden_inputs(html: str) -> GBHiddenInputs:
    hidden = {}
    for tag in _HIDDEN_INPUT_RE.findall(html):
        mname = _NAME_RE.search(tag)
        if not mname:
            continue
        name = mname.group(1)
        mval = _VALUE_RE.search(tag)
        val = mval.group(1) if mval else ""
        hidden[name] = val
    return GBHiddenInputs(values=hidden)

def save_cookies(session: requests.Session, path: str):
    try:
        with open(path, "wb") as f:
            pickle.dump(session.cookies, f)
    except Exception:
        pass

def load_cookies(session: requests.Session, path: str):
    try:
        with open(path, "rb") as f:
            jar = pickle.load(f)
            session.cookies.update(jar)
    except Exception:
        pass


def gb_is_logged_in(session: requests.Session) -> bool:
    """
    Robust auth check:
    - GET push page
    - If it redirects to /manage/, not logged in
    - If it returns 200 but contains login form markers, not logged in
    - If it contains push form markers, logged in
    """
    try:
        resp = session.get(
            abs_url(GB_PUSH_SEND_PATH),
            headers=GB_HEADERS_BASE,
            timeout=20,
            allow_redirects=False,
        )

        # Redirect to login (common)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if loc.startswith(GB_LOGIN_PATH) or loc.startswith("/manage"):
                return False
            # Some other redirect; treat as not logged in to be safe
            return False

        if resp.status_code != 200:
            return False

        html = resp.text or ""

        # If we got the login page HTML, we're not logged in
        if 'id="form-index"' in html or 'name="identification"' in html or 'name="login"' in html:
            return False

        # If we see the push form markers, we are logged in
        if 'id="form-push"' in html and 'id="zones"' in html:
            return True

        # Unknown page; be conservative
        return False

    except Exception:
        return False


def gb_login(session: requests.Session):
    if not GB_LOGIN or not GB_PASSWORD:
        raise RuntimeError("Missing GB_LOGIN or GB_PASSWORD environment variables.")


    # Step 1: GET login page to establish cookies
    session.get(abs_url(GB_LOGIN_PATH), headers=GB_HEADERS_BASE, timeout=20)

    # Step 2: POST credentials
    payload = {
        "identification": "true",
        "login": GB_LOGIN,
        "password": GB_PASSWORD,
        # do NOT send honeypot fields (none present on login page snippet)
    }
    resp = session.post(
        abs_url(GB_LOGIN_PATH),
        headers=GB_HEADERS_BASE,
        data=payload,
        timeout=20,
        allow_redirects=False,
    )

    # Common outcomes: 302 to /manage/app/design/ (or similar)
    if resp.status_code in (301, 302):
        return

    # Sometimes returns 200 with an error message
    if resp.status_code == 200 and "Cannot login" in resp.text:
        raise RuntimeError("GoodBarber login failed (appears to still be on login page).")

def gb_get_push_hidden_inputs(session: requests.Session) -> GBHiddenInputs:
    resp = session.get(abs_url(GB_PUSH_SEND_PATH), headers=GB_HEADERS_BASE, timeout=20)
    resp.raise_for_status()
    return parse_hidden_inputs(resp.text)

def gb_send_push(session: requests.Session, message: str, zones_payload_obj):
    """
    zones_payload_obj: Python object shaped like [[{lat,lng}...]]
    Returns True on success (302 to history), else False.
    """
    hidden = gb_get_push_hidden_inputs(session).values

    picker_date, iso_date, heure, hh, mm = today_strings_local()

    # Start with all hidden inputs from the page (includes the dynamic token).
    payload = dict(hidden)

    # Override/set required fields (mirrors your captured POST closely)
    payload.update({
        "action": "mod",
        "type": "simple",
        "message": message,

        "linktype": "",
        "link": "",

        "pushDate": "now",
        "picker-date": picker_date,
        "date": iso_date,
        "heure": heure,
        "hour-heure": hh,
        "minutes-heure": mm,

        "platform-target-ios": "ios",
        "platform-target-android": "android",

        # Targeting: we are using zones, so "select" is appropriate
        "target": "select",
        "period_launch": "none",

        # PWA targeting fields (present in your post; harmless)
        "pwa-target": "all",
        "pwa-period_launch": "none",

        # Sound: Bells is value "03"
        "sound": "03",

        # Zones: must be a JSON string
        "zones": json.dumps(zones_payload_obj, separators=(",", ":")),
    })

    # Honeypot fields named 'address': omit, or keep empty.
    # Your captured POST included them empty. Either is fine; keeping empty matches capture:
    payload["address"] = ""

    headers = dict(GB_HEADERS_BASE)
    headers["Referer"] = abs_url(GB_PUSH_SEND_PATH)


    resp = session.post(
        abs_url(GB_PUSH_SEND_PATH),
        headers=headers,
        data=payload,
        timeout=25,
        allow_redirects=False,
    )

    if resp.status_code in (301, 302):
        loc = resp.headers.get("Location", "")
        return loc.startswith(GB_PUSH_HISTORY_PATH)
    return False

# -----------------------------
# MAIN LOOP
# -----------------------------

def main():
    nws = requests.Session()
    gb = requests.Session()

    # Try cookie reuse
    load_cookies(gb, COOKIE_JAR_FILE)

    etags = {s: None for s in STATES}
    last_modified = {s: None for s in STATES}
    seen_ids = set()

    print(f"[{now_utc()}] Starting NWS->GoodBarber poller")
    print(f"States: {STATES}, interval: {POLL_INTERVAL}s")
    print(f"Polygon shrink: preferred <= {PREFERRED_POINTS} points, hard cap <= {MAX_POINTS} points")
    print(f"Dashboard: {DASHBOARD_BASE}")
    print("")

    while True:
        # Ensure GB session is authenticated before each poll cycle (cheap + robust)
        try:
            if not gb_is_logged_in(gb):
                gb_login(gb)
                save_cookies(gb, COOKIE_JAR_FILE)
        except Exception as e:
            print(f"[{now_utc()}] GoodBarber auth error: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        for state in STATES:
            try:
                data, etags[state], last_modified[state] = fetch_json(
                    nws,
                    NWS_ALERTS_URL,
                    params={"area": state},
                    headers=NWS_HEADERS,
                    etag=etags[state],
                    last_modified=last_modified[state],
                )

                if data is None:
                    print(f"[{now_utc()}] {state}: no change")
                    continue

                features = data.get("features", [])
                new_features = []
                for f in features:
                    props = f.get("properties", {})
                    aid = props.get("id") or f.get("id")
                    if aid and aid not in seen_ids:
                        seen_ids.add(aid)
                        new_features.append(f)

                print(f"[{now_utc()}] {state}: {len(features)} active, {len(new_features)} new")

                for f in new_features:
                    props = f.get("properties", {})
                    event = props.get("event") or "Alert"
                    headline = props.get("headline") or ""
                    aid = props.get("id") or f.get("id") or ""

                    # Find polygon
                    poly_src, geom = choose_polygon_for_alert(nws, f)
                    if not geom:
                        print(f"  new: {event} | {headline}")
                        print("    no polygon available (alert.geometry absent; no affectedZone polygon)")
                        continue

                    zones_obj, original_n, simplified_n, eps = geojson_polygon_to_goodbarber_zones(geom)
                    if not zones_obj:
                        print(f"  new: {event} | {headline}")
                        print(f"    polygon present but conversion failed (src={poly_src})")
                        continue

                    # Compose message (keep it short; GB limit is 256 chars)
                    msg = f"{event}: {headline}".strip()
                    if len(msg) > 250:
                        msg = msg[:247] + "..."

                    print(f"  new: {event} | {headline}")
                    print(f"    polygon src: {poly_src}")
                    print(f"    points: {original_n} -> {simplified_n} (epsilon={eps:.6f})")

                    ok = gb_send_push(gb, msg, zones_obj)
                    if ok:
                        print(f"    GoodBarber: push queued (302 -> history) [alert id: {aid}]")
                    else:
                        print(f"    GoodBarber: push send failed (no 302->history) [alert id: {aid}]")

                    print("")

            except Exception as e:
                print(f"[{now_utc()}] {state}: error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
