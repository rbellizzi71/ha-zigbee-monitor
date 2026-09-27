import json, os, sys, tempfile, time, urllib.error
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ['ZM_DATA'] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'zigbee_monitor'))
import monitor as m
m.LANG = 'es'

fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %s' % detail))
    fails += not cond

def payload(off=(), miss=(), unknown=(), valid=True, esperados=5):
    estado = 'PROBLEM' if off or miss else ('OK' if valid and not unknown else 'NO_DATA')
    return {'state': estado, 'expected': esperados, 'registered': esperados - len(miss) if valid else None,
            'offline_names': sorted(off), 'missing_names': sorted(miss),
            'unknown_names': sorted(unknown), 'data_valid': valid}

class FakeNotifier:
    def __init__(self, targets=('notify.x',)):
        self.targets = list(targets); self.sent = []
    def send(self, msg, extra=None):
        self.sent.append(msg)

NAMES = {'A', 'B', 'C', 'D'}
path = Path(TMP) / 'notificados.json'

# ---------- devices ----------
n = FakeNotifier(); a = m.Alerts(n, NAMES, path=path, delay=120, group=0)
a.devices(payload(off=['A']), 0); a.flush(0)
check('primer problema se avisa', n.sent == ['Nuevo offline: A · Total: 1 offline, 0 desaparecidos'], n.sent)
a.devices(payload(off=['A']), 0); a.flush(0)
check('mismo estado no repite', len(n.sent) == 1, n.sent)
a.devices(payload(off=['A', 'B'], miss=['C']), 0); a.flush(0)
check('varios a la vez = 1 mensaje', n.sent[-1] == 'Nuevo offline: B · Desaparecido: C · Total: 2 offline, 1 desaparecidos', n.sent[-1])
n2 = FakeNotifier(); a2 = m.Alerts(n2, NAMES, path=path, delay=120, group=0)
a2.devices(payload(off=['A', 'B'], miss=['C']), 0); a2.flush(0)
check('tras reiniciar el monitor no repite conocidos', n2.sent == [], n2.sent)
a2.devices(payload(off=['B'], miss=['C']), 0); a2.flush(0)
check('recuperación', n2.sent == ['Recuperado: A · Total: 1 offline, 1 desaparecidos'], n2.sent)
a2.devices(payload(miss=['B', 'C']), 0); a2.flush(0)
check('offline→desaparecido no es recuperado', n2.sent[-1] == 'Desaparecido: B · Total: 0 offline, 2 desaparecidos', n2.sent[-1])
a2.devices(payload(miss=['C'], unknown=['B']), 0); a2.flush(0)
check('sin datos no cuenta como recuperado', len(n2.sent) == 2, n2.sent)
a2.devices(payload(valid=False), 0); a2.flush(0)
check('datos inválidos no avisan', len(n2.sent) == 2, n2.sent)
n3 = FakeNotifier(); a3 = m.Alerts(n3, {'B', 'D'}, path=path, delay=120, group=0)
check('dispositivo quitado de la lista se olvida', 'C' not in a3.problems and a3.problems == {'B': 'missing'}, a3.problems)
a3.devices(payload(), 0); a3.flush(0)
check('todo en línea', n3.sent == ['Recuperado: B · Todos los dispositivos en línea'], n3.sent)
saved = json.loads(path.read_text())
check('estado persistido', saved == {'problems': {}, 'z2m': False, 'mqtt': False, 'setup': False, 'availability': False}, saved)

# ---------- system ----------
n = FakeNotifier(); path.unlink(); s = m.Alerts(n, NAMES, path=path, delay=120, group=0)
s.system('down', 'online', 0); s.system('down', 'online', 119)
check('MQTT caído <2 min: nada', n.sent == [], n.sent)
s.system('down', 'online', 120); s.system('down', 'online', 500)
check('MQTT caído 2 min: 1 aviso', n.sent == ['El monitor no tiene conexión con el broker MQTT desde hace más de 2 minutos'], n.sent)
s.system('up', 'online', 501)
check('MQTT recuperado se avisa', n.sent[-1] == 'El monitor recuperó la conexión con el broker MQTT', n.sent)
n.sent.clear()
s.system('up', 'offline', 600); s.system('up', 'offline', 660); s.system('up', 'online', 661)
check('reinicio planificado de Z2M (<2 min): nada', n.sent == [], n.sent)
s.system('up', 'offline', 700); s.system(None, 'offline', 750); s.system('up', 'offline', 820)
check('Z2M offline 2 min: aviso', n.sent == ['Zigbee2MQTT está offline desde hace más de 2 minutos'], n.sent)
s.system('up', 'offline', 900)
check('Z2M offline sostenido: no repite', len(n.sent) == 1, n.sent)
n4 = FakeNotifier(); s4 = m.Alerts(n4, NAMES, path=path, delay=120, group=0)
s4.system('up', 'online', 0)
check('aviso de vuelta de Z2M tras reiniciar el monitor', n4.sent == ['Zigbee2MQTT volvió a estar online'], n4.sent)
s.system('up', 'online', 950); n.sent.clear(); s.system('down', 'online', 1000); s.system('down', 'online', 1050)
s.system('up', 'online', 1051)
check('caída de MQTT <2 min no avisa recuperación', n.sent == [], n.sent)

