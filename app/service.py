import logging
import os
import random
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
    IGNORED_EVENTS,
)
from app.gb_client import gb_is_logged_in, gb_login, gb_send_push, load_cookies, save_cookies
from app.geometry import geojson_to_shapely, shapely_to_goodbarber_zones, union_geometries
from app.nws_client import fetch_json, choose_geometries_for_alert
from app.storage import db_init, db_mark_seen, db_seen


def setup_logging():
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = os.path.join(log_dir, f"nws_goodbarber_{date_tag}.log")

    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(name)s: %(message)s"
    )
    formatter.converter = time.gmtime

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers = []

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    logfile = logging.FileHandler(log_path, encoding="utf-8")
    logfile.setLevel(logging.ERROR)
    logfile.setFormatter(formatter)
    root.addHandler(logfile)


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
    setup_logging()
    logger = logging.getLogger(__name__)

    nws = requests.Session()
    gb = requests.Session()
    conn = sqlite3.connect(SEEN_ALERTS_DB)

    load_cookies(gb, COOKIE_JAR_FILE)
    db_init(conn)

    logger.info("Starting NWS->GoodBarber poller")
    logger.info("Scope: nationwide, interval: %ss", POLL_INTERVAL)
    logger.info("Seen-alerts DB: %s", SEEN_ALERTS_DB)
    if SIMPLIFY_ENABLED:
        logger.info("Polygon simplify: enabled (tolerance=%s)", SIMPLIFY_TOLERANCE)
    else:
        logger.info("Polygon simplify: disabled")
    logger.info("Dashboard: %s", DASHBOARD_BASE)

    while True:
        try:
            if not gb_is_logged_in(gb):
                gb_login(gb)
                save_cookies(gb, COOKIE_JAR_FILE)
        except Exception as e:
            logger.exception("GoodBarber auth error: %s", e)
            time.sleep(POLL_INTERVAL)
            continue

        try:
            for page_idx, data, url in iter_active_alert_pages(nws, NWS_ALERTS_URL):
                gb_requests = 0

                features = data.get("features", [])
                new_features = []
                for f in features:
                    props = f.get("properties", {})
                    aid = props.get("id") or f.get("id")
                    if aid and not db_seen(conn, aid):
                        new_features.append(f)
                    elif aid:
                        db_mark_seen(conn, aid)

                logger.info(
                    "Page %s: %s active, %s new (%s)",
                    page_idx,
                    len(features),
                    len(new_features),
                    url,
                )

                for f in new_features:
                    props = f.get("properties", {})
                    event = props.get("event") or "Alert"
                    message_type = props.get("messageType") or ""
                    if event in IGNORED_EVENTS:
                        continue
                    if message_type != "Alert":
                        continue
                    headline = props.get("headline") or ""
                    aid = props.get("id") or f.get("id") or ""

                    geom = f.get("geometry") or {}
                    affected = props.get("affectedZones") or []
                    if geom.get("type") not in ("Polygon", "MultiPolygon") and not affected:
                        logger.info("  new: %s | %s", event, headline)
                        logger.info("    no geometry available (alert.geometry absent; no affectedZone polygons)")
                        if aid:
                            db_mark_seen(conn, aid)
                        continue

                    sources, geoms = choose_geometries_for_alert(nws, f)
                    if not geoms:
                        logger.info("  new: %s | %s", event, headline)
                        logger.info("    no geometry available (alert.geometry absent; no affectedZone polygons)")
                        if aid:
                            db_mark_seen(conn, aid)
                        continue

                    shapely_geoms = [geojson_to_shapely(g) for g in geoms]
                    shapely_geoms = [g for g in shapely_geoms if g is not None]
                    union_geom = union_geometries(shapely_geoms)
                    union_type = _format_union_type(union_geom)

                    zones_obj = shapely_to_goodbarber_zones(union_geom)
                    if not zones_obj:
                        logger.info("  new: %s | %s", event, headline)
                        logger.info("    polygon present but conversion failed (sources=%s)", len(sources))
                        logger.info("    union type: %s", union_type)
                        continue

                    msg = f"{event}: {headline}".strip()
                    if len(msg) > 250:
                        msg = msg[:247] + "..."

                    logger.info("  new: %s | %s", event, headline)
                    logger.info("    geometries collected: %s", len(geoms))
                    logger.info("    union type: %s", union_type)
                    logger.info("    zones/rings emitted: %s", len(zones_obj))

                    time.sleep(random.uniform(2.5, 3))
                    ok, resp = gb_send_push(gb, msg, zones_obj)
                    gb_requests += 1
                    if gb_requests % 24 == 0:
                        time.sleep(random.uniform(60, 180))
                    if ok:
                        logger.info("    GoodBarber: push queued (302 -> history) [alert id: %s]", aid)
                        if aid:
                            db_mark_seen(conn, aid)
                    else:
                        logger.error(
                            "    GoodBarber: push send failed (no 302->history) [alert id: %s]",
                            aid,
                        )
                        status = resp.status_code if resp is not None else "unknown"
                        location = resp.headers.get("Location", "") if resp is not None else ""
                        body = (resp.text or "") if resp is not None else ""
                        body = body.replace("\n", " ").strip()
                        if len(body) > 500:
                            body = body[:497] + "..."
                        logger.error(
                            "    GoodBarber response: status=%s location='%s'",
                            status,
                            location,
                        )
                        if body:
                            logger.error("    GoodBarber body: %s", body)
        except Exception as e:
            logger.exception("Nationwide poll error: %s", e)

        time.sleep(POLL_INTERVAL)
