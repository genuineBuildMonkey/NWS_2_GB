NWS to GoodBarber Poller

Lightweight service that polls the NWS active alerts feed and sends polygon-based
push notifications to a GoodBarber dashboard.

Quick start

1) Create a .env file (or export vars) with:
   - DASHBOARD_BASE=https://your.goodbarber.domain
   - GB_LOGIN=your_login
   - GB_PASSWORD=your_password

2) Install dependencies:
   - python -m venv .venv
   - . .venv/bin/activate
   - pip install -r requirements.txt

3) Run:
   - python main.py

Logging

- Console output shows normal activity.
- Error logs are written to logs/nws_goodbarber_YYYY-MM-DD.log.
- On the first day of each month, the service prunes:
  - seen alerts older than 30 days from the sqlite DB
  - log files older than 30 days from logs/

Data files

- cookies: goodbarber_cookies.pkl
- seen alerts DB: nws_alerts_seen.sqlite3

Notes

- This is a long-running poller. Use systemd or another supervisor if you want
  automatic restarts.
