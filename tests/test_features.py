"""1.0.0: base topic option, automatic MQTT, Z2M availability disabled, sensor rename, panel."""
import json, os, signal, sys, tempfile, threading, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp()
os.environ.update(ZM_DATA=TMP, ZM_SETTLE_Z2M='3', ZM_PANEL_PORT='18799')
sys.path.insert(0, str(HERE / 'fakepaho'))
sys.path.insert(0, str(HERE.parent / 'zigbee_monitor'))
import paho.mqtt.client as fake
import monitor as m
m.LANG = 'es'
fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %s' % detail))
    fails += not cond

# ---------- options ----------
opt = Path(TMP) / 'options.json'
def options(**kw):
    base = {'mqtt_tls': False, 'language': 'es', 'mqtt_discovery': True, 'notify_targets': []}
    opt.write_text(json.dumps(dict(base, **kw)))
    return m.read_options()
o = options()
check('MQTT opcional: host vacío por defecto', o['mqtt_host'] == '' and o['mqtt_port'] == 1883 and o['z2m_base_topic'] == 'zigbee2mqtt', o)
for good in ('zigbee2mqtt', 'z2m', 'casa/z2m'):
    check('tema base válido %r' % good, options(z2m_base_topic=good)['z2m_base_topic'] == good)
for bad in ('', '/z2m', 'z2m/', 'z 2m', 'z2m/#', 'a+b', 7):
    try:
        options(z2m_base_topic=bad); check('rechaza tema base %r' % (bad,), False)
    except ValueError:
        check('rechaza tema base %r' % (bad,), True)
try:
    options(mqtt_host='h', mqtt_password='x'); check('manual: contraseña sin usuario', False)
except ValueError:
    check('manual: contraseña sin usuario rechazada', True)
check('auto: contraseña vieja sin host no molesta', options(mqtt_password='x')['mqtt_host'] == '')

# ---------- broker ----------
service = lambda: {'host': 'core-mosquitto', 'port': 1883, 'ssl': False, 'protocol': '3.1.1',
                   'username': 'addons', 'password': 'secret', 'addon': 'core_mosquitto'}
b = m.mqtt_settings(options(), service)
check('auto: datos del broker de HA', b == {'mode': 'auto', 'host': 'core-mosquitto', 'port': 1883, 'user': 'addons',
                                            'password': 'secret', 'tls': False}, b)
b = m.mqtt_settings(options(mqtt_host=' 192.168.1.5 ', mqtt_port=1884, mqtt_user='u', mqtt_password='p', mqtt_tls=True), service)
check('manual: usa lo escrito', b == {'mode': 'manual', 'host': '192.168.1.5', 'port': 1884, 'user': 'u', 'password': 'p', 'tls': True}, b)
def no_service():
    raise RuntimeError('400')
for name, svc in (('sin Mosquitto', no_service), ('respuesta rara', lambda: {'host': ''})):
    try:
        m.mqtt_settings(options(), svc); check('auto %s: NoBroker' % name, False)
    except m.NoBroker:
        check('auto %s: NoBroker' % name, True)
check('mensaje sin broker claro', 'Mosquitto' in m.t('no_broker'))

# ---------- availability setting ----------
ae = m.availability_enabled
cases = [({'availability': {'enabled': True, 'active': {}}}, True), ({'availability': {'enabled': False}}, False),
         ({}, False), ({'availability': True}, True), ({'availability': False}, False),
         ({'availability': {'active': {'timeout': 10}}}, True), ({'advanced': {'availability_timeout': 60}}, True),
         ({'availability': {'enabled': False}, 'devices': {'0x1': {'friendly_name': 'x', 'availability': True}}}, True),
         (None, None), ('x', None)]
for config, expected in cases:
    check('availability_enabled(%r) = %r' % (config, expected), ae(config) is expected, ae(config))

# ---------- Monitor: custom base + availability ----------
A, B = '0x%016x' % 1, '0x%016x' % 2
store = m.DeviceStore(Path(TMP) / 'dev.json')
mon = m.Monitor(store, 'casa/z2m')
devs = json.dumps([{'type': 'Coordinator', 'ieee_address': '0x00', 'friendly_name': 'C'},
                   {'type': 'Router', 'ieee_address': A, 'friendly_name': 'Luz A'},
                   {'type': 'EndDevice', 'ieee_address': B, 'friendly_name': 'Sensor B'}]).encode()
check('ignora el tema por defecto', not mon.wants('zigbee2mqtt/bridge/devices') and mon.wants('casa/z2m/bridge/info')
      and mon.wants('casa/z2m/Luz A/availability') and not mon.wants('casa/z2m/Luz A'))
