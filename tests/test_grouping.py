"""Grouped device notifications (20 s sliding wait) and the general-failure rules."""
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

check('valores: espera 20 s, falla general 5 en 60 s, estabilización 120 s',
      (m.NOTIFY_GROUP, m.GENERAL_FAILURE, m.GENERAL_WINDOW, m.RECOVERY_SETTLE, m.RECOVERY_SETTLE_MAX) == (20, 5, 60, 120, 600))

def payload(off=(), miss=(), valid=True, expected=30):
    return {'state': 'PROBLEM' if off or miss else 'OK', 'expected': expected,
            'offline_names': sorted(off), 'missing_names': sorted(miss), 'unknown_names': [], 'data_valid': valid}

class Notifier:
    targets = ['notify.x']
    def __init__(self): self.sent = []
    def send(self, msg, extra=None): self.sent.append((clock, msg))

clock = 0
def setup(names, settle_z2m=120):
    n = Notifier()
    alerts = m.Alerts(n, set(names), path=Path(tempfile.mkdtemp()) / 'a.json', delay=120)
    tracker = m.EventTracker(write=lambda *a: None, settle_mqtt=0, settle_z2m=settle_z2m, alerts=alerts)
    tracker.mqtt_up(0)
    return n, tracker

def run(tracker, events, until):
    global clock
    state = (payload(), 'online')
    for clock in range(until):
        state = events.get(clock, state)
        tracker.observe(state[0], state[1], clock)
        tracker.tick(clock)
GF = 'Falla general: 5 dispositivos dejaron de responder en poco tiempo'

# 1. Coordinator update: one drop every 17 s (same group), 4 back, Zigbee2MQTT restarts, all back.
cascade = ['C%d' % i for i in range(6)]
n, tr = setup(cascade)
ev, down = {}, []
for i, name in enumerate(cascade):
    down.append(name)
    ev[10 + i * 17] = (payload(off=list(down)), 'online')
ev[98] = (payload(off=['C4', 'C5']), 'online')
ev[111] = (payload(valid=False), 'offline')
ev[115] = (payload(), 'online')
run(tr, ev, 400)
check('cascada lenta: falla general con el 5.º del mismo grupo, al instante', n.sent and n.sent[0][0] == 10 + 4 * 17 and n.sent[0][1].startswith(GF), n.sent[:1])
check('sin nombres en el aviso de falla general', 'C0' not in n.sent[0][1])
check('al volver todo tras el reinicio de Z2M: red estable enseguida', n.sent[1:] == [(115 + 120, 'Red estable: todo en línea')], n.sent[1:])

# 2. 25 devices lost at one per second and never back: a single message, at the 5th loss.
many = ['D%02d' % i for i in range(25)]
n, tr = setup(many)
run(tr, {10 + i: (payload(off=many[:i + 1]), 'online') for i in range(25)}, 1000)
check('25 caídos que no vuelven: un solo mensaje, con el 5.º', [s for s, _ in n.sent] == [14], n.sent)

# 3. One device: notified after 20 s with its name.
n, tr = setup(['A', 'B'])
run(tr, {5: (payload(off=['A']), 'online')}, 100)
check('un offline aislado: aviso a los 20 s con nombre', n.sent == [(25, 'Nuevo offline: A · Total: 1 offline, 0 desaparecidos')], n.sent)

# 4. Drops and comes back within the wait: nothing.
n, tr = setup(['A', 'B'])
run(tr, {5: (payload(off=['A']), 'online'), 20: (payload(), 'online')}, 200)
check('se cae y vuelve dentro de la espera: nada', n.sent == [], n.sent)

# 5. Net result: A already notified; B drops and A comes back within one wait.
n, tr = setup(['A', 'B', 'C'])
run(tr, {5: (payload(off=['A']), 'online'), 100: (payload(off=['A', 'B']), 'online'), 110: (payload(off=['B']), 'online')}, 300)
check('resultado neto en un mensaje', [msg for _, msg in n.sent] == [
    'Nuevo offline: A · Total: 1 offline, 0 desaparecidos',
    'Nuevo offline: B · Recuperado: A · Total: 1 offline, 0 desaparecidos'], n.sent)

# 6. Four at once: a normal message with names, no general failure.
n, tr = setup(list('ABCDE'))
run(tr, {5: (payload(off=list('ABC'), miss=['D']), 'online')}, 100)
check('4 a la vez: aviso normal con nombres', n.sent == [(25, 'Nuevo offline: A, B, C · Desaparecido: D · Total: 3 offline, 1 desaparecidos')], n.sent)

# 7. 5 within 60 s across two groups: normal message for the first 4, then general failure at once.
n, tr = setup(list('ABCDEF'))
run(tr, {0: (payload(off=list('ABCD')), 'online'), 45: (payload(off=list('ABCDE')), 'online')}, 200)
check('5 en menos de 60 s en dos grupos: normal y luego falla general inmediata',
      [s for s, _ in n.sent] == [20, 45] and n.sent[1][1].startswith(GF), n.sent)

# 8. Drops 25 s apart: each one separately, never a general failure.
n, tr = setup(['S%d' % i for i in range(6)])
run(tr, {10 + i * 25: (payload(off=['S%d' % k for k in range(i + 1)]), 'online') for i in range(6)}, 400)
check('caídas cada 25 s: avisos sueltos, sin falla general', len(n.sent) == 6 and not any('Falla general' in msg for _, msg in n.sent), n.sent)

# 9. MQTT lost while a message waits: it waits too and resolves against the new state.
n, tr = setup(['A', 'B'])
state = (payload(), 'online')
for clock in range(200):
    if clock == 5:
        state = (payload(off=['A']), 'online')
    if clock == 15:
        tr.mqtt_down()
    if clock == 60:
        tr.mqtt_up(60)
        state = (payload(), 'online')
    if tr.mqtt == 'up':
        tr.observe(state[0], state[1], clock)
    tr.tick(clock)
check('MQTT caído durante la espera: se resuelve con el estado final (nada)', n.sent == [], n.sent)

print('\nFAILS:', fails)
sys.exit(1 if fails else 0)
