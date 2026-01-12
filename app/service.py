import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests

from app.config import (
    COOKIE_JAR_FILE,
    DASHBOARD_BASE,
    NWS_ALERTS_URL,
    NWS_HEADERS,
    POLL_INTERVAL,
    SEEN_ALERTS_DB,
    SIMPLIFY_ENABLED,
    SIMPLIFY_TOLERANCE,
)
from app.gb_client import gb_is_logged_in, gb_login, gb_send_push, load_cookies, save_cookies
from app.geometry import geojson_to_shapely, shapely_to_goodbarber_zones, union_geometries
from app.nws_client import fetch_json, choose_geometries_for_alert
from app.storage import db_init, db_mark_seen, db_seen


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _format_union_type(union_geom):
    if union_geom is None:
        return "none"
    return union_geom.geom_type


def iter_active_alert_pages(session, start_url):
    page = 0
    url = start_url
    seen_urls = set()

    while True:
        if url in seen_urls:
            return
        seen_urls.add(url)

        data, _, _ = fetch_json(session, url, headers=NWS_HEADERS)
        if not data:
            return

        yield page, data, url

        nxt = (data.get("pagination") or {}).get("next")
        if not nxt:
            return

        url = urljoin(url, nxt)
        page += 1


def main():
    nws = requests.Session()
    gb = requests.Session()
    conn = sqlite3.connect(SEEN_ALERTS_DB)

    load_cookies(gb, COOKIE_JAR_FILE)
    db_init(conn)

    print(f"[{now_utc()}] Starting NWS->GoodBarber poller")
    print(f"Scope: nationwide, interval: {POLL_INTERVAL}s")
    print(f"Seen-alerts DB: {SEEN_ALERTS_DB}")
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

        try:
            for page_idx, data, url in iter_active_alert_pages(nws, NWS_ALERTS_URL):
                features = data.get("features", [])
                new_features = []
                for f in features:
                    props = f.get("properties", {})
                    aid = props.get("id") or f.get("id")
                    if aid and not db_seen(conn, aid):
                        new_features.append(f)
                    elif aid:
                        db_mark_seen(conn, aid)

                print(
                    f"[{now_utc()}] Page {page_idx}: {len(features)} active, "
                    f"{len(new_features)} new ({url})"
                )

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
                        if aid:
                            db_mark_seen(conn, aid)
                    else:
                        print(f"    GoodBarber: push send failed (no 302->history) [alert id: {aid}]")

                    print("")
        except Exception as e:
            print(f"[{now_utc()}] Nationwide poll error: {e}")

        time.sleep(POLL_INTERVAL)
