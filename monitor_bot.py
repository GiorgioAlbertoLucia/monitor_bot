import psutil
import yaml
from telegram import Bot
import asyncio
import subprocess

# Definizione degli emoji con Unicode
SIREN_EMOJI = "\U0001F6A8"  # Emoji for general alert 🚨
FIRE_EMOJI = "\U0001F525"  # Emoji for high CPU fire 🔥
WARNING_EMOJI = "\U000026A0"  # Emoji for high RAM warning ⚠️
DISK_EMOJI = "\U0001F4BE"  # Emoji for disk usage floppy disk 💾
ANGER_EMOJI = "\U0001F92C"  # Unicode for face with symbols over mouth 🤬


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
async def monitor_system(config, bot, alert_states):
    thresholds = config["thresholds"]
    delta_threshold = config.get("delta_threshold", 5)  # % increase to re-alert
    chat_id = config["telegram"]["chat_id"]

    checks = [
        ("cpu",   psutil.cpu_percent(interval=0.5),          thresholds["cpu"],    send_cpu_notification,   "CPU",       None),
        ("ram",   psutil.virtual_memory().percent,           thresholds["ram"],    send_ram_notification,   "RAM",       None),
        ("disk",  psutil.disk_usage("/home").percent,        thresholds["disk"],   send_disk_notification,  "Disco /home", "/home"),
        ("disk2", psutil.disk_usage("/data").percent,        thresholds["disk_2"], send_disk_2_notification,"Disco /data", "/data"),
    ]

    for key, value, threshold, notify_fn, label, disk_path in checks:
        state = alert_states[key]
        if value > threshold:
            last = state.get("last_notified_value")
            if last is None or (value - last) >= delta_threshold:
                await notify_fn(bot, chat_id, f"Attenzione! Uso {label}: {value:.1f}%")
                if disk_path and last is not None and (value - last) >= delta_threshold:
                    await send_disk_usage_details(bot, chat_id, disk_path)
                state["last_notified_value"] = value
        else:
            state["last_notified_value"] = None  # Reset when back to normal
          
# Replace the message_counts dict and add dashboard thread in main()
async def main():
    config = load_config("config.yaml")
    bot = Bot(token=config["telegram"]["token"])

    alert_states = {k: {"last_notified_value": None} for k in ("cpu", "ram", "disk", "disk2")}

    # Start dashboard in background thread
    import threading
    import dashboard
    port = config.get("dashboard_port", 8080)
    threading.Thread(target=dashboard.start, kwargs={"port": port}, daemon=True).start()

    interval = config.get("interval", 60)
    while True:
        await monitor_system(config, bot, alert_states)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
