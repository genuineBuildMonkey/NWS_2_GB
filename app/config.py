import os

DASHBOARD_BASE = "https://nwsgbnoti.blizapps.com"
GB_LOGIN_PATH = "/manage/"
GB_PUSH_SEND_PATH = "/manage/users/push/send/"
GB_PUSH_HISTORY_PATH = "/manage/users/push/history/"

# Credentials: keep out of source control
# export GB_LOGIN="..." GB_PASSWORD="..."
GB_LOGIN = os.environ.get("GB_LOGIN", "")
GB_PASSWORD = os.environ.get("GB_PASSWORD", "")

# Cookie cache file (so you don't log in every loop)
COOKIE_JAR_FILE = "goodbarber_cookies.pkl"

# NWS polling
STATES = ["WY", "NM", "FL"]
POLL_INTERVAL = 60  # call every this many seconds

# Polygon simplification
MAX_POINTS = 20
PREFERRED_POINTS = 8
SIMPLIFY_ENABLED = False
SIMPLIFY_TOLERANCE = 0.001

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
