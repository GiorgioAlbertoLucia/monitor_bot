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

    # Every metric follows the same rules: alert when the threshold is crossed, then
    # only if the value rises by delta_threshold over the last alert; the state resets
    # once the value has stayed below threshold for alert_rearm_seconds, or after
    # alert_reset_hours.
    print('\n--- alerts: threshold crossing, delta re-alert, slow reset ---')
    config_rules = {**config, 'thresholds': {**config['thresholds'], 'ram': 90},
                    'delta_threshold': 5, 'alert_reset_hours': 3, 'alert_rearm_seconds': 60}
    only_ram = {'cpu', 'disk', 'disk2', 'temp'}
    states = fresh_states()

    async def rule_step(desc, ram, expect_sent):
        before = bot.send_message.call_count
        await monitor_system(config_rules, bot, states, muted_metrics=only_ram,
                             metrics={**metrics, 'ram': ram})
        sent = bot.send_message.call_count - before
        print(f'  {desc}: {sent} sent')
        assert sent == expect_sent, f'{desc}: expected {expect_sent}, got {sent}'

    await rule_step('RAM crosses threshold (91%)', 91, 1)
    await rule_step('same value, next interval', 91, 0)
    await rule_step('up 4 points (below delta)', 95, 0)
    await rule_step('up 5 points over last alert', 96, 1)
    await rule_step('still high, no further rise', 97, 0)
    await rule_step('dips below threshold', 80, 0)
    await rule_step('back over within the re-arm time (no alert)', 91, 0)
    await rule_step('below threshold again', 80, 0)
    states['ram']['below_since'] -= 60  # pretend it stayed below for 60 s
    await rule_step('still below after 60 s (re-armed)', 80, 0)
    await rule_step('crosses threshold again', 91, 1)
    await rule_step('stays over threshold', 91, 0)
    states['ram']['last_notified_time'] -= 3 * 3600  # pretend 3 h passed
    await rule_step('3 h after the last alert (reset)', 91, 1)
    not_ram = only_ram | {'ram'}  # as after /mute ram
    states = fresh_states()
    await monitor_system(config_rules, bot, states, muted_metrics=not_ram,
                         metrics={**metrics, 'ram': 99})
    assert states['ram']['last_notified_value'] is None, 'muted metric alerted'
    print('  all alert-rule cases OK')

    # A disk alert sent before the first du scan finished is edited once it's done.
    print('\n--- disk: alert sent mid-scan gets the top users edited in ---')
    edit_bot = MagicMock()
    edit_bot.send_message = AsyncMock(
        side_effect=lambda chat_id, text, **kw: SimpleNamespace(message_id=7, text_html=text,
                                                                reply_markup=kw.get('reply_markup')))
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
    buttons = [b.callback_data for row in edited['reply_markup'].inline_keyboard for b in row]
    assert buttons == ['procs', 'mute:disk:3600'], f'alert buttons lost in the edit: {buttons}'
    await monitor_system(config_edit, edit_bot, states, muted_metrics=only_home, metrics=metrics)
    assert edit_bot.edit_message_text.call_count == 1, 'edited twice'
    assert edit_bot.send_message.call_count == 1, 'disk alert re-sent'
    print('  edit case OK')


asyncio.run(main())
