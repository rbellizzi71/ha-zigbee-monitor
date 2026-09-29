"""1.3.0: Settings tab. Which notifications are sent (devices, general failure, system), changed live
from the panel or Home Assistant switches; history, panel and entities never change."""
import json, os, signal, sys, tempfile, threading, time, urllib.request, urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp()
os.environ.update(ZM_DATA=TMP, ZM_PANEL_PORT='18899', ZM_NOTIFY_GROUP='1')
sys.path.insert(0, str(HERE / 'fakepaho'))
sys.path.insert(0, str(HERE.parent / 'zigbee_monitor'))
import paho.mqtt.client as fake
import monitor as m
m.LANG = 'es'
fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %s' % (detail,)))
    fails += not cond

JOURNAL = []
m.journal_write = lambda kind, message: JOURNAL.append((kind, message))

# ---------- Settings: defaults, persistence, history lines ----------
path = Path(TMP) / 's.json'
s = m.Settings(path)
check('por defecto: todo activado', s.snapshot() == {'devices': True, 'general': True, 'system': True}, s.snapshot())
check('describe: todas', s.describe() == 'Notificaciones: todas', s.describe())
check('cambio devuelve True y sube la versión', s.set_notify('devices', False, 'panel') is True and s.version == 1)
check('sin cambio real: False, sin línea', s.set_notify('devices', False, 'panel') is False and s.version == 1 and len(JOURNAL) == 1, JOURNAL)
check('línea de historial con el origen', JOURNAL[-1] == ('SYSTEM', 'Notificaciones de dispositivos: desactivadas (panel)'), JOURNAL)
s.set_notify('general', False, 'ha')
check('línea desde Home Assistant', JOURNAL[-1] == ('SYSTEM', 'Notificaciones de fallo general: desactivadas (Home Assistant)'), JOURNAL)
check('describe: algunas', s.describe() == 'Notificaciones: sistema (dispositivos, fallo general: desactivadas)', s.describe())
check('persistencia', m.Settings(path).snapshot() == {'devices': False, 'general': False, 'system': True})
s.set_notify('system', False, 'panel')
check('describe: ninguna', s.describe() == 'Notificaciones: todas desactivadas', s.describe())
for bad in (('otra', True), ('devices', 'yes')):
    try:
        s.set_notify(bad[0], bad[1], 'panel'); check('rechaza %r' % (bad,), False)
    except ValueError:
        check('rechaza %r' % (bad,), True)
path.write_text('{roto')
check('archivo ilegible: todo activado', m.Settings(path).snapshot() == {'devices': True, 'general': True, 'system': True})

# ---------- Alerts with settings ----------
def payload(off=(), miss=()):
    return {'state': 'PROBLEM' if off or miss else 'OK', 'expected': 30, 'offline_names': sorted(off),
            'missing_names': sorted(miss), 'unknown_names': [], 'data_valid': True}

class Notifier:
    targets = ['notify.x']
    def __init__(self): self.sent = []
    def send(self, msg, extra=None): self.sent.append(msg)

def setup(names, **notify):
    settings = m.Settings(Path(tempfile.mkdtemp()) / 's.json')
    for kind, value in notify.items():
        settings.set_notify(kind, value, 'panel')
    n = Notifier()
    alerts = m.Alerts(n, set(names), path=Path(tempfile.mkdtemp()) / 'a.json', delay=120, group=20, settings=settings)
    tracker = m.EventTracker(write=lambda *a: None, settle_mqtt=0, settle_z2m=120, alerts=alerts)
    tracker.mqtt_up(0)
    return n, tracker, alerts, settings

def run(tracker, events, start, until, bridge=None):
    state = None
    for clock in range(start, until):
        state = events.get(clock, state)
        if state is not None:
            tracker.observe(state[0], state[1], clock)
        tracker.tick(clock)

# devices off: nothing sent, memory updated; back on: no stale news
n, tr, al, st = setup(['A', 'B'], devices=False)
run(tr, {0: (payload(), 'online'), 5: (payload(off=['A']), 'online')}, 0, 100)
check('dispositivos desactivados: no se envía', n.sent == [], n.sent)
check('...pero la memoria se actualiza', al.problems == {'A': 'offline'}, al.problems)
st.set_notify('devices', True, 'panel')
run(tr, {100: (payload(off=['A']), 'online')}, 100, 200)
check('al reactivar: sin avisos atrasados', n.sent == [], n.sent)
run(tr, {200: (payload(), 'online')}, 200, 300)
check('después: los cambios nuevos se notifican', n.sent == ['Recuperado: A · Todos los dispositivos en línea'], n.sent)

