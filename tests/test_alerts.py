# test_alerts.py

import sys
sys.path.append('..')  # add parent directory to path to import monitor_system
import asyncio
from unittest.mock import AsyncMock, MagicMock
import monitor_bot
from monitor_bot import monitor_system

# Stub the per-process detail helpers so the test never shells out to du/process_iter.
monitor_bot.top_cpu_users = lambda: '  <top cpu>'
monitor_bot.top_ram_users = lambda: '  <top ram>'
monitor_bot.top_disk_users = lambda path: f'  <top disk {path}>'

KEYS = ('cpu', 'ram', 'disk', 'disk2', 'temp')


def fresh_states():
    return {k: {'last_notified_value': None, 'last_notified_time': None} for k in KEYS}


async def main():
    config = {
        # low thresholds so they trigger easily; temp threshold in °C
        'thresholds': {'cpu': 1, 'ram': 1, 'disk': 1, 'disk_2': 1, 'temp': 1},
        'telegram': {'chat_id': '123'},
        'delta_threshold': 0.1,
    }
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=lambda chat_id, text: print(f'[TG] {text}'))

    # Deterministic metrics (no psutil / no /data dependency); temp included.
    metrics = {'cpu': 95, 'ram': 80, 'disk': 50, 'disk2': 90, 'temp': 88}

    print('--- all metrics over threshold (temp muted) ---')
    states = fresh_states()
    await monitor_system(config, bot, states, muted_metrics={'temp'}, metrics=metrics)

    print('\n--- temp unavailable (None) is skipped ---')
    states = fresh_states()
    await monitor_system(config, bot, states, metrics={**metrics, 'temp': None})


asyncio.run(main())
