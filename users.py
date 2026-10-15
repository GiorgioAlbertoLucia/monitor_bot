# users.py
# Per-user contributions to CPU / RAM / disk, recorded over time so a user's
# usage can be overlaid as extra lines on the dashboard charts.
import os
import pwd
import re
import subprocess
import threading
import time

import psutil

# Mapped onto the dashboard chart keys: first path -> "disk", second -> "disk2".
MONITORED_DISK_PATHS = ["/home", "/data"]
DISK_KEYS = ["disk", "disk2"]

# cgroup v2 login slices (user-<uid>.slice), used for page-accurate per-user RAM.
CGROUP_USER_SLICES = "/sys/fs/cgroup/user.slice"

# Disk usage is expensive (du over /data takes ~30 s), so it is cached per path and,
# once start_disk_refresher() has been called, refreshed by a background thread so
# callers never wait for du.
DISK_TTL = 120  # seconds
_disk_cache = {}  # path -> (scanned_at, {user: %})
_disk_lock = threading.Lock()
_refresher = None


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


# Interpreters whose script, not the binary, says what a process is.
INTERPRETERS = {"python", "python3", "node", "bash", "sh", "perl", "Rscript", "root", "root.exe", "java"}


def process_label(name, cmdline):
    """Short readable name: the program from the command line (thread names such as
    "MainThread" hide it), plus the script for interpreters like python."""
    if not cmdline:
        return name
    # Programs that rewrite their title ("avahi-daemon: running") put it all in argv[0].
    words = cmdline[0].split()
    prog = os.path.basename(words[0]).rstrip(":") if words else name
    if re.sub(r"[\d.]+$", "", prog) in INTERPRETERS or prog in INTERPRETERS:
        args = cmdline[1:]
        script = None
        for i, a in enumerate(args):
            if a == "-c":
                script = "-c"  # inline code: the code itself is no name
                break
            if a == "-m" and i + 1 < len(args):
                script = "-m " + args[i + 1]
                break
            if a and not a.startswith("-"):
                script = os.path.basename(a.split()[0])
                break
        if script:
            prog += " " + script
    return prog[:40]


def top_processes(n=5):
    """The ``n`` heaviest processes by CPU and by RAM, as lists of dicts with
    pid, user, name, cpu (share of total capacity, 0-100) and ram (bytes)."""
    for p in psutil.process_iter():
        try:
            p.cpu_percent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(0.5)

    ncpu = psutil.cpu_count() or 1
    procs = []
    for p in psutil.process_iter(["username", "name", "cmdline", "memory_info"]):
        try:
            procs.append({
                "pid":  p.pid,
                "user": p.info["username"] or "unknown",
                "name": process_label(p.info["name"] or "?", p.info["cmdline"]),
                "cpu":  p.cpu_percent() / ncpu,
                "ram":  p.info["memory_info"].rss if p.info["memory_info"] else 0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    by_cpu = sorted(procs, key=lambda x: x["cpu"], reverse=True)[:n]
    by_ram = sorted(procs, key=lambda x: x["ram"], reverse=True)[:n]
    return by_cpu, by_ram


def _cgroup_user_memory():
    """{uid: bytes} of program memory (anon + shmem, no page cache) charged to each
    user's login slice. Empty if cgroup v2 user slices aren't available.

    The kernel charges every physical page once, so forked workers sharing
    copy-on-write memory aren't counted once per process, as summing RSS does.
    """
    usage = {}
    try:
        slices = os.listdir(CGROUP_USER_SLICES)
    except OSError:
        return usage
    for name in slices:
        m = re.fullmatch(r"user-(\d+)\.slice", name)
        if not m:
            continue
        try:
            with open(os.path.join(CGROUP_USER_SLICES, name, "memory.stat")) as f:
                stat = dict(line.split() for line in f)
        except (OSError, ValueError):
            continue
        usage[int(m.group(1))] = int(stat.get("anon", 0)) + int(stat.get("shmem", 0))
    return usage


def _in_user_slice(pid):
    """True if the process belongs to a login session (so its cgroup accounts for it)."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            return "/user.slice/" in f.read()
    except OSError:
        return False


def ram_by_user():
    """{user: RAM%} as a share of total memory.

    Login sessions are read from their cgroup (page-accurate). Processes outside
    one (system services, cron jobs) fall back to summed RSS, which counts memory
    shared between forked processes once per process.
    """
    total = psutil.virtual_memory().total or 1
    by_uid = _cgroup_user_memory()
    sums = {}
    for uid, used in by_uid.items():
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = str(uid)
        sums[user] = sums.get(user, 0) + used

    for p in psutil.process_iter(["username", "memory_info"]):
        try:
            info = p.info
            if not info["memory_info"] or (by_uid and _in_user_slice(p.pid)):
                continue
            rss = info["memory_info"].rss
            user = info["username"] or "unknown"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        sums[user] = sums.get(user, 0) + rss
    return {u: b / total * 100 for u, b in sums.items() if b > 0}


def _scan_disk(path):
    """One du pass over ``path``: {user: %} share of that mount. Slow."""
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
    return {u: b / total * 100 for u, b in sums.items()} if total > 0 else {}


def disk_by_user(paths=None, ttl=DISK_TTL):
    """{path: {user: %}} — each user's share of that mount.

    With the background refresher running this never blocks, and a path whose first
    scan hasn't finished yet is left out. Without it, stale paths are scanned inline
    and cached for ``ttl`` seconds.
    """
    paths = paths or MONITORED_DISK_PATHS
    result = {}
    for path in paths:
        with _disk_lock:
            entry = _disk_cache.get(path)
        if _refresher is None and (entry is None or time.time() - entry[0] >= ttl):
            entry = (time.time(), _scan_disk(path))
            with _disk_lock:
                _disk_cache[path] = entry
        if entry is not None:
            result[path] = entry[1]
    return result


def start_disk_refresher(paths=None, every=DISK_TTL):
    """Rescan the disks in a daemon thread, pausing ``every`` seconds between passes."""
    global _refresher
    if _refresher is not None:
        return
    paths = list(paths or MONITORED_DISK_PATHS)

    def loop():
        while True:
            for path in paths:
                try:
                    shares = _scan_disk(path)
                except Exception as e:
                    print(f"Disk scan of {path} failed: {e}")
                    continue
                with _disk_lock:
                    _disk_cache[path] = (time.time(), shares)
            time.sleep(every)

    _refresher = threading.Thread(target=loop, name="disk-refresher", daemon=True)
    _refresher.start()


def breakdown(disk_paths=None):
    """{user: {cpu, ram, disk, disk2}} percentages, for every user with any usage.

    A disk whose first scan hasn't finished yet is reported as None, not 0.
    """
    disk_paths = disk_paths or MONITORED_DISK_PATHS
    cpu = cpu_by_user()
    ram = ram_by_user()
    disk = disk_by_user(disk_paths)
    disk_by_key = {key: disk.get(path) for key, path in zip(DISK_KEYS, disk_paths)}

    everyone = set(cpu) | set(ram)
    for shares in disk_by_key.values():
        everyone |= set(shares or {})

    def disk_share(key, user):
        shares = disk_by_key.get(key)
        return None if shares is None else round(shares.get(user, 0.0), 1)

    rows = {}
    for user in everyone:
        rows[user] = {
            "cpu":   round(cpu.get(user, 0.0), 1),
            "ram":   round(ram.get(user, 0.0), 1),
            "disk":  disk_share("disk", user),
            "disk2": disk_share("disk2", user),
        }
    return rows
