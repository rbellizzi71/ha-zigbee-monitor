import copy, json, os, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path
TMP = tempfile.mkdtemp(); os.environ['ZM_DATA'] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'zigbee_monitor'))
import monitor as m
m.LANG = 'es'
fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %r' % (detail,))); fails += not cond

A, B, C, D = ('0x%016x' % i for i in (1, 2, 3, 4))
Z2M = {A: 'Luz A', B: 'Sensor B', C: 'Puerta C'}

# ---------- DeviceStore ----------
path = Path(TMP) / 'devices.json'
st = m.DeviceStore(path)
check('almacén nuevo: no existe y sin vigilados', not st.exists and st.watched == set())
check('primera instantánea silenciosa', st.observe(dict(Z2M)) == [] and set(st.seen) == set(Z2M))
check('instantánea igual: nada nuevo', st.observe(dict(Z2M)) == [])
new = dict(Z2M, **{D: 'Enchufe D'})
check('dispositivo nuevo detectado una vez', st.observe(new) == ['Enchufe D'] and st.observe(new) == [])
added, removed = st.update([A, B, '0x00000000000000ff'], [], new)
check('vigilar solo los que existen en Z2M', added == ['Luz A', 'Sensor B'] and st.watched == {A, B}, (added, st.watched))
check('expected con nombres de Z2M', st.expected(new) == {A: 'Luz A', B: 'Sensor B'})
renamed = dict(new, **{A: 'Luz A renombrada'})
st.observe(renamed)
check('los nombres siguen a Z2M', st.expected(renamed)[A] == 'Luz A renombrada')
gone = {k: v for k, v in renamed.items() if k != B}
check('desaparecido conserva último nombre', st.expected(gone)[B] == 'Sensor B')
added, removed = st.update([], [B], gone)
check('dejar de vigilar un desaparecido', removed == ['Sensor B'] and st.watched == {A})
st2 = m.DeviceStore(path)
check('persistencia', st2.exists and st2.watched == {A} and st2.names[A] == 'Luz A renombrada' and D in st2.seen)
w, u = st2.view(renamed, lambda i: 'online')
check('vista: vigilados y sin vigilar (nuevo marcado)', [x['name'] for x in w] == ['Luz A renombrada'] and
      {x['name']: x['new'] for x in u} == {'Enchufe D': True, 'Puerta C': False, 'Sensor B': False}, [w, u])
st2.seen[C] = '2026-09-01T10:00:00'
w, u = st2.view(renamed, lambda i: 'online')
check('vista: antiguo no es nuevo', {x['name']: x['new'] for x in u}['Puerta C'] is False)
# problems first (offline, missing, no data, online), each group by name
mix = m.DeviceStore(Path(TMP) / 'mix.json')
ids = ['0x%016x' % i for i in range(10, 17)]
names_ = ['zeta', 'Alfa', 'beta', 'Gamma', 'delta', 'Epsilon', 'eta']
status_ = dict(zip(ids, ['online', 'unknown', 'offline', 'missing', 'offline', 'online', 'unknown']))
present = dict(zip(ids, names_))
mix.update(ids, [], present)
w, _ = mix.view(present, lambda i: status_[i])
check('vista: problemas arriba por grupo y por nombre', [(x['status'], x['name']) for x in w] == [
    ('offline', 'beta'), ('offline', 'delta'), ('missing', 'Gamma'), ('unknown', 'Alfa'), ('unknown', 'eta'),
    ('online', 'Epsilon'), ('online', 'zeta')], w)

# ---------- Monitor + report ----------
path.unlink()
store = m.DeviceStore(path)
mon = m.Monitor(store)
dev_payload = json.dumps([{'type': 'Coordinator', 'ieee_address': '0x00', 'friendly_name': 'Coord'}] +
                         [{'type': 'Router', 'ieee_address': i, 'friendly_name': n} for i, n in Z2M.items()]).encode()
mon.receive('zigbee2mqtt/bridge/state', b'{"state":"online"}')
check('receive devuelve True con instantánea', mon.receive('zigbee2mqtt/bridge/devices', dev_payload) is True)
for n in Z2M.values():
    mon.receive('zigbee2mqtt/%s/availability' % n, b'{"state":"online"}')
r = mon.report()
check('sin vigilados: NO_DATA no_devices (nunca OK)', r['state'] == 'NO_DATA' and r['reason'] == 'no_devices' and r['expected'] == 0, r)
check('describe no_devices', m.describe(r) == 'Ningún dispositivo seleccionado para vigilar')
store.update([A, B], [], dict(mon.actual))
mon.receive('zigbee2mqtt/Sensor B/availability', b'{"state":"offline"}')
r = mon.report()
check('con vigilados: PROBLEM normal', r['state'] == 'PROBLEM' and r['offline_names'] == ['Sensor B'] and 'reason' not in r, r)
v = mon.devices_view()
check('vista del panel con estado', {d['name']: d['status'] for d in v['watched']} == {'Luz A': 'online', 'Sensor B': 'offline'}
      and [d['name'] for d in v['unwatched']] == ['Puerta C'] and v['z2m_valid'], v)
mon.receive('zigbee2mqtt/bridge/state', b'{"state":"offline"}')
v = mon.devices_view()
check('Z2M offline: sin lista sin vigilar, estados desconocidos', not v['z2m_valid'] and v['unwatched'] == [] and
      all(d['status'] == 'unknown' for d in v['watched']), v)