# ---------- disabled ----------
path.unlink()
off = m.Alerts(m.Notifier([]), NAMES, path=path, group=0)
off.devices(payload(off=['A']), 0); off.flush(0); off.system('down', None, 0); off.system('down', None, 999)
check('sin destinos: nada y sin archivo', not path.exists())

# ---------- Notifier ----------
lines = []
def deliver(target, msg):
    if target == 'notify.malo':
        raise urllib.error.HTTPError('u', 400, 'bad', {}, None)
nt = m.Notifier(['notify.a', 'notify.malo', 'notify.b'], write=lambda t, msg: lines.append((t, msg)), send=deliver)
nt.send('hola'); nt.flush(5)
check('envío parcial registrado', lines == [('NOTIFY', 'Enviado a 2 destinos: hola'), ('NOTIFY', 'Falló el envío a: notify.malo (HTTP 400)')], lines)
slow = []
def slow_deliver(t, msg):
    time.sleep(1); slow.append(t)
ns = m.Notifier(['notify.a'], write=lambda *a: None, send=slow_deliver)
t0 = time.monotonic(); ns.send('x'); elapsed = time.monotonic() - t0
check('send no bloquea el bucle MQTT', elapsed < 0.1, elapsed)
ns.flush(5)
check('flush espera el envío pendiente', slow == ['notify.a'], slow)

# ---------- routing entity vs legacy ----------
calls = []
def fake_core(method, path, body=None):
    calls.append((method, path, body))
    if path == 'states/notify.todos_los_telefonos':
        return {'entity_id': 'notify.todos_los_telefonos'}
    if path.startswith('states/'):
        raise urllib.error.HTTPError('u', 404, 'nf', {}, None)
    return []
m.core_api = fake_core
m.send_notification('notify.todos_los_telefonos', 'm1')
m.send_notification('notify.mobile_app_iphone_16_pro_max_rb', 'm2')
check('entidad notify → notify.send_message', calls[1] == ('POST', 'services/notify/send_message',
      {'entity_id': 'notify.todos_los_telefonos', 'title': 'Zigbee Monitor', 'message': 'm1'}), calls[:2])
check('servicio clásico → notify/<nombre>', calls[3] == ('POST', 'services/notify/mobile_app_iphone_16_pro_max_rb',
      {'title': 'Zigbee Monitor', 'message': 'm2'}), calls[2:])
def core_500(method, path, body=None):
    raise urllib.error.HTTPError('u', 502, 'down', {}, None)
m.core_api = core_500
try:
    m.send_notification('notify.x', 'm'); check('HA caído propaga el error', False)
except urllib.error.HTTPError as e:
    check('HA caído propaga el error (sin tratarlo como servicio)', e.code == 502)

# ---------- options ----------
base = {'mqtt_host': 'h', 'mqtt_port': 1883, 'mqtt_user': '', 'mqtt_password': '', 'mqtt_tls': False, 'devices': []}
check('sin clave notificaciones (instalación vieja) = []', m.notify_targets(base) == [])
check('duplicados eliminados', m.notify_targets(dict(base, notify_targets=['notify.a', 'notify.b', 'notify.a'])) == ['notify.a', 'notify.b'])
for bad in (['light.x'], ['notify.'], ['notify.A'], 'notify.a', [3]):
    try:
        m.notify_targets(dict(base, notify_targets=bad)); check('rechaza %r' % (bad,), False)
    except ValueError:
        check('rechaza %r' % (bad,), True)

# ---------- tracker + alerts: Z2M restart storm ----------
path.unlink(missing_ok=True)
n = FakeNotifier(); al = m.Alerts(n, NAMES, path=path, delay=120, group=0)
t = m.EventTracker(write=lambda *a: None, settle_mqtt=10, settle_z2m=120, alerts=al)
t.mqtt_up(0); t.observe(payload(off=['A']), 'online', 11); t.tick(11)
check('estado inicial avisado', n.sent == ['Nuevo offline: A · Total: 1 offline, 0 desaparecidos'], n.sent)
n.sent.clear()
t.observe(payload(valid=False), 'offline', 100); t.tick(100)
t.observe(payload(off=['A', 'B', 'C', 'D']), 'online', 130); t.tick(130)
for i, off in enumerate([['A', 'B', 'C'], ['A', 'C'], ['A', 'D'], ['A', 'D']]):
    t.observe(payload(off=off), 'online', 140 + i * 30); t.tick(140 + i * 30)
check('durante la tormenta no se avisa nada', n.sent == [], n.sent)
t.observe(payload(off=['A', 'D']), 'online', 251); t.tick(251)
check('al asentar solo la diferencia con antes del reinicio', n.sent == ['Nuevo offline: D · Total: 2 offline, 0 desaparecidos'], n.sent)

print('\nFALLOS:', fails)
sys.exit(1 if fails else 0)
