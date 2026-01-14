import json
import logging
import pickle
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from app.config import (
    DASHBOARD_BASE,
    GB_HEADERS_BASE,
    GB_LOGIN,
    GB_LOGIN_PATH,
    GB_PASSWORD,
    GB_PUSH_HISTORY_PATH,
    GB_PUSH_SEND_PATH,
)

logger = logging.getLogger(__name__)


@dataclass
class GBHiddenInputs:
    values: dict


_HIDDEN_INPUT_RE = re.compile(
    r'<input[^>]+type=["\']hidden["\'][^>]*>',
    flags=re.IGNORECASE,
)
_NAME_RE = re.compile(r'name=["\']([^"\']+)["\']', flags=re.IGNORECASE)
_VALUE_RE = re.compile(r'value=["\']([^"\']*)["\']', flags=re.IGNORECASE)


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
    local = datetime.now()
    picker_date = local.strftime("%m/%d/%Y")
    iso_date = local.strftime("%Y-%m-%d")
    hh = local.strftime("%H")
    mm = local.strftime("%M")
    heure = f"{hh}:{mm}"
    return picker_date, iso_date, heure, hh, mm


def abs_url(path: str) -> str:
    return DASHBOARD_BASE.rstrip("/") + path


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

        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if loc.startswith(GB_LOGIN_PATH) or loc.startswith("/manage"):
                return False
            return False

        if resp.status_code != 200:
            return False

        html = resp.text or ""

        if 'id="form-index"' in html or 'name="identification"' in html or 'name="login"' in html:
            return False

        if 'id="form-push"' in html and 'id="zones"' in html:
            return True

        return False

    except Exception:
        return False


def gb_login(session: requests.Session):
    if not GB_LOGIN or not GB_PASSWORD:
        raise RuntimeError("Missing GB_LOGIN or GB_PASSWORD environment variables.")

    session.get(abs_url(GB_LOGIN_PATH), headers=GB_HEADERS_BASE, timeout=20)

    payload = {
        "identification": "true",
        "login": GB_LOGIN,
        "password": GB_PASSWORD,
    }
    resp = session.post(
        abs_url(GB_LOGIN_PATH),
        headers=GB_HEADERS_BASE,
        data=payload,
        timeout=20,
        allow_redirects=False,
    )

    if resp.status_code in (301, 302):
        return

    if resp.status_code == 200 and "Cannot login" in resp.text:
        raise RuntimeError("GoodBarber login failed (appears to still be on login page).")


def gb_get_push_hidden_inputs(session: requests.Session) -> GBHiddenInputs:
    resp = session.get(abs_url(GB_PUSH_SEND_PATH), headers=GB_HEADERS_BASE, timeout=20)
    resp.raise_for_status()
    return parse_hidden_inputs(resp.text)


def gb_send_push(session: requests.Session, message: str, zones_payload_obj):
    """
    zones_payload_obj: Python object shaped like [[{lat,lng}...], ...]
    Returns (ok, resp) where ok=True means 302 to history.
    """
    try:
        hidden = gb_get_push_hidden_inputs(session).values
    except Exception:
        logger.exception("GoodBarber push: failed to load hidden inputs")
        raise

    picker_date, iso_date, heure, hh, mm = today_strings_local()

    payload = dict(hidden)

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
        "target": "select",
        "period_launch": "none",
        "pwa-target": "all",
        "pwa-period_launch": "none",
        "sound": "03",
        "zones": json.dumps(zones_payload_obj, separators=(",", ":")),
    })

    payload["address"] = ""

    headers = dict(GB_HEADERS_BASE)
    headers["Referer"] = abs_url(GB_PUSH_SEND_PATH)

    resp = None
    for attempt in range(4):
        try:
            resp = session.post(
                abs_url(GB_PUSH_SEND_PATH),
                headers=headers,
                data=payload,
                timeout=(5, 15),
                allow_redirects=False,
            )
            break
        except requests.exceptions.Timeout:
            sleep_for = (2 ** (attempt + 1)) + random.uniform(1.0, 2.0)
            logger.error(
                "GoodBarber push: timeout on attempt %s; retrying in %.1fs",
                attempt + 1,
                sleep_for,
            )
            time.sleep(sleep_for)
            continue

    if resp is None:
        logger.error("GoodBarber push: exhausted retries without response")
        return False, None

    if resp.status_code in (301, 302):
        loc = resp.headers.get("Location", "")
        if not loc.startswith(GB_PUSH_HISTORY_PATH):
            logger.error(
                "GoodBarber push: unexpected redirect status=%s location=%s",
                resp.status_code,
                loc,
            )
        return loc.startswith(GB_PUSH_HISTORY_PATH), resp
    body = (resp.text or "").replace("\n", " ").strip()
    if len(body) > 300:
        body = body[:297] + "..."
    logger.error(
        "GoodBarber push: unexpected status=%s location=%s body=%s",
        resp.status_code,
        resp.headers.get("Location", ""),
        body,
    )
    return False, resp