# general off: a mass failure becomes a grouped message with names
names6 = ['D%d' % i for i in range(6)]
n, tr, al, st = setup(names6, general=False)
run(tr, {0: (payload(), 'online'), 5: (payload(off=names6), 'online')}, 0, 100)
check('fallo general desactivado: no se abre', al.general is None)
check('...los perdidos llegan agrupados con nombres', len(n.sent) == 1 and n.sent[0].startswith('Nuevo offline: D0, D1, D2, D3, D4, D5'), n.sent)

# general on, devices off: the general failure is still announced
n, tr, al, st = setup(names6, devices=False)
run(tr, {0: (payload(), 'online'), 5: (payload(off=names6), 'online')}, 0, 50)
check('dispositivos desactivados, fallo general activado: aviso de fallo general', len(n.sent) == 1 and n.sent[0].startswith('Falla general'), n.sent)

# general turned off while a failure is open: it ends silently
n, tr, al, st = setup(names6)
run(tr, {0: (payload(), 'online'), 5: (payload(off=names6), 'online')}, 0, 50)
st.set_notify('general', False, 'panel')
run(tr, {50: (payload(), 'online')}, 50, 400)
check('apagado en medio de un fallo: termina sin "Red estable"', len(n.sent) == 1 and al.general is None, n.sent)

# system off: Zigbee2MQTT offline is not notified, the memory still follows it
n, tr, al, st = setup(['A'], system=False)
tr.observe(payload(), 'online', 0)
for clock in range(1, 300):
    tr.observe(payload(), 'offline', clock); tr.tick(clock)
check('sistema desactivado: Z2M caído no se notifica', n.sent == [] and al.z2m_alerted is True, (n.sent, al.z2m_alerted))
st.set_notify('system', True, 'panel')
for clock in range(300, 310):
    tr.observe(payload(), 'online', clock); tr.tick(clock)
check('...al volver, con sistema activado: aviso de vuelta', n.sent == [m.t('alert_z2m_back')], n.sent)
n, tr, al, st = setup(['A'], system=False)
al.setup(True)
check('sistema desactivado: recordatorio sin dispositivos no se envía', n.sent == [] and al.setup_alerted is True)

# ---------- discovery: the three switches ----------
m.LANG = 'es'
d = dict(m.discovery_messages(True, 'x_zigbee_monitor'))
sw = json.loads(d['homeassistant/switch/zigbee_monitor/notify_devices/config'])
check('interruptor en el dispositivo Zigbee Monitor, sección configuración',
      sw['device']['identifiers'] == ['zigbee_monitor'] and sw['entity_category'] == 'config', sw)
check('interruptor: ids y temas', sw['default_entity_id'] == 'switch.zigbee_monitor_notify_devices'
      and sw['unique_id'] == 'zigbee_monitor_notify_devices' and sw['state_topic'] == 'zigbee_monitor/settings'
      and sw['command_topic'] == 'zigbee_monitor/settings/notify_devices/set' and sw['name'] == 'Notificar dispositivos', sw)
check('los tres interruptores', all('homeassistant/switch/zigbee_monitor/%s/config' % k in d
      for k in ('notify_devices', 'notify_general_failure', 'notify_system')))

