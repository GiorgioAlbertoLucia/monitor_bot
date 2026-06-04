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


def collect_metrics():
    """Sample all monitored metrics once. ``temp`` is None on machines without sensors."""
    return {
        "cpu":   psutil.cpu_percent(interval=0.5),
        "ram":   psutil.virtual_memory().percent,
        "disk":  psutil.disk_usage("/home").percent,
        "disk2": psutil.disk_usage("/data").percent,
        "temp":  cpu_temperature(),
    }
