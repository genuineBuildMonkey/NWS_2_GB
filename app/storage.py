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

    # 1) Insert if new
    conn.execute(
        """
        INSERT OR IGNORE INTO seen_alerts(alert_id, first_seen_at, last_seen_at)
        VALUES (?, ?, ?)
        """,
        (alert_id, ts, ts),
    )

    # 2) Always refresh last_seen_at (for existing rows this updates; for new rows it matches)
    conn.execute(
        """
        UPDATE seen_alerts
        SET last_seen_at = ?
        WHERE alert_id = ?
        """,
        (ts, alert_id),
    )

    conn.commit()


def db_prune_seen_before(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    cursor = conn.execute(
        "DELETE FROM seen_alerts WHERE last_seen_at < ?",
        (cutoff_iso,),
    )
    conn.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0