# ---------- panel API ----------
st = m.Settings(Path(tempfile.mkdtemp()) / 's.json')
st.targets, st.discovery = True, True
srv = m.start_panel(m.Journal(Path(tempfile.mkdtemp()) / 'e.log'), m.PanelState(), port=18898, settings=st)
def call(method='GET', body=None, header='settings'):
    headers = {'Content-Type': 'application/json'}
    if header:
        headers['X-Zigbee-Monitor'] = header
    req = urllib.request.Request('http://127.0.0.1:18898/api/settings', method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
code, data = call()
check('GET ajustes', code == 200 and data['notify'] == {'devices': True, 'general': True, 'system': True}
      and data['entities']['general'] == 'switch.zigbee_monitor_notify_general_failure' and data['targets'] is True, data)
code, data = call('POST', {'notify': {'devices': False}})
check('POST cambia al instante', code == 200 and data['notify']['devices'] is False and st.notifies('devices') is False, data)
check('POST sin cabecera: 403', call('POST', {'notify': {'devices': True}}, header=None)[0] == 403)
check('POST inválido: 400', call('POST', {'notify': {'devices': 'no'}})[0] == 400 and call('POST', {'notify': {'x': True}})[0] == 400
      and call('POST', {'notify': {}})[0] == 400)
check('tras rechazos: sin cambios', st.notifies('devices') is False)
html = m.render_page()
check('panel: pestaña Ajustes', 'tab-settings' in html and 'view-settings' in html and 'innerHTML' not in html)
srv.shutdown()

# ---------- end to end: a Home Assistant switch turns device notifications off ----------
data = Path(tempfile.mkdtemp())
m.DATA = data; m.OPTIONS = data / 'options.json'; m.DEVICES = data / 'devices.json'
m.EVENTS = data / 'eventos.log'; m.ALERTS = data / 'notificados.json'; m.SETTINGS = data / 'settings.json'
m.DeviceStore.__init__.__defaults__ = (m.DEVICES,)
m.Journal.__init__.__defaults__ = (m.EVENTS,)
m.Alerts.__init__.__defaults__ = (m.ALERTS, None, None, None)
m.Settings.__init__.__defaults__ = (m.SETTINGS,)
m.journal_write = lambda kind, message: m.JOURNAL and m.JOURNAL.write(kind, message)
m.SETTLE_MQTT = 1
A, B = '0x%016x' % 1, '0x%016x' % 2
m.OPTIONS.write_text(json.dumps({'mqtt_host': 'broker', 'mqtt_port': 1883, 'mqtt_user': 'u', 'mqtt_password': 'p',
                                 'mqtt_tls': False, 'z2m_base_topic': 'zigbee2mqtt', 'language': 'es',
                                 'mqtt_discovery': True, 'notify_targets': ['notify.x']}))
m.DEVICES.write_text(json.dumps({'watched': [A, B], 'names': {A: 'Luz A', B: 'Sensor B'},
                                 'seen': {A: '2026-01-01T00:00:00', B: '2026-01-01T00:00:00'}, 'initial': [A, B]}))
m.supervisor = lambda path, body=None: {'slug': 'local_zigbee_monitor'} if path == 'addons/self/info' else (_ for _ in ()).throw(RuntimeError(path))
class NoListener:
    def __init__(self, actions): pass
    def start(self): return self
m.ActionListener = NoListener
SENT = []
m.send_notification = lambda target, msg: SENT.append(msg)
devs = json.dumps([{'type': 'Coordinator', 'ieee_address': '0x00', 'friendly_name': 'C'},
                   {'type': 'Router', 'ieee_address': A, 'friendly_name': 'Luz A'},
                   {'type': 'Router', 'ieee_address': B, 'friendly_name': 'Sensor B'}])
av = lambda name, state: ('msg', ('zigbee2mqtt/%s/availability' % name, json.dumps({'state': state})))
fake.T0 = time.monotonic()
fake.SCRIPT[:] = [(0, 'msg', ('zigbee2mqtt/bridge/state', 'online')), (0, 'msg', ('zigbee2mqtt/bridge/devices', devs)),
                  (0, *av('Luz A', 'online')), (0, *av('Sensor B', 'online')),
                  (3, 'msg', ('zigbee_monitor/settings/notify_devices/set', 'OFF')),
                  (4, *av('Luz A', 'offline'))]
threading.Thread(target=lambda: (time.sleep(9), os.kill(os.getpid(), signal.SIGTERM)), daemon=True).start()
m.main()
lines = [l.split(' | ', 2)[1:] for l in m.EVENTS.read_text().splitlines()]
for l in lines:
    print('   ', l)
check('arranque: notificaciones activas en la línea', lines[0][1].endswith(' · Notificaciones: todas'), lines[0])
check('cambio desde Home Assistant en el historial',
      ['SYSTEM', 'Notificaciones de dispositivos: desactivadas (Home Assistant)'] in lines, lines)
check('el offline queda en el historial', any(k == 'PROBLEM' and 'Luz A' in msg for k, msg in lines), lines)
check('...pero no se notifica', not any('Luz A' in msg for msg in SENT), SENT)
check('ajuste guardado', json.loads(m.SETTINGS.read_text())['notify']['devices'] is False)
states = [json.loads(p) for t, p, r in fake.TOPICS if t == 'zigbee_monitor/settings' and r]
check('estado de los interruptores publicado (retenido) y actualizado',
      states and states[0]['notify_devices'] is True and states[-1] == {'notify_devices': False, 'notify_general_failure': True,
                                                                         'notify_system': True}, states)

print('\nFAILS:', fails)
sys.exit(1 if fails else 0)
