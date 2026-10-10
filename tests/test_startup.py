"""Monitor start while Zigbee2MQTT is starting too (host reboot, power cut): no false general failure."""
import os, sys, tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ['ZM_DATA'] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'zigbee_monitor'))
import monitor as m
m.LANG = 'es'
fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %s' % (detail,)))
    fails += not cond

def payload(off=(), miss=(), valid=True, expected=30):
    return {'state': 'PROBLEM' if off or miss else 'OK', 'expected': expected,
            'offline_names': sorted(off), 'missing_names': sorted(miss), 'unknown_names': [], 'data_valid': valid}

class Notifier:
    targets = ['notify.x']
    def __init__(self): self.sent = []
    def send(self, msg, extra=None): self.sent.append((clock, msg))

clock = 0
NAMES = ['D%02d' % i for i in range(30)]

def setup(problems=None):
    path = Path(tempfile.mkdtemp()) / 'a.json'
    if problems:
        path.write_text('{"problems": %s}' % problems)
    n, lines = Notifier(), []
    alerts = m.Alerts(n, set(NAMES), path=path, delay=120)
    tracker = m.EventTracker(write=lambda k, msg: lines.append((clock, k, msg)), settle_mqtt=10,
                             settle_z2m=120, alerts=alerts)
    tracker.mqtt_up(0)
    return n, lines, tracker

def run(tracker, events, until):
    global clock
    state = (payload(valid=False), None)
    for clock in range(until):
        state = events.get(clock, state)
        tracker.observe(state[0], state[1], clock)
        tracker.tick(clock)

def storm(back_every=2, stay=()):
    """27 devices down at start (as before the stop all were online); back one every 2 s."""
    down = NAMES[:27]
    ev = {1: (payload(off=down), 'online')}
    for i, name in enumerate(down):
        if name in stay:
            continue
        down = [d for d in down if d != name]
        ev[11 + i * back_every] = (payload(off=down), 'online')
    return ev

# 1. Everything comes back within a minute: no notification at all, history explains the wait.
n, lines, tr = setup()
run(tr, storm(), 400)
check('arranque con Z2M arrancando: sin falla general ni avisos', n.sent == [], n.sent)
check('historial: línea de espera al arrancar', any(k == 'SYSTEM' and '27 dispositivos' in msg for _, k, msg in lines), lines)
check('historial: estado final completo, sin 27 offline', [x[1:] for x in lines if x[1] != 'SYSTEM' and x[1] != 'MQTT'] ==
      [('OK', 'Los 30 dispositivos vigilados están en línea')], lines)

# 2. Most come back, one stays down: one normal grouped notice once the wait is over.
n, lines, tr = setup()
run(tr, storm(stay=['D05']), 400)
check('uno sigue caído tras la espera: aviso normal con su nombre',
      [msg for _, msg in n.sent] == ['Nuevo offline: D05 · Total: 1 offline, 0 desaparecidos'], n.sent)
check('...al terminar la espera de 120 s + 20 s', n.sent and n.sent[0][0] == 10 + 120 + 20, n.sent)

# 3. A real outage while the monitor was stopped: still a general failure, after the wait.
n, lines, tr = setup()
run(tr, {1: (payload(off=NAMES[:27]), 'online')}, 400)
check('falla real durante la parada: falla general tras la espera',
      len(n.sent) == 1 and n.sent[0][0] == 10 + 120 and n.sent[0][1].startswith('Falla general: 27'), n.sent)

# 4. Normal restart: devices already notified as down are not "unexpected"; no wait.
n, lines, tr = setup(problems='{%s}' % ', '.join('"%s": "offline"' % d for d in NAMES[:6]))
run(tr, {1: (payload(off=NAMES[:6]), 'online')}, 100)
check('reinicio normal con problemas ya avisados: sin espera ni avisos', n.sent == [] and
      not any(k == 'SYSTEM' for _, k, _ in lines) and any(k == 'PROBLEM' and c == 10 for c, k, _ in lines), lines)

# 5. A few new problems at start (below the general-failure threshold): judged at once, as before.
n, lines, tr = setup()
run(tr, {1: (payload(off=NAMES[:2]), 'online')}, 100)
check('pocos problemas nuevos al arrancar: aviso normal enseguida',
      [c for c, _ in n.sent] == [10 + 20], n.sent)

print('\nFALLOS:', fails)
sys.exit(1 if fails else 0)
