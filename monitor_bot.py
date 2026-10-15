from logging import config
import html

import psutil
from pyngrok import ngrok
import yaml
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
import asyncio
from telegram.ext import Application, CallbackQueryHandler, CommandHandler
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
from metrics import CpuMeter, collect_metrics


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
async def send_cpu_notification(bot, chat_id, message, reply_markup=None):
    return await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML",
                                  reply_markup=reply_markup)


@add_emoji(WARNING_EMOJI)
async def send_ram_notification(bot, chat_id, message, reply_markup=None):
    return await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML",
                                  reply_markup=reply_markup)


@add_emoji(DISK_EMOJI)
async def send_disk_notification(bot, chat_id, message, reply_markup=None):
    return await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML",
                                  reply_markup=reply_markup)


@add_emoji(DISK_EMOJI)
async def send_disk_2_notification(bot, chat_id, message, reply_markup=None):
    return await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML",
                                  reply_markup=reply_markup)


@add_emoji(THERMO_EMOJI)
async def send_temp_notification(bot, chat_id, message, reply_markup=None):
    return await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML",
                                  reply_markup=reply_markup)


def format_top(title, shares, pings=None, min_share=50):
    """Top-3 users by share, as HTML; users mapped in ``pings`` at >= ``min_share``%
    are mentioned instead of shown by their Unix name.

    ``pings`` maps Unix names to Telegram user IDs; the mention is a tg://user link
    showing the Unix name. Mentions only notify people who are members of the chat
    the alert goes to.
    """
    pings = pings or {}
    top = sorted(shares.items(), key=lambda x: x[1], reverse=True)[:3]
    lines = []
    for user, share in top:
        name = html.escape(user)
        user_id = pings.get(user)
        if user_id and share >= min_share:
            name = f'<a href="tg://user?id={user_id}">{name}</a>'
        lines.append(f"  {name}: {share:.1f}%")
    return f"{html.escape(title)}:\n" + "\n".join(lines)


def top_cpu_users(pings=None, min_share=50):
    return format_top("Top CPU users", users.cpu_by_user(), pings, min_share)


def top_ram_users(pings=None, min_share=50):
    return format_top("Top RAM users", users.ram_by_user(), pings, min_share)


def scan_pending_text(path):
    return html.escape(f"Top disk users ({path}): scan still running")


def top_disk_users(path, pings=None, min_share=50):
    disk_map = users.disk_by_user([path]).get(path)
    if disk_map is None:
        return scan_pending_text(path)
    return format_top(f"Top disk users ({path})", disk_map, pings, min_share)


METRIC_KEYS = ("cpu", "ram", "disk", "disk2", "temp")
METRIC_NAMES = {"cpu": "CPU", "ram": "RAM", "disk": "Disk /home", "disk2": "Disk /data", "temp": "CPU temperature"}
THRESHOLD_KEYS = {"cpu": "cpu", "ram": "ram", "disk": "disk", "disk2": "disk_2", "temp": "temp"}
MUTE_BUTTON_SECONDS = 3600  # "Mute 1h" on alerts


def alert_keyboard(key, public_url=None):
    """Buttons under an alert: dashboard, heaviest processes, mute this metric for 1h."""
    top = [InlineKeyboardButton("🐢 Processes", callback_data="procs")]
    if public_url:
        top.insert(0, InlineKeyboardButton("📊 Dashboard", url=public_url))
    return InlineKeyboardMarkup([
        top,
        [InlineKeyboardButton(f"🔕 Mute {METRIC_NAMES[key]} 1h",
                              callback_data=f"mute:{key}:{MUTE_BUTTON_SECONDS}")],
    ])


def format_status(metrics, thresholds, muted=()):
    """HTML snapshot of every metric against its threshold."""
    lines = []
    for key in METRIC_KEYS:
        value = metrics.get(key)
        unit = "°C" if key == "temp" else "%"
        threshold = thresholds.get(THRESHOLD_KEYS[key], 80 if key == "temp" else None)
        if value is None:
            lines.append(f"⚪ {METRIC_NAMES[key]}: n/a")
            continue
        mark = "🔴" if threshold is not None and value > threshold else "🟢"
        limit = f" / {threshold}{unit}" if threshold is not None else ""
        muted_note = " 🔕" if key in muted else ""
        lines.append(f"{mark} {METRIC_NAMES[key]}: <b>{value:.1f}{unit}</b>{limit}{muted_note}")
    return ("📈 <b>Machine status</b>\n\n" + "\n".join(lines) +
            f"\n\n<i>{time.strftime('%d/%m %H:%M:%S')}</i>")


