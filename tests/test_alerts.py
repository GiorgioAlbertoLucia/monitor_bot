# test_alerts.py

import sys
sys.path.append('..')  # add parent directory to path to import monitor_system
import asyncio
from unittest.mock import AsyncMock, MagicMock
from monitor_bot import monitor_system

async def main():
    config = {
        'thresholds': {'cpu': 1, 'ram': 1, 'disk': 1, 'disk_2': 1},  # low thresholds so they trigger easily
        'telegram': {'chat_id': '123'},
        'delta_threshold': 0.1,
    }
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=lambda chat_id, text: print(f'[TG] {text}'))

    alert_states = {k: {'last_notified_value': None} for k in ('cpu', 'ram', 'disk', 'disk2')}

    for i in range(5):
        print(f'\n--- check {i+1} ---')
        await monitor_system(config, bot, alert_states)
        await asyncio.sleep(1)

asyncio.run(main())