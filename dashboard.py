# dashboard.py
import os

from flask import Flask, Response, jsonify, request, send_from_directory

import database
import users as users_mod
from metrics import collect_metrics

app = Flask(__name__, static_folder="static")

# Defaults; monitor_bot overrides these via app.config before launching.
app.config.setdefault("DB_PATH", os.environ.get("MONITOR_DB", "data/metrics.db"))
app.config.setdefault("THRESHOLDS", {})
app.config.setdefault("AUTH", None)  # "user:password" enables HTTP Basic Auth.
app.config.setdefault("DISK_PATHS", None)  # None -> users.MONITORED_DISK_PATHS

MAX_WINDOW = 7 * 24 * 3600  # cap /history at one week


@app.before_request
def require_auth():
    cred = app.config.get("AUTH")
    if not cred:
        return None
    auth = request.authorization
    if not auth or f"{auth.username}:{auth.password}" != cred:
        return Response(
            "Authentication required", 401,
            {"WWW-Authenticate": 'Basic realm="System Monitor"'},
        )
    return None


@app.route("/metrics")
def metrics():
    return jsonify(collect_metrics())


@app.route("/history")
def history():
    seconds = request.args.get("seconds", default=3600, type=int)
    seconds = max(60, min(seconds, MAX_WINDOW))
    return jsonify(database.fetch_since(app.config["DB_PATH"], seconds))


@app.route("/users")
def users():
    """List of users available to overlay on the charts (seen in the last hour)."""
    return jsonify({"users": database.list_users(app.config["DB_PATH"], 3600)})


@app.route("/users/current")
def users_current():
    """Live per-user CPU and RAM percentages, sorted by CPU descending."""
    bd = users_mod.breakdown(app.config.get("DISK_PATHS"))
    rows = [
        {"user": u, "cpu": m["cpu"], "ram": m["ram"]}
        for u, m in bd.items()
    ]
    rows.sort(key=lambda r: r["cpu"], reverse=True)
    return jsonify(rows)


@app.route("/users/history")
def users_history():
    seconds = request.args.get("seconds", default=3600, type=int)
    seconds = max(60, min(seconds, MAX_WINDOW))
    names = [n for n in request.args.get("users", "").split(",") if n]
    return jsonify(database.fetch_user_history(app.config["DB_PATH"], seconds, names))


@app.route("/config")
def config():
    return jsonify({"thresholds": app.config.get("THRESHOLDS", {})})


@app.route("/")
def index():
    return send_from_directory("static", "index.html")
