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


def fetch_since(path, seconds):
    """Return rows recorded in the last ``seconds``, oldest first, as dicts."""
    cutoff = int(time.time()) - seconds
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT ts, cpu, ram, disk, disk2, temp FROM metrics "
            "WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
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


def fetch_user_history(path, seconds, usernames):
    """Per-user rows in the last ``seconds`` for the given users, oldest first."""
    if not usernames:
        return []
    cutoff = int(time.time()) - seconds
    placeholders = ",".join("?" * len(usernames))
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT ts, user, cpu, ram, disk, disk2 FROM user_metrics "
            f"WHERE ts >= ? AND user IN ({placeholders}) ORDER BY ts ASC",
            (cutoff, *usernames),
        ).fetchall()
    return [dict(zip(USER_COLUMNS, row)) for row in rows]


def prune(path, retention_seconds):
    cutoff = int(time.time()) - retention_seconds
    with _connect(path) as conn:
        conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM user_metrics WHERE ts < ?", (cutoff,))
