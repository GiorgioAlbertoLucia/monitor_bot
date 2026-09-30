from logging import config

import psutil
from pyngrok import ngrok
import yaml
from telegram import Bot
import asyncio
from telegram.ext import Application, CommandHandler
import os

# Definizione degli emoji con Unicode
SIREN_EMOJI = "\U0001F6A8"  # Emoji for general alert 🚨
FIRE_EMOJI = "\U0001F525"  # Emoji for high CPU fire 🔥
WARNING_EMOJI = "\U000026A0"  # Emoji for high RAM warning ⚠️
DISK_EMOJI = "\U0001F4BE"  # Emoji for disk usage floppy disk 💾
THERMO_EMOJI = "\U0001F321"  # Emoji for temperature thermometer 🌡

import queue
import time

import database
import users
from metrics import collect_metrics


# Funzione per caricare il file YAML
def load_config(file_path="config.yaml"):
    with open(file_path, "r") as file:
        return yaml.safe_load(file)


# Decoratore per aggiungere emoji all'inizio e alla fine del messaggio
def add_emoji(emj):
    def decorator(func):
        async def wrapper(bot, chat_id, message, *args, **kwargs):
            # Emojis wrap the first line only, so they don't trail the top-users list.
            first, sep, rest = message.partition("\n")
            decorated_message = f"{emj}{emj} {first} {emj}{emj}{sep}{rest}"
            return await func(bot, chat_id, decorated_message, *args, **kwargs)

        return wrapper

    return decorator


# Funzione per inviare notifiche su Telegram con decoratore parametrizzato
@add_emoji(FIRE_EMOJI)
async def send_cpu_notification(bot, chat_id, message):
    await bot.send_message(chat_id=chat_id, text=message)


@add_emoji(WARNING_EMOJI)
async def send_ram_notification(bot, chat_id, message):
    await bot.send_message(chat_id=chat_id, text=message)


@add_emoji(DISK_EMOJI)
async def send_disk_notification(bot, chat_id, message):
    await bot.send_message(chat_id=chat_id, text=message)


@add_emoji(DISK_EMOJI)
async def send_disk_2_notification(bot, chat_id, message):
    await bot.send_message(chat_id=chat_id, text=message)


@add_emoji(THERMO_EMOJI)
async def send_temp_notification(bot, chat_id, message):
    await bot.send_message(chat_id=chat_id, text=message)


def format_top(title, shares, pings=None, min_share=50):
    """Top-3 users by share; users mapped in ``pings`` at >= ``min_share``% are shown
    as their @-mention instead of their Unix name.

    Mentions only notify people who are members of the chat the alert goes to.
    """
    pings = pings or {}
    top = sorted(shares.items(), key=lambda x: x[1], reverse=True)[:3]
    lines = []
    for user, share in top:
        name = user
        handle = pings.get(user)
        if handle and share >= min_share:
            name = handle if handle.startswith("@") else "@" + handle
        lines.append(f"  {name}: {share:.1f}%")
    return f"{title}:\n" + "\n".join(lines)


def top_cpu_users(pings=None, min_share=50):
    return format_top("Top CPU users", users.cpu_by_user(), pings, min_share)


def top_ram_users(pings=None, min_share=50):
    return format_top("Top RAM users", users.ram_by_user(), pings, min_share)


def top_disk_users(path, pings=None, min_share=50):
    disk_map = users.disk_by_user([path]).get(path)
    if disk_map is None:
        return f"Top disk users ({path}): scan still running"
    return format_top(f"Top disk users ({path})", disk_map, pings, min_share)


# Funzione per inviare i dettagli dell'uso del disco
async def send_disk_usage_details(bot, chat_id, disk_path):
    # Esegue il comando e cattura l'output
    try:
        result = subprocess.check_output(
            ["sudo", "du", "-h", "--max-depth=1", disk_path], text=True
        )
    except subprocess.CalledProcessError as e:
        result = f"Errore durante l'esecuzione del comando: {e}"

    disk_usage_message = f"Dettagli utilizzo disco su {disk_path}:\n\n{result}"
    await bot.send_message(chat_id=chat_id, text=disk_usage_message)

