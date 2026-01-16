import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
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
from app.storage import db_init, db_mark_seen, db_prune_seen_before, db_seen


LOG_DIR = "logs"

LONG_UNTIL = re.compile(
    r"\buntil\s+(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})\s+at\s+(?P<time>\d{1,2}:\d{2}\s*[AP]M)",
    re.IGNORECASE,
)
NUM_UNTIL = re.compile(
    r"\buntil\s+(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s+(?P<month>\d{1,2})/(?P<day>\d{1,2})",
    re.IGNORECASE,
)
MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        1,
    )
}


def setup_logging():
    log_dir = LOG_DIR
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


def _clean_one_line(raw: str) -> str:
    return " ".join((raw or "").strip().split())


def _extract_title(s: str) -> str:
    if ":" in s:
        idx = s.find(":")
        if idx != -1 and idx + 1 < len(s) and s[idx + 1] == " ":
            return s[:idx].strip()
    idx = s.lower().find(" issued")
    return (s[:idx] if idx != -1 else s).strip()


def _fmt_time(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {ampm}"


def format_nws_notification(raw: str, *, year_default: int | None = None) -> str:
    s = _clean_one_line(raw)
    title = _extract_title(s)
    if year_default is None:
        year_default = datetime.now().year

    if " until " not in s.lower():
        return f"⚠️  {title} issued. Tap for details!"

    match = LONG_UNTIL.search(s)
    if match:
        month = MONTHS.get(match.group("month").lower())
        day = int(match.group("day"))
        t = datetime.strptime(match.group("time").replace(" ", "").upper(), "%I:%M%p").time()
        if month is not None:
            until_dt = datetime(year_default, month, day, t.hour, t.minute)
            return (
                f"⚠️  {title} issued until {_fmt_time(until_dt)} "
                f"{until_dt.strftime('%A')}! Tap for details!"
            )

    match = NUM_UNTIL.search(s)
    if match:
        month = int(match.group("month"))
        day = int(match.group("day"))
        t = datetime.strptime(match.group("time").replace(" ", "").upper(), "%I:%M%p").time()
        until_dt = datetime(year_default, month, day, t.hour, t.minute)
        return (
            f"⚠️  {title} issued until {_fmt_time(until_dt)} "
            f"{until_dt.strftime('%A')}! Tap for details!"
        )

    return f"⚠️  {title} issued. Tap for details!"


def prune_logs_before(log_dir: str, cutoff_ts: float) -> int:
    try:
        entries = os.listdir(log_dir)
    except FileNotFoundError:
        return 0

    removed = 0
    for name in entries:
        path = os.path.join(log_dir, name)
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            continue
        if not os.path.isfile(path):
            continue
        if stat.st_mtime < cutoff_ts:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                continue
    return removed


def iter_active_alert_pages(session, start_url):
    page = 0
    url = start_url
    seen_urls = set()
    params = {"region_type": "land", "message_type": "alert"}

    while True:
        if url in seen_urls:
            return
        seen_urls.add(url)

        query_params = params if page == 0 else None
        data, _, _ = fetch_json(session, url, headers=NWS_HEADERS, params=query_params)
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

    last_prune_key = None
    while True:
        now = datetime.now(timezone.utc)
        if now.day == 1:
            prune_key = (now.year, now.month)
            if prune_key != last_prune_key:
                cutoff = now - timedelta(days=30)
                cutoff_iso = cutoff.isoformat(timespec="seconds")
                pruned_db = db_prune_seen_before(conn, cutoff_iso)
                pruned_logs = prune_logs_before(LOG_DIR, cutoff.timestamp())
                logger.info(
                    "Monthly prune: removed %s seen alerts older than %s",
                    pruned_db,
                    cutoff_iso,
                )
                logger.info(
                    "Monthly prune: removed %s log files older than %s",
                    pruned_logs,
                    cutoff.date().isoformat(),
                )
                last_prune_key = prune_key

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
                    region_type = props.get("regionType") or props.get("region_type") or ""
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

                    raw_msg = headline or event
                    msg = format_nws_notification(raw_msg)
                    if len(msg) > 250:
                        msg = msg[:247] + "..."

                    logger.info("  new: %s | %s", event, headline)
                    logger.info("    geometries collected: %s", len(geoms))
                    logger.info("    union type: %s", union_type)
                    logger.info("    zones/rings emitted: %s", len(zones_obj))

                    time.sleep(random.uniform(1.5, 3))

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
