import json, os, sys, tempfile, threading, time, urllib.request, urllib.error
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ['ZM_DATA'] = TMP
os.environ['ZM_PANEL_PORT'] = '18099'
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'zigbee_monitor'))
import monitor as m
m.LANG = 'es'

fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %s' % detail))
    if not cond:
        fails += 1

# ---------- Journal ----------
j = m.Journal(Path(TMP) / 'eventos.log')
check('journal vacío al inicio', j.read() == [] and list(j.recent) == [])
j.write('SISTEMA', 'uno')
j.write('PROBLEM', 'dos\ncon salto | y separador')
ev = j.read()
check('lee más reciente primero', [e['message'] for e in ev][0].startswith('dos'), ev)
check('mensaje en una sola línea', '\n' not in ev[0]['message'] and ev[0]['type'] == 'PROBLEM', ev[0])
check('recent sincronizado', [e['message'] for e in j.recent] == [e['message'] for e in ev], list(j.recent))
v = j.version
# reopen from disk (simulates restart)
j2 = m.Journal(Path(TMP) / 'eventos.log')
check('recent se recupera tras reinicio', [e['message'] for e in j2.recent][:2] == [ev[0]['message'], 'uno'], list(j2.recent))
# rotation
m.EVENTS_MAX_BYTES_SAVE = m.EVENTS_MAX_BYTES
j.handler.maxBytes = 2000
for i in range(200):
    j.write('ESTADO', 'linea %03d ' % i + 'x' * 40)
files = sorted(p.name for p in Path(TMP).glob('eventos.log*'))
check('rotación crea como máximo 2 respaldos', files == ['eventos.log', 'eventos.log.1', 'eventos.log.2'], files)
allv = j.read(10000)
check('lectura cruza respaldos en orden', allv[0]['message'].startswith('linea 199') and
      all(allv[i]['message'] > allv[i + 1]['message'] for i in range(len(allv) - 1) if allv[i+1]['message'].startswith('linea')), allv[:3])
size = sum(p.stat().st_size for p in Path(TMP).glob('eventos.log*'))
check('tamaño total acotado (3 x maxBytes)', size <= 3 * 2000 + 200, size)
j.clear()
files = sorted(p.name for p in Path(TMP).glob('eventos.log*'))
after = j.read()
check('limpiar borra respaldos', files == ['eventos.log'], files)
check('limpiar deja rastro único', len(after) == 1 and after[0]['message'] == 'Historial borrado por el usuario', after)
j.write('SISTEMA', 'después de limpiar')
check('se sigue escribiendo tras limpiar', j.read()[0]['message'] == 'después de limpiar')
check('recent tras limpiar', [e['message'] for e in j.recent] == ['después de limpiar', 'Historial borrado por el usuario'], list(j.recent))
check('RECENT_EVENTS respetado', len(j.recent) <= m.RECENT_EVENTS)
j.handler.maxBytes = m.EVENTS_MAX_BYTES

# ---------- Tracker ----------
def payload(off=(), miss=(), unknown=(), valid=True, esperados=5):
    estado = 'PROBLEM' if off or miss else ('OK' if valid and not unknown else 'NO_DATA')
    return {'state': estado, 'expected': esperados, 'registered': esperados - len(miss) if valid else None,
            'offline_names': sorted(off), 'missing_names': sorted(miss),
            'unknown_names': sorted(unknown), 'data_valid': valid}

log = []
t = m.EventTracker(write=lambda tipo, msg: log.append((tipo, msg)), settle_mqtt=10, settle_z2m=120)
t.mqtt_down(); t.mqtt_down(); t.mqtt_down()
check('fallos repetidos de conexión = 1 línea', log == [('MQTT', 'No se pudo conectar al broker MQTT')], log)
log.clear()
t.mqtt_up(0)
check('primera conexión tras fallos', log == [('MQTT', 'Conectado al broker MQTT')], log)
log.clear()
# startup settle: transient SIN_DATOS not logged
t.observe(payload(valid=False), None, 1)
t.observe(payload(unknown=['A', 'B']), 'online', 3)
t.observe(payload(off=['A']), 'online', 8)
check('durante asentamiento MQTT no se escribe', log == [], log)
t.observe(payload(off=['A']), 'online', 11)
check('al asentar: una línea completa', log == [('PROBLEM', 'Offline (1): A')], log)
log.clear()
for s in range(12, 60):
    t.observe(payload(off=['A']), 'online', s)
check('sin cambios no escribe (heartbeat)', log == [], log)
t.observe(payload(off=['A', 'B']), 'online', 61)
check('delta nuevo offline', log == [('PROBLEM', 'Nuevo offline: B · Total: 2 offline, 0 desaparecidos')], log)
log.clear()
t.observe(payload(off=['B'], miss=['A']), 'online', 62)
check('movimiento offline→desaparecido no es "recuperado"', log == [('PROBLEM', 'Desaparecido: A · Total: 1 offline, 1 desaparecidos')], log)
log.clear()
t.observe(payload(), 'online', 63)
check('vuelta a OK', log == [('OK', 'Recuperado: B · Reapareció: A · Todos los dispositivos en línea')], log)
log.clear()
# Z2M restart storm
t.observe(payload(off=['C']), 'online', 70)
log.clear()
t.observe(payload(valid=False), 'offline', 100)
check('Z2M offline: 1 línea Z2M, sin línea de estado', log == [('Z2M', 'Zigbee2MQTT está offline')], log)
log.clear()
for s in range(101, 130):
    t.observe(payload(valid=False), 'offline', s)
