import sqlite3
from datetime import datetime, timezone


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_alerts (
            alert_id TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_seen_at  TEXT NOT NULL
        )
        """
    )
    conn.commit()


def db_seen(conn: sqlite3.Connection, alert_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM seen_alerts WHERE alert_id=?",
        (alert_id,),
    ).fetchone()
    return row is not None


def db_mark_seen(conn: sqlite3.Connection, alert_id: str) -> None:
    ts = now_utc()
    conn.execute(
        """
        INSERT INTO seen_alerts(alert_id, first_seen_at, last_seen_at)
        VALUES (?, ?, ?)
        ON CONFLICT(alert_id) DO UPDATE SET
            last_seen_at=excluded.last_seen_at
        """,
        (alert_id, ts, ts),
    )
    conn.commit()
