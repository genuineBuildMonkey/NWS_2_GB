import os


def _load_dotenv(env_path):
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


_load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

DASHBOARD_BASE = os.environ.get("DASHBOARD_BASE", "") 
GB_LOGIN_PATH = "/manage/"
GB_PUSH_SEND_PATH = "/manage/users/push/send/"
GB_PUSH_HISTORY_PATH = "/manage/users/push/history/"

# Credentials: keep out of source control
# export GB_LOGIN="..." GB_PASSWORD="..."
GB_LOGIN = os.environ.get("GB_LOGIN", "")
GB_PASSWORD = os.environ.get("GB_PASSWORD", "")

# Cookie cache file (so you don't log in every loop)
COOKIE_JAR_FILE = "goodbarber_cookies.pkl"

# Persist alert IDs to avoid duplicate notifications across runs.
SEEN_ALERTS_DB = "nws_alerts_seen.sqlite3"

# NWS polling
POLL_INTERVAL = 60  # call every this many seconds
IGNORED_EVENTS = [
    "Small Craft Advisory",
    "Special Marine Warning",
]

# Polygon simplification
MAX_POINTS = 300
PREFERRED_POINTS = 250
SIMPLIFY_ENABLED = True
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
