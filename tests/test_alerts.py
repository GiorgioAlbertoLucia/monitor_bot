# test_alerts.py

import sys
sys.path.append('..')  # add parent directory to path to import monitor_system
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import users
from monitor_bot import monitor_system

# Stub the per-user samplers so the test never shells out to du/process_iter;
# the real top_*_users / mention formatting still runs on top of these.
users.cpu_by_user = lambda: {'alice': 72.0, 'bob': 15.5, 'carol': 3.1, 'root': 0.4}
users.ram_by_user = lambda: {'bob': 55.0, 'alice': 20.0, 'dave': 49.9}
users.disk_by_user = lambda paths: {p: {'carol': 80.0, 'alice': 10.0} for p in paths}

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
    bot.send_message = AsyncMock(side_effect=lambda chat_id, text, **kw: print(f'[TG] {text}'))

    # Deterministic metrics (no psutil / no /data dependency); temp included.
    metrics = {'cpu': 95, 'ram': 80, 'disk': 50, 'disk2': 90, 'temp': 88}

    print('--- all metrics over threshold (temp muted) ---')
    states = fresh_states()
    await monitor_system(config, bot, states, muted_metrics={'temp'}, metrics=metrics)

    print('\n--- temp unavailable (None) is skipped ---')
    states = fresh_states()
    await monitor_system(config, bot, states, metrics={**metrics, 'temp': None})

    # alice/bob/carol mapped to user IDs; dave unmapped;
    # 50% cutoff. Expected: alice linked on cpu+temp, bob on ram, carol on both disks;
    # bob at 15.5% CPU and dave at 49.9% RAM (unmapped anyway) not pinged.
    print('\n--- pings: mapped users at >= 50% get @-mentioned ---')
    config_pings = {
        **config,
        'user_pings': {'alice': 111111111, 'bob': 222222222, 'carol': 333333333},
        'ping_min_share': 50,
    }
    states = fresh_states()
    await monitor_system(config_pings, bot, states, metrics=metrics)

    # CPU: users at >= 50% that triggered an alert don't trigger another for an hour,
    # whatever happens in between (dips, temperature). The CPU threshold here is 1%,
    # so a "dip" is 0.5%.
    print('\n--- cpu: each user over 50% triggers at most one alert per hour ---')
    config_cpu = {**config, 'thresholds': {**config['thresholds'], 'temp': 85},
                  'cpu_same_user_cooldown_minutes': 60, 'ping_min_share': 50}
    only_cpu = {'ram', 'disk', 'disk2', 'temp'}
    states = fresh_states()

    async def step(desc, cpu, temp, expect_sent):
        before = bot.send_message.call_count
        await monitor_system(config_cpu, bot, states, muted_metrics=only_cpu,
                             metrics={**metrics, 'cpu': cpu, 'temp': temp})
        sent = bot.send_message.call_count - before
        print(f'  {desc}: {"sent" if sent else "silent"}')
        assert sent == expect_sent, f'{desc}: expected {expect_sent}, got {sent}'

    await step('alice at 72%, first alert', 95, 60, 1)
    await step('alice, CPU up 4 points', 99, 60, 0)
    # The stress-test pattern: full load heats the CPU, dips, comes back hot.
    await step('alice, CPU dips below threshold', 0.5, 84, 0)
    await step('alice, back over threshold with temp over threshold', 99.5, 93, 0)
    users.cpu_by_user = lambda: {'bob': 80.0, 'alice': 10.0}
    await step('bob at 80%, new user', 97, 60, 1)
    await step('bob again', 99, 95, 0)
    users.cpu_by_user = lambda: {'alice': 72.0, 'bob': 15.5}
    await step('alice back on top within her hour', 99, 60, 0)
    users.cpu_by_user = lambda: {'bob': 30.0, 'carol': 30.0, 'dave': 30.0}
    await step('nobody over 50%, CPU rose since last alert (usual rules)', 99.5, 60, 1)
    await step('nobody over 50%, no change', 99.5, 60, 0)
    users.cpu_by_user = lambda: {'alice': 72.0, 'bob': 15.5}
    states['cpu']['culprits']['alice'] -= 3601  # pretend alice's hour is up
    await step('alice, one hour after her alert', 99.5, 60, 1)
    print('  all cpu cases OK')

    # Disks: each disk alerts at most once per 6 h, ignoring rises and dips.
    print('\n--- disks: at most one alert per disk every 6 hours ---')
    config_disk = {**config, 'disk_alert_interval_hours': 6}
    not_disks = {'cpu', 'ram', 'temp'}
    states = fresh_states()

    async def disk_step(desc, disk, disk2, expect_sent):
        before = bot.send_message.call_count
        await monitor_system(config_disk, bot, states, muted_metrics=not_disks,
                             metrics={**metrics, 'disk': disk, 'disk2': disk2})
        sent = bot.send_message.call_count - before
        print(f'  {desc}: {sent} sent')
        assert sent == expect_sent, f'{desc}: expected {expect_sent}, got {sent}'

    await disk_step('/home over threshold, /data below', 91, 0.5, 1)
    await disk_step('/home up 8 points', 99, 0.5, 0)
    await disk_step('/home dips below and back up', 0.5, 0.5, 0)
    await disk_step('/home back over threshold', 95, 0.5, 0)
    await disk_step('/data over threshold (independent of /home)', 95, 92, 1)
    await disk_step('both still over threshold', 95, 97, 0)
    states['disk']['sent_at'] -= 6 * 3600  # pretend 6 h passed for /home only
    await disk_step('6 h after the /home alert', 95, 97, 1)
    print('  all disk cases OK')

    # RAM and temperature: an alert on every check while over threshold.
    print('\n--- ram/temp: one alert per interval while over threshold ---')
    config_rep = {**config, 'thresholds': {**config['thresholds'], 'ram': 90, 'temp': 85}}
    not_ram_temp = {'cpu', 'disk', 'disk2'}
    states = fresh_states()

    async def rep_step(desc, ram, temp, expect_sent):
        before = bot.send_message.call_count
        await monitor_system(config_rep, bot, states, muted_metrics=not_ram_temp,
                             metrics={**metrics, 'ram': ram, 'temp': temp})
        sent = bot.send_message.call_count - before
        print(f'  {desc}: {sent} sent')
        assert sent == expect_sent, f'{desc}: expected {expect_sent}, got {sent}'

    await rep_step('RAM 91%, temp 88', 91, 88, 2)
    await rep_step('same values, next interval', 91, 88, 2)
    await rep_step('RAM 91%, temp back to 60', 91, 60, 1)
    await rep_step('both below threshold', 80, 60, 0)
    not_ram_temp.add('ram')  # as after /mute ram
    await rep_step('RAM muted with /mute ram: only temp', 91, 88, 1)
    print('  all ram/temp cases OK')

    # A disk alert sent before the first du scan finished is edited once it's done.
    print('\n--- disk: alert sent mid-scan gets the top users edited in ---')
    edit_bot = MagicMock()
    edit_bot.send_message = AsyncMock(
        side_effect=lambda chat_id, text, **kw: SimpleNamespace(message_id=7, text_html=text))
    edit_bot.edit_message_text = AsyncMock()
    config_edit = {**config, 'user_pings': {'carol': 333333333}}
    only_home = {'cpu', 'ram', 'disk2', 'temp'}
    states = fresh_states()
    users.disk_by_user = lambda paths: {}  # scan still running
    await monitor_system(config_edit, edit_bot, states, muted_metrics=only_home, metrics=metrics)
    sent = edit_bot.send_message.call_args.kwargs['text']
    assert 'scan still running' in sent and 'tg://user?id=333333333' in sent
    await monitor_system(config_edit, edit_bot, states, muted_metrics=only_home, metrics=metrics)
    assert not edit_bot.edit_message_text.called, 'edited before the scan finished'
    users.disk_by_user = lambda paths: {p: {'carol': 80.0} for p in paths}  # scan done
    await monitor_system(config_edit, edit_bot, states, muted_metrics=only_home, metrics=metrics)
    edited = edit_bot.edit_message_text.call_args.kwargs
    print(f'[TG edit] {edited["text"]}')
    assert edited['message_id'] == 7 and 'scan still running' not in edited['text']
    assert 'carol</a>: 80.0%' in edited['text']
    await monitor_system(config_edit, edit_bot, states, muted_metrics=only_home, metrics=metrics)
    assert edit_bot.edit_message_text.call_count == 1, 'edited twice'
    assert edit_bot.send_message.call_count == 1, 'disk alert re-sent'
    print('  edit case OK')


asyncio.run(main())
