import time
from datetime import datetime, timezone

import requests

from app.config import (
    COOKIE_JAR_FILE,
    DASHBOARD_BASE,
    NWS_ALERTS_URL,
    NWS_HEADERS,
    POLL_INTERVAL,
    SIMPLIFY_ENABLED,
    SIMPLIFY_TOLERANCE,
    STATES,
)
from app.gb_client import gb_is_logged_in, gb_login, gb_send_push, load_cookies, save_cookies
from app.geometry import geojson_to_shapely, shapely_to_goodbarber_zones, union_geometries
from app.nws_client import fetch_json, choose_geometries_for_alert


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _format_union_type(union_geom):
    if union_geom is None:
        return "none"
    return union_geom.geom_type


def main():
    nws = requests.Session()
    gb = requests.Session()

    load_cookies(gb, COOKIE_JAR_FILE)

    etags = {s: None for s in STATES}
    last_modified = {s: None for s in STATES}
    seen_ids = set()

    print(f"[{now_utc()}] Starting NWS->GoodBarber poller")
    print(f"States: {STATES}, interval: {POLL_INTERVAL}s")
    if SIMPLIFY_ENABLED:
        print(f"Polygon simplify: enabled (tolerance={SIMPLIFY_TOLERANCE})")
    else:
        print("Polygon simplify: disabled")
    print(f"Dashboard: {DASHBOARD_BASE}")
    print("")

    while True:
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

                    sources, geoms = choose_geometries_for_alert(nws, f)
                    if not geoms:
                        print(f"  new: {event} | {headline}")
                        print("    no geometry available (alert.geometry absent; no affectedZone polygons)")
                        continue

                    shapely_geoms = [geojson_to_shapely(g) for g in geoms]
                    shapely_geoms = [g for g in shapely_geoms if g is not None]
                    union_geom = union_geometries(shapely_geoms)
                    union_type = _format_union_type(union_geom)

                    zones_obj = shapely_to_goodbarber_zones(union_geom)
                    if not zones_obj:
                        print(f"  new: {event} | {headline}")
                        print(f"    polygon present but conversion failed (sources={len(sources)})")
                        print(f"    union type: {union_type}")
                        continue

                    msg = f"{event}: {headline}".strip()
                    if len(msg) > 250:
                        msg = msg[:247] + "..."

                    print(f"  new: {event} | {headline}")
                    print(f"    geometries collected: {len(geoms)}")
                    print(f"    union type: {union_type}")
                    print(f"    zones/rings emitted: {len(zones_obj)}")

                    ok = gb_send_push(gb, msg, zones_obj)
                    if ok:
                        print(f"    GoodBarber: push queued (302 -> history) [alert id: {aid}]")
                    else:
                        print(f"    GoodBarber: push send failed (no 302->history) [alert id: {aid}]")

                    print("")

            except Exception as e:
                print(f"[{now_utc()}] {state}: error: {e}")

        time.sleep(POLL_INTERVAL)