def store_sample(db_path, ts, metrics, retention_seconds):
    """Write one aggregate + per-user sample and drop rows past the retention window."""
    database.insert(db_path, metrics, ts=ts)
    database.insert_users(db_path, ts, users.breakdown())
    database.prune(db_path, retention_seconds)


# Replace the existing monitor_system function
async def monitor_system(config, bot, alert_states, muted_metrics=None, metrics=None):
    thresholds = config["thresholds"]
    delta_threshold = config.get("delta_threshold", 5)  # increase needed to re-alert
    chat_id = config["telegram"]["chat_id"]
    muted_metrics = muted_metrics or set()
    if metrics is None:
        metrics = await asyncio.to_thread(collect_metrics)

    # Unix user -> Telegram @username, pinged when their share is >= ping_min_share %.
    ping = {
        "pings": {str(u): str(h) for u, h in (config.get("user_pings") or {}).items()},
        "min_share": config.get("ping_min_share", 50),
    }
    cpu_cooldown = config.get("cpu_same_user_cooldown_minutes", 60) * 60
    disk_interval = config.get("disk_alert_interval_hours", 6) * 3600

    # key, value, threshold, notify_fn, label, unit, detail_fn (None = no detail)
    # Temperature has no per-user share, so its alert blames the top CPU users.
    checks = [
        ("cpu",   metrics["cpu"],      thresholds["cpu"],         send_cpu_notification,    "Uso CPU",         "%",  lambda: top_cpu_users(**ping)),
        ("ram",   metrics["ram"],      thresholds["ram"],         send_ram_notification,    "Uso RAM",         "%",  lambda: top_ram_users(**ping)),
        ("disk",  metrics["disk"],     thresholds["disk"],        send_disk_notification,   "Uso Disco /home", "%",  lambda: top_disk_users("/home", **ping)),
        ("disk2", metrics["disk2"],    thresholds["disk_2"],      send_disk_2_notification, "Uso Disco /data", "%",  lambda: top_disk_users("/data", **ping)),
        ("temp",  metrics.get("temp"), thresholds.get("temp", 80), send_temp_notification,  "Temperatura CPU", "°C", lambda: top_cpu_users(**ping)),
    ]

    for key, value, threshold, notify_fn, label, unit, detail_fn in checks:
        if value is None or key in muted_metrics:
            continue
        state = alert_states[key]
        now = asyncio.get_event_loop().time()
        reset_after = config.get("alert_reset_hours", 3) * 3600

        # Reset if back below threshold or timeout elapsed
        if state["last_notified_value"] is not None:
            timed_out = (
                state.get("last_notified_time") is not None
                and (now - state["last_notified_time"]) >= reset_after
            )
            if value <= threshold or timed_out:
                state["last_notified_value"] = None
                state["last_notified_time"] = None

        if value <= threshold:
            continue
        last = state["last_notified_value"]
        should_notify = last is None or (value - last) >= delta_threshold
        detail = None
        offenders = set()

        if key == "cpu":
            # Sample per-user CPU once: it picks the offenders and fills the detail.
            shares = await asyncio.to_thread(users.cpu_by_user)
            detail = format_top("Top CPU users", shares, **ping)
            # Users at >= ping_min_share are held responsible, and each triggers at
            # most one CPU alert per cooldown, whatever happens in between (dips
            # below threshold, temperature). With nobody over the share, the
            # usual threshold/delta rules above apply.
            offenders = {u for u, s in shares.items() if s >= ping["min_share"]}
            notified = state.setdefault("culprits", {})  # user -> time of their alert
            for user in [u for u, t in notified.items() if now - t >= cpu_cooldown]:
                del notified[user]
            if offenders:
                should_notify = bool(offenders - notified.keys())

        if key in ("disk", "disk2"):
            # Disks fill slowly: each one alerts at most once per disk_interval,
            # ignoring rises and dips below threshold in between.
            sent_at = state.get("sent_at")
            should_notify = sent_at is None or (now - sent_at) >= disk_interval

        if should_notify:
            if detail is None and detail_fn:
                # Details sample processes / read disk shares: keep them off the event loop.
                detail = await asyncio.to_thread(detail_fn)
            message = f"Attenzione! {label}: {value:.1f}{unit}"
            if detail:
                message += f"\n\n{detail}"
            await notify_fn(bot, chat_id, message)
            state["last_notified_value"] = value
            state["last_notified_time"] = now
            state["sent_at"] = now  # unlike last_notified_*, never cleared by a reset
            for user in offenders:
                notified.setdefault(user, now)  # already-known users keep their hour