mon.receive('casa/z2m/bridge/state', b'online')
mon.receive('casa/z2m/bridge/devices', devs)
mon.receive('casa/z2m/Luz A/availability', b'{"state":"online"}')
mon.receive('casa/z2m/Sensor B/availability', b'{"state":"offline"}')
store.update([A, B], [], dict(mon.actual))
r = mon.report()
check('base propia: datos leídos', r['state'] == 'PROBLEM' and r['offline_names'] == ['Sensor B'] and r['z2m_availability'] is None
      and 'reason' not in r, r)
mon.receive('casa/z2m/bridge/info', json.dumps({'version': '2.6.0', 'config': {'availability': {'enabled': False}}}).encode())
r = mon.report()
check('availability desactivada: retenidos viejos ignorados', r['state'] == 'NO_DATA' and r['unknown_names'] == ['Luz A', 'Sensor B']
      and r['offline_names'] == [] and r['reason'] == 'z2m_availability_disabled' and r['z2m_availability'] is False, r)
check('panel: estados sin datos', {d['status'] for d in mon.devices_view()['watched']} == {'unknown'})
mon.receive('casa/z2m/bridge/info', json.dumps({'config': {'availability': {'enabled': True}}}).encode())
r = mon.report()
check('activada otra vez: vuelve a la normalidad', r['state'] == 'PROBLEM' and 'reason' not in r and r['z2m_availability'] is True, r)

# ---------- tracker + alerts ----------
class FakeNotifier:
    targets = ['notify.x']
    def __init__(self): self.sent = []
    def send(self, msg, extra=None): self.sent.append(msg)
log = []
n = FakeNotifier()
alerts = m.Alerts(n, {'Luz A', 'Sensor B'}, path=Path(TMP) / 'al.json', delay=120, group=0)
tr = m.EventTracker(write=lambda k, msg: log.append((k, msg)), settle_mqtt=0, settle_z2m=0, alerts=alerts)
tr.mqtt_up(0); log.clear()
def pay(avail):
    p = {'state': 'NO_DATA', 'expected': 2, 'registered': 2, 'offline_names': [], 'missing_names': [],
         'unknown_names': ['Luz A', 'Sensor B'], 'data_valid': True, 'z2m_availability': avail}
    if avail is False:
        p['reason'] = 'z2m_availability_disabled'
    return p
tr.observe(pay(None), 'online', 1)
tr.observe(pay(False), 'online', 2)
tr.observe(pay(False), 'online', 3)
check('una línea al detectarla', [x for x in log if x[0] == 'SYSTEM'] == [('SYSTEM', m.t('availability_off'))], log)
check('una sola notificación', n.sent.count(m.t('alert_availability')) == 1, n.sent)
alerts2 = m.Alerts(n, {'Luz A', 'Sensor B'}, path=Path(TMP) / 'al.json', delay=120, group=0)
tr2 = m.EventTracker(write=lambda k, msg: log.append((k, msg)), settle_mqtt=0, alerts=alerts2)
tr2.mqtt_up(0); tr2.observe(pay(False), 'online', 1)
check('tras reiniciar el monitor no se repite la notificación', n.sent.count(m.t('alert_availability')) == 1, n.sent)
log.clear()
tr2.observe(dict(pay(True), state='OK', unknown_names=[]), 'online', 5)
check('al activarla: línea de sistema', ('SYSTEM', m.t('availability_on')) in log, log)
check('memoria de avisos reiniciada', json.loads((Path(TMP) / 'al.json').read_text())['availability'] is False)

# ---------- discovery + panel ----------
d = dict(m.discovery_messages(True))
st = json.loads(d['homeassistant/sensor/zigbee_monitor/status/config'])
check('sensor principal renombrado', st['unique_id'] == 'zigbee_monitor_status' and st['default_entity_id'] == 'sensor.zigbee_monitor_status', st)
check('atributo z2m_availability', 'z2m_availability' in st['json_attributes_template'])
check('sin tema viejo', not any('/state/config' in k for k in d))
html = m.render_page()
check('panel: banner de availability', 'availability-off' in html and m.PANEL_TEXTS['es']['availability_banner'] in html)
check('panel: refresco rápido tras cambios', 'setTimeout(load, 1500)' in html and 'setTimeout(load, 3000)' in html)
check('panel: aviso de desaparecidos al dejar de vigilar', 'confirm_unwatch_missing' in html and "d.status === 'missing'" in html)
check('sin tipos legacy', 'STATE' not in json.dumps(m.PANEL_TEXTS) and m.parse_event('x | ESTADO | y')['type'] == 'ESTADO')

