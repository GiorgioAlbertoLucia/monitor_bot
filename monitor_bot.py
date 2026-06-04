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
ANGER_EMOJI = "\U0001F92C"  # Unicode for face with symbols over mouth 🤬

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
            decorated_message = f"{emj}{emj} {message} {emj}{emj}"
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


def top_cpu_users():
    top = sorted(users.cpu_by_user().items(), key=lambda x: x[1], reverse=True)[:3]
    lines = [f"  {u}: {v:.1f}%" for u, v in top]
    return "Top CPU users:\n" + "\n".join(lines)


def top_ram_users():
    top = sorted(users.ram_by_user().items(), key=lambda x: x[1], reverse=True)[:3]
    lines = [f"  {u}: {v:.1f}%" for u, v in top]
    return "Top RAM users:\n" + "\n".join(lines)


def top_disk_users(path):
    disk_map = users.disk_by_user([path]).get(path, {})
    top = sorted(disk_map.items(), key=lambda x: x[1], reverse=True)[:3]
    lines = [f"  {u}: {v:.1f}%" for u, v in top]
    return f"Top disk users ({path}):\n" + "\n".join(lines)


# Funzione per inviare il messaggio speciale
async def send_special_message(bot, chat_id):
    special_message = f"{ANGER_EMOJI}{ANGER_EMOJI} Il responsabile agisca, prima che gli tagli le mani {ANGER_EMOJI}{ANGER_EMOJI}"
    await bot.send_message(chat_id=chat_id, text=special_message)


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

# Replace the existing monitor_system function
async def monitor_system(config, bot, alert_states, muted_metrics=None, metrics=None):
    thresholds = config["thresholds"]
    delta_threshold = config.get("delta_threshold", 5)  # increase needed to re-alert
    chat_id = config["telegram"]["chat_id"]
    muted_metrics = muted_metrics or set()
    if metrics is None:
        metrics = collect_metrics()

    # key, value, threshold, notify_fn, label, unit, detail_fn (None = no detail)
    checks = [
        ("cpu",   metrics["cpu"],      thresholds["cpu"],         send_cpu_notification,    "Uso CPU",         "%",  top_cpu_users),
        ("ram",   metrics["ram"],      thresholds["ram"],         send_ram_notification,    "Uso RAM",         "%",  top_ram_users),
        ("disk",  metrics["disk"],     thresholds["disk"],        send_disk_notification,   "Uso Disco /home", "%",  lambda: top_disk_users("/home")),
        ("disk2", metrics["disk2"],    thresholds["disk_2"],      send_disk_2_notification, "Uso Disco /data", "%",  lambda: top_disk_users("/data")),
        ("temp",  metrics.get("temp"), thresholds.get("temp", 80), send_temp_notification,  "Temperatura CPU", "°C", None),
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

        if value > threshold:
            last = state["last_notified_value"]
            if last is None or (value - last) >= delta_threshold:
                detail = detail_fn() if detail_fn else None
                message = f"Attenzione! {label}: {value:.1f}{unit}"
                if detail:
                    message += f"\n\n{detail}"
                await notify_fn(bot, chat_id, message)
                state["last_notified_value"] = value
                state["last_notified_time"] = now

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
            ts = int(time.time())
            metrics = collect_metrics()
            try:
                database.insert(db_path, metrics, ts=ts)
                database.insert_users(db_path, ts, users.breakdown())
                database.prune(db_path, retention_seconds)
            except Exception as e:
                print(f"DB write failed: {e}")
            await monitor_system(config, app.bot, alert_states, muted_metrics, metrics)
            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