# Replace the main() function
async def main():
    config = load_config("configs/config.yaml")
    
    alert_states = {
        k: {"last_notified_value": None, "last_notified_time": None}
        for k in ("cpu", "ram", "disk", "disk2", "temp")
    }

    muted_metrics = set()
    VALID_KEYS = {"cpu", "ram", "disk", "disk2", "temp", "all"}

    async def mute_cmd(update, context):
        args = context.args
        if not args or args[0].lower() not in VALID_KEYS:
            await update.message.reply_text(
                "Usage: /mute <cpu|ram|disk|disk2|all>\nCurrently muted: "
                + (", ".join(sorted(muted_metrics)) or "none")
            )
            return
        target = args[0].lower()
        if target == "all":
            muted_metrics.update({"cpu", "ram", "disk", "disk2", "temp"})
        else:
            muted_metrics.add(target)
        await update.message.reply_text(
            f"🔕 Muted: {', '.join(sorted(muted_metrics))}"
        )

    async def unmute_cmd(update, context):
        args = context.args
        if not args or args[0].lower() not in VALID_KEYS:
            await update.message.reply_text(
                "Usage: /unmute <cpu|ram|disk|disk2|all>\nCurrently muted: "
                + (", ".join(sorted(muted_metrics)) or "none")
            )
            return
        target = args[0].lower()
        if target == "all":
            muted_metrics.clear()
        else:
            muted_metrics.discard(target)
        await update.message.reply_text(
            f"🔔 Unmuted: {target}. Currently muted: {', '.join(sorted(muted_metrics)) or 'none'}"
        )
    
    async def help_cmd(update, context):
        text = (
            "📋 *Available commands*\n\n"
            "/monitor — get the dashboard URL\n"
            "/mute <cpu|ram|disk|disk2|temp|all> — disable notifications\n"
            "/unmute <cpu|ram|disk|disk2|temp|all> — re-enable notifications\n"
            "/help — show this message"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    from pyngrok.conf import PyngrokConfig
    ngrok.set_auth_token(config["ngrok_token"])

    # Start dashboard in background thread
    import threading
    import dashboard
    port = config.get("dashboard_port", 8080)

    # Persistent metric history (SQLite) + dashboard wiring
    db_cfg = config.get("database", {})
    db_path = db_cfg.get("path", "data/metrics.db")
    retention_seconds = db_cfg.get("retention_hours", 168) * 3600
    database.init_db(db_path)
    users.start_disk_refresher()  # du runs in the background; nobody waits for it
    dashboard.app.config["DB_PATH"] = db_path
    dashboard.app.config["THRESHOLDS"] = config["thresholds"]
    dashboard.app.config["AUTH"] = config.get("dashboard_auth")  # "user:pass" or None

    url_queue = queue.Queue()

    def run_dashboard():
        from pyngrok import ngrok
        tunnel = ngrok.connect(port)
        url_queue.put(tunnel.public_url)
        dashboard.app.run(host="0.0.0.0", port=port, use_reloader=False)

    threading.Thread(target=run_dashboard, daemon=True).start()
    public_url = url_queue.get(timeout=10)
    print(f"Dashboard available at: {public_url}")

    # Build application with command handler
    app = Application.builder().token(config["telegram"]["token"]).build()

    async def monitor_cmd(update, context):
        await update.message.reply_text(f"📊 Dashboard: {public_url}")

    app.add_handler(CommandHandler("monitor", monitor_cmd))
    app.add_handler(CommandHandler("mute",   mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # Run polling and monitoring loop concurrently
    interval = config.get("interval", 60)
    async with app:
        await app.start()
        await app.updater.start_polling()
        while True:
            # Sampling and DB writes block (sleeps, process scans, SQLite), so they run
            # in a worker thread; the event loop stays free to answer Telegram commands.
            ts = int(time.time())
            metrics = await asyncio.to_thread(collect_metrics)
            try:
                await asyncio.to_thread(store_sample, db_path, ts, metrics, retention_seconds)
            except Exception as e:
                print(f"DB write failed: {e}")
            await monitor_system(config, app.bot, alert_states, muted_metrics, metrics)
            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
