# users.py
# Per-user contributions to CPU / RAM / disk, recorded over time so a user's
# usage can be overlaid as extra lines on the dashboard charts.
import os
import pwd
import subprocess
import time

import psutil

# Mapped onto the dashboard chart keys: first path -> "disk", second -> "disk2".
MONITORED_DISK_PATHS = ["/home", "/data"]
DISK_KEYS = ["disk", "disk2"]

# Disk usage is expensive (shells out to du), so cache it briefly.
DISK_TTL = 120  # seconds
_disk_cache = {"ts": 0.0, "data": {}}


def cpu_by_user():
    """{user: CPU%} as a share of total capacity (0-100)."""
    # process_iter caches Process objects across calls, so priming then reading works.
    for p in psutil.process_iter():
        try:
            p.cpu_percent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(0.5)

    totals = {}
    for p in psutil.process_iter(["username"]):
        try:
            cpu = p.cpu_percent()
            user = p.info["username"] or "unknown"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        totals[user] = totals.get(user, 0.0) + cpu

    ncpu = psutil.cpu_count() or 1
    return {u: v / ncpu for u, v in totals.items() if v > 0}


def ram_by_user():
    """{user: RAM%} as a share of total memory."""
    total = psutil.virtual_memory().total or 1
    sums = {}
    for p in psutil.process_iter(["username", "memory_info"]):
        try:
            info = p.info
            rss = info["memory_info"].rss if info["memory_info"] else 0
            user = info["username"] or "unknown"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        sums[user] = sums.get(user, 0) + rss
    return {u: b / total * 100 for u, b in sums.items() if b > 0}


def disk_by_user(paths=None, ttl=DISK_TTL):
    """{path: {user: %}} — each user's share of that mount, cached for ``ttl`` seconds."""
    paths = paths or MONITORED_DISK_PATHS
    now = time.time()
    if _disk_cache["data"] and (now - _disk_cache["ts"]) < ttl:
        return _disk_cache["data"]

    result = {}
    for path in paths:
        try:
            total = psutil.disk_usage(path).total
        except Exception:
            total = 0
        sums = {}
        try:
            out = subprocess.check_output(
                f"du -s {path}/*", shell=True, text=True, stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError as e:
            out = e.output or ""
        for line in out.strip().splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            size_kb, name = int(parts[0]), parts[1]
            try:
                owner = pwd.getpwuid(os.stat(name).st_uid).pw_name
            except Exception:
                owner = "unknown"
            sums[owner] = sums.get(owner, 0) + size_kb * 1024
        result[path] = (
            {u: b / total * 100 for u, b in sums.items()} if total > 0 else {}
        )

    _disk_cache.update(ts=now, data=result)
    return result


def breakdown(disk_paths=None):
    """{user: {cpu, ram, disk, disk2}} percentages, for every user with any usage."""
    disk_paths = disk_paths or MONITORED_DISK_PATHS
    cpu = cpu_by_user()
    ram = ram_by_user()
    disk = disk_by_user(disk_paths)
    disk_by_key = {key: disk.get(path, {}) for key, path in zip(DISK_KEYS, disk_paths)}

    everyone = set(cpu) | set(ram)
    for shares in disk_by_key.values():
        everyone |= set(shares)

    rows = {}
    for user in everyone:
        rows[user] = {
            "cpu":   round(cpu.get(user, 0.0), 1),
            "ram":   round(ram.get(user, 0.0), 1),
            "disk":  round(disk_by_key.get("disk", {}).get(user, 0.0), 1),
            "disk2": round(disk_by_key.get("disk2", {}).get(user, 0.0), 1),
        }
    return rows