def fmt_ram(n_bytes):
    gb = n_bytes / 1024 ** 3
    return f"{gb:.1f} GB" if gb >= 1 else f"{n_bytes / 1024 ** 2:.0f} MB"


def format_processes(by_cpu, by_ram):
    """HTML message listing the heaviest processes by CPU and by RAM."""
    def line(p, value):
        return (f"  <code>{p['pid']}</code> {html.escape(p['user'])} — "
                f"{html.escape(p['name'])}: {value}")
    cpu_lines = [line(p, f"{p['cpu']:.1f}%") for p in by_cpu]
    ram_lines = [line(p, fmt_ram(p["ram"])) for p in by_ram]
    return ("🐢 <b>Heaviest processes</b>\n\n"
            "<b>CPU</b> (share of the whole machine):\n" + "\n".join(cpu_lines) +
            "\n\n<b>RAM</b>:\n" + "\n".join(ram_lines) +
            f"\n\n<i>{time.strftime('%d/%m %H:%M:%S')}</i>")


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
async def monitor_system(config, bot, alert_states, muted_metrics=None, metrics=None, public_url=None,
                         keys=None):
    """Alert on the metrics in ``keys`` (default: all of them)."""
    thresholds = config["thresholds"]
    delta_threshold = config.get("delta_threshold", 5)  # increase needed to re-alert
    chat_id = config["telegram"]["chat_id"]
    muted_metrics = muted_metrics or set()
    if metrics is None:
        metrics = await asyncio.to_thread(collect_metrics)

    # Unix user -> Telegram user ID, pinged when their share is >= ping_min_share %.
    ping = {
        "pings": {str(u): str(h) for u, h in (config.get("user_pings") or {}).items()},
        "min_share": config.get("ping_min_share", 50),
    }
    disk_paths = {"disk": "/home", "disk2": "/data"}
    ping_all_keys = set(config.get("ping_everyone", ["disk", "disk2"]))

    # key, value, threshold, notify_fn, label, unit, detail_fn (None = no detail)
    # Temperature has no per-user share, so its alert blames the top CPU users.
    checks = [
        ("cpu",   metrics.get("cpu"),      thresholds["cpu"],         send_cpu_notification,    "Uso CPU",         "%",  lambda: top_cpu_users(**ping)),
        ("ram",   metrics.get("ram"),      thresholds["ram"],         send_ram_notification,    "Uso RAM",         "%",  lambda: top_ram_users(**ping)),
        ("disk",  metrics.get("disk"),     thresholds["disk"],        send_disk_notification,   "Uso Disco /home", "%",  lambda: top_disk_users(disk_paths["disk"], **ping)),
        ("disk2", metrics.get("disk2"),    thresholds["disk_2"],      send_disk_2_notification, "Uso Disco /data", "%",  lambda: top_disk_users(disk_paths["disk2"], **ping)),
        ("temp",  metrics.get("temp"), thresholds.get("temp", 80), send_temp_notification,  "Temperatura CPU", "°C", lambda: top_cpu_users(**ping)),
    ]

    for key, value, threshold, notify_fn, label, unit, detail_fn in checks:
        if keys is not None and key not in keys:
            continue
        state = alert_states[key]
        pending = state.get("pending_edit")
        if pending is not None:
            # A disk alert went out before its first du scan finished: once the scan
            # is done, edit the top-users list into it (edits don't re-notify).
            path = disk_paths[key]
            if users.disk_by_user([path]).get(path) is not None:
                state["pending_edit"] = None
                detail = await asyncio.to_thread(top_disk_users, path, **ping)
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=pending.message_id, parse_mode="HTML",
                        text=pending.text_html.replace(scan_pending_text(path), detail),
                        reply_markup=getattr(pending, "reply_markup", None),
                    )
                except Exception as e:
                    print(f"Could not update the {key} alert with the scan: {e}")

        if value is None or key in muted_metrics:
            continue
        now = asyncio.get_event_loop().time()
        reset_after = config.get("alert_reset_hours", 3) * 3600
        rearm_after = config.get("alert_rearm_seconds", 60)

        # Reset once the value has stayed below threshold for rearm_after seconds (a
        # value hovering around the threshold doesn't re-alert on every crossing),
        # or when the timeout has elapsed.
        if value > threshold:
            state["below_since"] = None
        elif state.get("below_since") is None:
            state["below_since"] = now
        if state["last_notified_value"] is not None:
            timed_out = (
                state.get("last_notified_time") is not None
                and (now - state["last_notified_time"]) >= reset_after
            )
            recovered = value <= threshold and now - state["below_since"] >= rearm_after
            if recovered or timed_out:
                state["last_notified_value"] = None
                state["last_notified_time"] = None

        if value <= threshold:
            continue
        # Alert when the threshold is first crossed, then again only if the value
        # rises by delta_threshold over the last alert (or after the slow reset).
        last = state["last_notified_value"]
        if last is None or (value - last) >= delta_threshold:
            detail = None
            if detail_fn:
                # Details sample processes / read disk shares: keep them off the event loop.
                detail = await asyncio.to_thread(detail_fn)
            message = html.escape(f"Attenzione! {label}: {value:.1f}{unit}")
            if detail:
                message += f"\n\n{detail}"
            if key in ping_all_keys and ping["pings"]:
                # Shared resources: everyone mapped is told, whatever their share.
                everyone = ", ".join(
                    f'<a href="tg://user?id={uid}">{html.escape(u)}</a>'
                    for u, uid in ping["pings"].items()
                )
                message += f"\n\n{everyone}"
            sent = await notify_fn(bot, chat_id, message, reply_markup=alert_keyboard(key, public_url))
            if key in disk_paths and detail == scan_pending_text(disk_paths[key]):
                state["pending_edit"] = sent
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
    muted_until = {}  # metric -> time.time() when a timed mute ends (mute button)
    VALID_KEYS = set(METRIC_KEYS) | {"all"}

    def muted_text():
        if not muted_metrics:
            return "none"
        parts = []
        for k in sorted(muted_metrics):
            until = muted_until.get(k)
            parts.append(f"{k} (until {time.strftime('%H:%M', time.localtime(until))})" if until else k)
        return ", ".join(parts)

    async def mute_cmd(update, context):
        args = context.args
        if not args or args[0].lower() not in VALID_KEYS:
            await update.message.reply_text(
                "Usage: /mute <cpu|ram|disk|disk2|temp|all>\nCurrently muted: " + muted_text()
            )
            return
        target = args[0].lower()
        targets = METRIC_KEYS if target == "all" else (target,)
        muted_metrics.update(targets)
        for k in targets:
            muted_until.pop(k, None)  # /mute lasts until /unmute
        await update.message.reply_text(f"🔕 Muted: {muted_text()}")

    async def unmute_cmd(update, context):
        args = context.args
        if not args or args[0].lower() not in VALID_KEYS:
            await update.message.reply_text(
                "Usage: /unmute <cpu|ram|disk|disk2|temp|all>\nCurrently muted: " + muted_text()
            )
            return
        target = args[0].lower()
        targets = METRIC_KEYS if target == "all" else (target,)
        for k in targets:
            muted_metrics.discard(k)
            muted_until.pop(k, None)
        await update.message.reply_text(f"🔔 Unmuted: {target}. Currently muted: {muted_text()}")

    async def help_cmd(update, context):
        text = (
            "📋 *Available commands*\n\n"
            "/monitor — dashboard link and quick buttons\n"
            "/status — current CPU, RAM, disks and temperature\n"
            "/processes — heaviest processes by CPU and RAM\n"
            "/mute <cpu|ram|disk|disk2|temp|all> — disable notifications until /unmute\n"
            "/unmute <cpu|ram|disk|disk2|temp|all> — re-enable notifications\n"
            "/help — show this message\n\n"
            "Alerts also have a button to mute that metric for 1 hour."
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
    retention_seconds = db_cfg.get("retention_hours", 336) * 3600
    database.init_db(db_path)
    users.start_disk_refresher()  # du runs in the background; nobody waits for it
    dashboard.app.config["DB_PATH"] = db_path
    dashboard.app.config["THRESHOLDS"] = config["thresholds"]
    dashboard.app.config["AUTH"] = config.get("dashboard_auth")  # "user:pass" or None
    dashboard.app.config["DEFAULT_USERS"] = [str(u) for u in config.get("dashboard_default_users") or []]

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

    def refresh_button(data):
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data=data)]])

    async def reply_or_edit(query, text, data):
        """Answer a button: "*_refresh" buttons update their message, others reply."""
        markup = refresh_button(data.removesuffix("_refresh") + "_refresh")
        if data.endswith("_refresh"):
            try:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            except BadRequest as e:
                if "not modified" not in str(e):
                    raise
        else:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def processes_text():
        # Sampling sleeps and walks every process: keep it off the event loop.
        return format_processes(*await asyncio.to_thread(users.top_processes))

    async def status_text():
        metrics = await asyncio.to_thread(collect_metrics)
        return format_status(metrics, config["thresholds"], muted_metrics)

    async def monitor_cmd(update, context):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Open dashboard", url=public_url)],
            [InlineKeyboardButton("📈 Status", callback_data="status"),
             InlineKeyboardButton("🐢 Heaviest processes", callback_data="procs")],
        ])
        await update.message.reply_text(f"📊 Dashboard: {public_url}", reply_markup=keyboard)

    async def processes_cmd(update, context):
        await update.message.reply_text(await processes_text(), parse_mode="HTML",
                                        reply_markup=refresh_button("procs_refresh"))

    async def status_cmd(update, context):
        await update.message.reply_text(await status_text(), parse_mode="HTML",
                                        reply_markup=refresh_button("status_refresh"))

    async def procs_button(update, context):
        query = update.callback_query
        await query.answer("Sampling processes…")
        await reply_or_edit(query, await processes_text(), query.data)

    async def status_button(update, context):
        query = update.callback_query
        await query.answer()
        await reply_or_edit(query, await status_text(), query.data)

    async def mute_button(update, context):
        query = update.callback_query
        _, key, seconds = query.data.split(":")
        if key not in METRIC_KEYS:
            await query.answer()
            return
        if key in muted_metrics and key not in muted_until:
            await query.answer(f"{METRIC_NAMES[key]} is already muted until /unmute")
            return
        muted_metrics.add(key)
        muted_until[key] = time.time() + int(seconds)
        until = time.strftime("%H:%M", time.localtime(muted_until[key]))
        await query.answer(f"Muted until {until}")
        who = html.escape(query.from_user.first_name) if query.from_user else "someone"
        await query.message.reply_text(
            f"🔕 {METRIC_NAMES[key]} alerts muted until {until} by {who}. /unmute {key} to undo.",
            parse_mode="HTML",
        )

    app.add_handler(CommandHandler("monitor", monitor_cmd))
    app.add_handler(CommandHandler("start", monitor_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("processes", processes_cmd))
    app.add_handler(CallbackQueryHandler(procs_button, pattern="^procs"))
    app.add_handler(CallbackQueryHandler(status_button, pattern="^status"))
    app.add_handler(CallbackQueryHandler(mute_button, pattern="^mute:"))
    app.add_handler(CommandHandler("mute",   mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    async def end_timed_mutes():
        """Lift mutes set with the alert button once their hour is over, and say so."""
        now = time.time()
        for key in [k for k, t in muted_until.items() if now >= t]:
            del muted_until[key]
            muted_metrics.discard(key)
            try:
                await app.bot.send_message(chat_id=config["telegram"]["chat_id"],
                                           text=f"🔔 {METRIC_NAMES[key]} alerts are back on.")
            except Exception as e:
                print(f"Could not announce the end of the {key} mute: {e}")

    # Run polling and monitoring loop concurrently
    interval = config.get("interval", 60)
    # CPU is checked more often than the rest: a check is one /proc/stat read, averaged
    # since the previous one, so even a short burst over threshold is seen.
    cpu_interval = config.get("cpu_check_interval", 1)
    cpu_meter = CpuMeter()
    cpu_peak = None  # highest CPU check since the last DB sample, for the dashboard

    async def watch_cpu():
        nonlocal cpu_peak
        while True:
            await asyncio.sleep(cpu_interval)
            value = cpu_meter.read()
            cpu_peak = value if cpu_peak is None else max(cpu_peak, value)
            try:
                await monitor_system(config, app.bot, alert_states, muted_metrics,
                                     {"cpu": value}, public_url, keys=("cpu",))
            except Exception as e:
                print(f"CPU check failed: {e}")

    async with app:
        await app.start()
        # Commands shown in Telegram's "/" menu.
        await app.bot.set_my_commands([
            BotCommand("monitor", "Dashboard link and quick buttons"),
            BotCommand("status", "Current CPU, RAM, disks and temperature"),
            BotCommand("processes", "Heaviest processes by CPU and RAM"),
            BotCommand("mute", "Mute alerts: cpu, ram, disk, disk2, temp or all"),
            BotCommand("unmute", "Re-enable alerts: cpu, ram, disk, disk2, temp or all"),
            BotCommand("help", "List the commands"),
        ])
        await app.updater.start_polling()
        cpu_task = asyncio.create_task(watch_cpu())  # kept referenced so it isn't collected
        while True:
            # Sampling and DB writes block (sleeps, process scans, SQLite), so they run
            # in a worker thread; the event loop stays free to answer Telegram commands.
            ts = int(time.time())
            metrics = await asyncio.to_thread(collect_metrics)
            if cpu_peak is not None:
                # Store the peak of the CPU checks since the last sample, not a 0.5 s snapshot.
                metrics["cpu"], cpu_peak = cpu_peak, None
            try:
                await asyncio.to_thread(store_sample, db_path, ts, metrics, retention_seconds)
            except Exception as e:
                print(f"DB write failed: {e}")
            await end_timed_mutes()
            await monitor_system(config, app.bot, alert_states, muted_metrics, metrics, public_url,
                                 keys=("ram", "disk", "disk2", "temp"))
            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