# ---------- end to end: automatic MQTT, custom base topic, availability disabled then enabled ----------
data = Path(tempfile.mkdtemp())
m.DATA = data; m.OPTIONS = data / 'options.json'; m.DEVICES = data / 'devices.json'
m.EVENTS = data / 'eventos.log'; m.ALERTS = data / 'notificados.json'; m.SETTINGS = data / 'settings.json'
m.DeviceStore.__init__.__defaults__ = (m.DEVICES,)
m.Journal.__init__.__defaults__ = (m.EVENTS,)
m.Alerts.__init__.__defaults__ = (m.ALERTS, None, None, None)
m.Settings.__init__.__defaults__ = (m.SETTINGS,)
m.SETTLE_MQTT = 1
m.OPTIONS.write_text(json.dumps({'mqtt_tls': False, 'z2m_base_topic': 'casa/z2m', 'language': 'es',
                                 'mqtt_discovery': True, 'notify_targets': ['notify.x']}))
m.DEVICES.write_text(json.dumps({'watched': [A, B], 'names': {A: 'Luz A', B: 'Sensor B'},
                                 'seen': {A: '2026-01-01T00:00:00', B: '2026-01-01T00:00:00'}, 'initial': [A, B]}))
SUP = []
def fake_supervisor(path, body=None):
    SUP.append(path)
    if path == 'services/mqtt':
        return service()
    if path == 'addons/self/info':
        return {'slug': 'local_zigbee_monitor'}
    raise RuntimeError(path)
m.supervisor = fake_supervisor
SENT = []
m.send_notification = lambda target, msg: SENT.append(msg)
info = lambda enabled: ('msg', ('casa/z2m/bridge/info', json.dumps({'config': {'availability': {'enabled': enabled}}})))
fake.T0 = time.monotonic()
fake.SCRIPT[:] = [(0, 'msg', ('casa/z2m/bridge/state', '{"state":"online"}')), (0, 'msg', ('casa/z2m/bridge/devices', devs.decode())),
                  (0, 'msg', ('casa/z2m/Luz A/availability', '{"state":"online"}')),
                  (0, 'msg', ('casa/z2m/Sensor B/availability', '{"state":"online"}')),
                  (0, 'msg', ('zigbee2mqtt/bridge/devices', '[]')),  # another Z2M instance: ignored
                  (0, *info(False)), (4, *info(True))]
threading.Thread(target=lambda: (time.sleep(7), os.kill(os.getpid(), signal.SIGTERM)), daemon=True).start()
m.main()
lines = [l.split(' | ', 2)[1:] for l in m.EVENTS.read_text().splitlines()]
for l in lines:
    print('   ', l)
check('arranque: MQTT automático y tema base', lines[0] == ['SYSTEM', 'Monitor iniciado (v%s): 2 dispositivos vigilados, 1 destino de notificación · MQTT: automático · Z2M: casa/z2m · Notificaciones: todas' % m.VERSION], lines[0])
check('conectó al broker de HA con sus credenciales', fake.CONNECTS[0] == ('core-mosquitto', 1883, 'addons'), fake.CONNECTS)
check('suscrito solo al tema base', 'services/mqtt' in SUP)
body = [x[1] for x in lines]
check('historial: availability desactivada y luego activada',
      m.t('availability_off') in body and m.t('availability_on') in body and body.index(m.t('availability_off')) < body.index(m.t('availability_on')), body)
check('estado final OK', ['OK', 'Los 2 dispositivos vigilados están en línea'] in lines, lines)
check('notificación de availability enviada una vez', SENT.count(m.t('alert_availability')) == 1, SENT)
topics = {t for t, p, r in fake.TOPICS}
check('publica el sensor nuevo', 'homeassistant/sensor/zigbee_monitor/status/config' in topics)
status = [p for _, p in fake.PUBLISHED if isinstance(p, dict) and 'z2m_availability' in p]
check('payload incluye z2m_availability False y luego True', any(p['z2m_availability'] is False for p in status) and status[-2]['z2m_availability'] is True, [p['z2m_availability'] for p in status])

# no broker at all
m.OPTIONS.write_text(json.dumps({'mqtt_tls': False, 'language': 'es', 'mqtt_discovery': True, 'notify_targets': []}))
m.supervisor = lambda path, body=None: (_ for _ in ()).throw(RuntimeError('no mqtt'))
try:
    m.main(); check('sin broker: se detiene', False)
except SystemExit as e:
    check('sin broker: se detiene con código 1 y línea clara', e.code == 1 and m.EVENTS.read_text().splitlines()[-1].endswith(m.t('no_broker')))
print('\nFALLOS:', fails)
sys.exit(1 if fails else 0)