lines = []
m.journal_write = lambda k, msg: lines.append((k, msg))

# ---------- Tracker + alerts with a changing watch list ----------
class FakeNotifier:
    targets = ['notify.x']
    def __init__(self): self.sent = []
    def send(self, msg, extra=None): self.sent.append(msg)
def payload(off=(), miss=(), expected=3, reason=None):
    p = {'state': 'PROBLEM' if off or miss else 'OK', 'expected': expected, 'offline_names': sorted(off), 'missing_names': sorted(miss),
         'unknown_names': [], 'data_valid': True}
    if reason:
        p.update(state='NO_DATA', reason=reason)
    return p
n = FakeNotifier(); al = m.Alerts(n, {'Luz A', 'Sensor B'}, path=Path(TMP) / 'n.json', group=0)
log = []
tr = m.EventTracker(write=lambda k, msg: log.append((k, msg)), settle_mqtt=0, alerts=al)
tr.mqtt_up(0); log.clear()
tr.observe(payload(expected=0, reason='no_devices'), 'online', 1)
check('primera instalación: línea NO_DATA', log == [('NO_DATA', 'Ningún dispositivo seleccionado para vigilar')], log)
check('aviso de configuración una vez', n.sent == ['Elige en el panel de Zigbee Monitor los dispositivos a vigilar'], n.sent)
tr.observe(payload(expected=0, reason='no_devices'), 'online', 2)
check('no repite el aviso', len(n.sent) == 1)
log.clear(); n.sent.clear()
tr.watch_list_changed([])
tr.observe(payload(off=['Sensor B'], expected=2), 'online', 3); tr.tick(3)
check('tras elegir: línea completa y aviso del offline', log == [('PROBLEM', 'Offline (1): Sensor B')] and
      n.sent == ['Nuevo offline: Sensor B · Total: 1 offline, 0 desaparecidos'], (log, n.sent))
log.clear(); n.sent.clear()
tr.watch_list_changed(['Sensor B'])
tr.observe(payload(expected=1), 'online', 4); tr.tick(4)
check('dejar de vigilar un offline: sin "Recuperado"', log == [('OK', 'El único dispositivo vigilado está en línea')] and n.sent == [], (log, n.sent))
tr.watch_list_changed(['Luz A'])
tr.observe(payload(expected=0, reason='no_devices'), 'online', 5)
check('quitar todos: vuelve el aviso de configuración', n.sent == ['Elige en el panel de Zigbee Monitor los dispositivos a vigilar'], n.sent)

# ---------- Panel API ----------
path.unlink(missing_ok=True)
store = m.DeviceStore(path); mon = m.Monitor(store)
mon.receive('zigbee2mqtt/bridge/state', b'{"state":"online"}'); mon.receive('zigbee2mqtt/bridge/devices', dev_payload)
store.observe(dict(mon.actual))
panel = m.PanelState()
srv = m.start_panel(m.Journal(Path(TMP) / 'e.log'), panel, port=18599, monitor=mon)
base = 'http://127.0.0.1:18599'
def post(body, header='devices'):
    req = urllib.request.Request(base + '/api/devices', data=json.dumps(body).encode(), method='POST',
                                 headers={'X-Zigbee-Monitor': header} if header else {})
    return json.load(urllib.request.urlopen(req))
view = json.load(urllib.request.urlopen(base + '/api/devices'))
check('GET /api/devices', view['z2m_valid'] and view['watched'] == [] and len(view['unwatched']) == 3, view)
for body, header, code in (({'add': [A]}, None, 403), ({'add': ['nope']}, 'devices', 400), ({'add': 'x'}, 'devices', 400)):
    try:
        post(body, header); check('rechazo %s' % code, False)
    except urllib.error.HTTPError as e:
        check('POST inválido → %d' % code, e.code == code, e.code)
r = post({'add': [A.upper().replace('0X', '0x'), B]})
check('vigilar vía API', r == {'added': ['Luz A', 'Sensor B'], 'removed': []} and store.watched == {A, B}, r)
check('cambio encolado para el bucle principal', panel.changes.get_nowait() == (['Luz A', 'Sensor B'], []))
r = post({'remove': [A]})
check('dejar de vigilar vía API', r == {'added': [], 'removed': ['Luz A']} and store.watched == {B})
mon.receive('zigbee2mqtt/bridge/state', b'{"state":"offline"}')
try:
    post({'add': [C]}); check('sin datos de Z2M no se puede añadir', False)
except urllib.error.HTTPError as e:
    check('sin datos de Z2M: añadir → 409', e.code == 409)
r = post({'remove': [B]})
check('sin datos de Z2M: quitar sí funciona', r['removed'] == ['Sensor B'] and store.watched == set())
html = urllib.request.urlopen(base + '/').read().decode()
check('panel: separadores de color por estado', "el('div', 'sep ' + group" in html and '.sep.offline' in html)
check('página con pestañas y sin innerHTML', 'tab-devices' in html and 'innerHTML' not in html and '"tab_devices": "Dispositivos"' in html)
srv.shutdown()
print('\nFALLOS:', fails); sys.exit(1 if fails else 0)
