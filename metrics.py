# metrics.py
# Single source of truth for the metrics sampled by both the dashboard and the bot.
import psutil


def cpu_temperature():
    """Return a representative CPU temperature in °C, or None if unavailable."""
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors is None:
        return None
    try:
        temps = sensors()
    except Exception:
        return None
    if not temps:
        return None

    # Prefer the well-known CPU sensor chips, in order.
    preferred = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "acpitz")
    for name in preferred:
        entries = temps.get(name)
        if not entries:
            continue
        # Within a chip, prefer the package/die reading; otherwise the hottest core.
        labeled = [e for e in entries if e.label in ("Package id 0", "Tctl", "Tdie", "CPU")]
        chosen = labeled[0] if labeled else max(entries, key=lambda e: e.current or 0)
        if chosen.current is not None:
            return round(chosen.current, 1)

    # Fallback: hottest reading across every sensor.
    vals = [e.current for entries in temps.values() for e in entries if e.current is not None]
    return round(max(vals), 1) if vals else None


def _busy_total(t):
    """(busy, total) CPU seconds from a psutil.cpu_times() reading, as psutil counts them."""
    # guest time is already included in user time on Linux; iowait counts as idle.
    total = sum(t) - getattr(t, "guest", 0) - getattr(t, "guest_nice", 0)
    idle = t.idle + getattr(t, "iowait", 0)
    return total - idle, total


class CpuMeter:
    """Whole-machine CPU % averaged since the previous read(): checks a few seconds apart
    leave no gaps for a load spike to hide in. Each read is one /proc/stat parse (~0.3 ms)."""

    def __init__(self):
        self._last = psutil.cpu_times()

    def read(self):
        now = psutil.cpu_times()
        busy0, total0 = _busy_total(self._last)
        busy1, total1 = _busy_total(now)
        self._last = now
        if total1 <= total0:
            return 0.0
        return round(min(100.0, max(0.0, (busy1 - busy0) / (total1 - total0) * 100)), 1)


def collect_metrics():
    """Sample all monitored metrics once. ``temp`` is None on machines without sensors."""
    return {
        "cpu":   psutil.cpu_percent(interval=0.5),
        "ram":   psutil.virtual_memory().percent,
        "disk":  psutil.disk_usage("/home").percent,
        "disk2": psutil.disk_usage("/data").percent,
        "temp":  cpu_temperature(),
    }
