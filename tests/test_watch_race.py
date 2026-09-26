"""Stopping watching an offline device never looks like a recovery, whatever the timing.

Before 1.0.2 the panel saved the change and queued it in two steps; a status report taken
between them already missed the device while the tracker still expected it."""
import json, os, sys, tempfile, threading, time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ['ZM_DATA'] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'zigbee_monitor'))
import monitor as m
m.LANG = 'en'
fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %s' % detail))
    fails += not cond

A, B = '0x%016x' % 1, '0x%016x' % 2
devs = json.dumps([{'type': 'Coordinator', 'ieee_address': '0x00', 'friendly_name': 'C'},
                   {'type': 'Router', 'ieee_address': A, 'friendly_name': 'Lamp'},
                   {'type': 'Router', 'ieee_address': B, 'friendly_name': 'Leak'}]).encode()

def setup():
    store = m.DeviceStore(Path(tempfile.mkdtemp()) / 'devices.json')
    mon = m.Monitor(store)
    mon.receive('zigbee2mqtt/bridge/state', b'online')
    mon.receive('zigbee2mqtt/bridge/devices', devs)
    mon.receive('zigbee2mqtt/Lamp/availability', b'online')
    mon.receive('zigbee2mqtt/Leak/availability', b'offline')
    store.update([A, B], [], dict(mon.actual))
    sent, lines = [], []
    class Notifier:
        targets = ['notify.x']
        def send(self, msg): sent.append(msg)
    alerts = m.Alerts(Notifier(), {'Lamp', 'Leak'}, path=Path(tempfile.mkdtemp()) / 'a.json', delay=120)
    tracker = m.EventTracker(write=lambda k, msg: lines.append((k, msg)), settle_mqtt=0, alerts=alerts)
    tracker.mqtt_up(0)
    tracker.observe(mon.report(), 'online', 1)
    return store, mon, tracker, sent, lines

# 1. The panel cannot save a change while the main loop is taking its report.
store, mon, tracker, sent, lines = setup()
panel = m.PanelState()
with panel.change_lock:  # the main loop is inside sync()
    worker = threading.Thread(target=panel.change_watch_list, args=(store, [], [B], dict(mon.actual)))
    worker.start()
    time.sleep(0.2)
    check('change waits while the main loop reads', B in store.watched and panel.changes.empty())
worker.join(2)
check('change saved and queued afterwards', B not in store.watched and not panel.changes.empty())

# 2. The next sync applies the change before reporting: no false recovery.
report, changed = panel.sync(mon, tracker)
tracker.observe(report, 'online', 2)
check('sync reports the change', changed)
check('history: no "Recovered"', not any('Recovered' in msg for _, msg in lines), lines)
check('history: full line after the change', lines[-1] == ('OK', 'The only watched device is online'), lines[-2:])
check('notifications: no "Recovered"', not any('Recovered' in msg for msg in sent), sent)

# 3. Many interleavings: the panel and the main loop running at the same time.
false_recoveries = 0
for _ in range(200):
    store, mon, tracker, sent, lines = setup()
    panel = m.PanelState()
    stop = threading.Event()
    def main_loop():
        n = 2
        while not stop.is_set():
            report, _ = panel.sync(mon, tracker)
            tracker.observe(report, 'online', n)
            n += 1
    loop = threading.Thread(target=main_loop)
    loop.start()
    panel.change_watch_list(store, [], [B], dict(mon.actual))
    time.sleep(0.002)
    stop.set()
    loop.join(2)
    report, _ = panel.sync(mon, tracker)
    tracker.observe(report, 'online', 10**6)
    if any('Recovered' in msg for msg in sent):
        false_recoveries += 1
check('200 concurrent runs: never a false recovery', false_recoveries == 0, false_recoveries)

print('\nFAILS:', fails)
sys.exit(1 if fails else 0)