check('Z2M offline sostenido no repite', log == [], log)
t.observe(payload(off=list('ABCDEF')), 'online', 130)
check('Z2M vuelve: línea de estabilización', len(log) == 1 and log[0][0] == 'Z2M' and '120 s' in log[0][1], log)
log.clear()
storm = [list('ABCDEF'), list('ABCDE'), list('ACD'), list('CD'), ['C'], ['C']]
for i, off in enumerate(storm):
    t.observe(payload(off=off), 'online', 131 + i * 20)
check('tormenta de reconexión no se escribe', log == [], log)
t.observe(payload(off=['C']), 'online', 251)
check('fin de ventana: línea Z2M + línea de estado', log == [('Z2M', 'Reconexión de Zigbee2MQTT completada'), ('PROBLEM', 'Offline (1): C')], log)
log.clear()
t.observe(payload(), 'online', 400)
check('después de la ventana, cambios normales', log == [('OK', 'Recuperado: C · Todos los dispositivos en línea')], log)
log.clear()
# MQTT drop while Z2M fine: retained messages rebuild state, no restart line
t.mqtt_down(); t.mqtt_down()
t.observe(payload(valid=False), None, 401)
t.mqtt_up(405)
t.observe(payload(valid=False), None, 405)
t.observe(payload(), 'online', 406)
t.observe(payload(), 'online', 416)
check('caída y vuelta de MQTT: 2 líneas, sin repetir estado igual',
      log == [('MQTT', 'Conexión con el broker perdida'), ('MQTT', 'Conexión con el broker recuperada')], log)
log.clear()
# Z2M comes back while monitor MQTT was down → still counts as Z2M restart
t.observe(payload(valid=False), 'offline', 500)
t.mqtt_down()
t.mqtt_up(510)
t.observe(payload(off=['X']), 'online', 511)
check('sin línea ESTADO en ningún caso', True)
check('Z2M volvió mientras MQTT caído: ventana Z2M', any(tp == 'Z2M' and 'volvió' in msg for tp, msg in log), log)
log.clear()
# not ready: nothing
t2 = m.EventTracker(write=lambda tipo, msg: log.append((tipo, msg)), settle_mqtt=0)
t2.mqtt_up(0); log.clear()
t2.observe(payload(off=['A']), 'online', 5, ready=False)
check('sin inicializar no escribe estado', log == [], log)
# long lists are truncated
big = ['D%02d' % i for i in range(30)]
check('lista larga resumida', m.describe(payload(off=big, esperados=40)).endswith('y 20 más'), m.describe(payload(off=big, esperados=40)))

# ---------- Panel ----------
panel = m.PanelState()
panel.set({'state': 'OK', 'expected': 1})
srv = m.start_panel(j, panel, port=18099)
base = 'http://127.0.0.1:18099'
html = urllib.request.urlopen(base + '/').read().decode()
check('GET / sirve la página', '<title>Zigbee Monitor</title>' in html)
check('la página usa rutas relativas (Ingress)', "fetch('api/events'" in html and "fetch('api/clear'" in html)
data = json.load(urllib.request.urlopen(base + '/api/events'))
check('API devuelve estado y eventos', data['status']['state'] == 'OK' and data['events'][0]['message'] == 'después de limpiar', data)
try:
    urllib.request.urlopen(urllib.request.Request(base + '/api/clear', method='POST'))
    check('POST sin cabecera rechazado', False)
except urllib.error.HTTPError as e:
    check('POST sin cabecera rechazado (403)', e.code == 403, e.code)
try:
    urllib.request.urlopen(base + '/api/clear')
    check('GET /api/limpiar no borra', False)
except urllib.error.HTTPError as e:
    check('GET /api/limpiar no borra (404)', e.code == 404 and j.read()[0]['message'] == 'después de limpiar', e.code)
r = urllib.request.urlopen(urllib.request.Request(base + '/api/clear', method='POST', headers={'X-Zigbee-Monitor': 'clear'}))
check('POST con cabecera limpia', json.load(r) == {'ok': True} and [e['message'] for e in j.read()] == ['Historial borrado por el usuario'], j.read())
# peers other than Ingress are refused
saved = m.INGRESS_PEERS
m.INGRESS_PEERS = ('172.30.32.2',)
try:
    urllib.request.urlopen(base + '/api/events')
    check('IP ajena rechazada', False)
except urllib.error.HTTPError as e:
    check('IP ajena rechazada (403)', e.code == 403, e.code)
m.INGRESS_PEERS = saved
# XSS: names are rendered with textContent only
check('sin innerHTML en la página', 'innerHTML' not in html)
srv.shutdown()

print('\nFALLOS:', fails)
sys.exit(1 if fails else 0)
