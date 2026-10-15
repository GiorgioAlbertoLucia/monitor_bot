# database.py
# Lightweight SQLite store for metric history, so the dashboard survives restarts.
import os
import sqlite3
import time

COLUMNS = ("ts", "cpu", "ram", "disk", "disk2", "temp")
USER_COLUMNS = ("ts", "user", "cpu", "ram", "disk", "disk2")

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    ts    INTEGER PRIMARY KEY,
    cpu   REAL,
    ram   REAL,
    disk  REAL,
    disk2 REAL,
    temp  REAL
);
"""

# Per-user contributions, so a user's lines can be overlaid on the charts.
USER_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_metrics (
    ts    INTEGER,
    user  TEXT,
    cpu   REAL,
    ram   REAL,
    disk  REAL,
    disk2 REAL,
    PRIMARY KEY (ts, user)
);
CREATE INDEX IF NOT EXISTS idx_user_metrics_user_ts ON user_metrics (user, ts);
"""


def _connect(path):
    # A short busy timeout lets the Flask reader and the bot writer share the file.
    return sqlite3.connect(path, timeout=5)


def init_db(path):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with _connect(path) as conn:
        conn.execute(SCHEMA)
        conn.executescript(USER_SCHEMA)


def insert(path, metrics, ts=None):
    ts = int(ts if ts is not None else time.time())
    with _connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO metrics (ts, cpu, ram, disk, disk2, temp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, metrics.get("cpu"), metrics.get("ram"),
             metrics.get("disk"), metrics.get("disk2"), metrics.get("temp")),
        )


def fetch_since(path, seconds, bucket=1):
    """Return rows recorded in the last ``seconds``, oldest first, as dicts.

    Samples are grouped into ``bucket``-second slots keeping each slot's peak, so
    long windows stay light without hiding the spikes that trigger alerts.
    """
    cutoff = int(time.time()) - seconds
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT (ts / ?) * ? AS b, MAX(cpu), MAX(ram), MAX(disk), MAX(disk2), MAX(temp) "
            "FROM metrics WHERE ts >= ? GROUP BY b ORDER BY b ASC",
            (bucket, bucket, cutoff),
        ).fetchall()
    return [dict(zip(COLUMNS, row)) for row in rows]


def insert_users(path, ts, rows):
    """rows: {username: {cpu, ram, disk, disk2}} recorded at timestamp ``ts``."""
    ts = int(ts)
    payload = [
        (ts, user, m.get("cpu"), m.get("ram"), m.get("disk"), m.get("disk2"))
        for user, m in rows.items()
    ]
    if not payload:
        return
    with _connect(path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO user_metrics (ts, user, cpu, ram, disk, disk2) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            payload,
        )


def list_users(path, seconds):
    """Distinct users that have records in the last ``seconds``."""
    cutoff = int(time.time()) - seconds
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT user FROM user_metrics WHERE ts >= ? ORDER BY user",
            (cutoff,),
        ).fetchall()
    return [r[0] for r in rows]


def fetch_user_history(path, seconds, usernames, bucket=1):
    """Per-user rows in the last ``seconds`` for the given users, oldest first,
    grouped into ``bucket``-second slots like ``fetch_since``."""
    if not usernames:
        return []
    cutoff = int(time.time()) - seconds
    placeholders = ",".join("?" * len(usernames))
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT (ts / ?) * ? AS b, user, MAX(cpu), MAX(ram), MAX(disk), MAX(disk2) "
            f"FROM user_metrics WHERE ts >= ? AND user IN ({placeholders}) "
            "GROUP BY b, user ORDER BY b ASC",
            (bucket, bucket, cutoff, *usernames),
        ).fetchall()
    return [dict(zip(USER_COLUMNS, row)) for row in rows]


def prune(path, retention_seconds):
    cutoff = int(time.time()) - retention_seconds
    with _connect(path) as conn:
        conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM user_metrics WHERE ts < ?", (cutoff,))
