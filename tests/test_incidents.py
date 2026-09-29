"""1.4.0: Incidents tab. Automatic incident reports (general failure, Zigbee2MQTT down), kept across restarts
and power cuts, plus the unexpected-stop line."""
import json, os, signal, sys, tempfile, threading, time, urllib.request, urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp()
os.environ.update(ZM_DATA=TMP, ZM_PANEL_PORT='18896', ZM_NOTIFY_GROUP='1', ZM_SETTLE_Z2M='1',
                  ZM_INCIDENT_SETTLE='2', ZM_ALERT_DELAY='120')
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

now = [1_800_000_000.0]
clock = lambda: now[0]
HISTORY = []

def recorder(**kw):
    d = Path(tempfile.mkdtemp())
    tr = m.Traffic(d / 't.json', clock=clock)
    inc = m.Incidents(tr, directory=d / 'incidents', write=lambda k, msg: HISTORY.append((k, msg)), clock=clock, **kw)
    inc.threaded = False
    return inc, tr, d

def payload(off=(), miss=()):
    return {'data_valid': True, 'offline_names': sorted(off), 'missing_names': sorted(miss)}

def step(seconds=1):
    now[0] += seconds

N = ['D%d' % i for i in range(8)]

# ---------- context: warnings kept, info counted, chatty devices only counted ----------
inc, tr, d = recorder()
inc.z2m('online')
inc.bridge_info(json.dumps({'version': '2.14.1', 'coordinator': {'type': 'ZStack3x0', 'ieee_address': '0x00124b', 'meta': {'revision': 20260310}},
                            'config': {'serial': {'adapter': 'zstack', 'port': 'tcp://192.168.1.7:6638'}}}))
for i in range(50):
    inc.z2m_log(json.dumps({'level': 'info', 'message': "MQTT publish: topic 'x'"}))
inc.z2m_log(json.dumps({'level': 'error', 'message': 'SRSP - SYS - ping after 6000ms'}))
inc.z2m_log('no json')
inc.device_availability('Luz', 'online')
inc.device_availability('Luz', 'online')
check('primera disponibilidad no es un evento', not any(e['kind'] == 'availability' for _, e in inc.context))
inc.device_availability('Luz', 'offline')
kinds = [e['kind'] for _, e in inc.context]
check('contexto: error guardado, info solo contado, cambio de disponibilidad', kinds == ['log', 'availability'] and len(inc.info_logs) == 50, kinds)
for i in range(400):
    inc.z2m_log(json.dumps({'level': 'warning', 'message': 'aviso %d' % i}))
check('límite de líneas del contexto: 300 más recientes', len(inc.context) == 300 and inc.context[-1][1]['text'] == 'warning: aviso 399'
      and len(inc.dropped) == 102, (len(inc.context), len(inc.dropped)))
step(700)
inc.z2m_log(json.dumps({'level': 'warning', 'message': 'nuevo'}))
check('ventana de 10 minutos', len(inc.context) == 1 and len(inc.dropped) == 0, len(inc.context))

# ---------- general failure: open, timeline, recovery, close ----------
HISTORY.clear()
inc, tr, d = recorder()
inc.z2m('online')
inc.observe(payload())
tr.count('0x%016x' % 1)
inc.message('Radar')
inc.z2m_log(json.dumps({'level': 'error', 'message': 'SRSP - SYS - ping after 6000ms'}))
for i in range(4):
    step(); inc.observe(payload(off=N[:i + 1]))
check('4 perdidos: sin incidente', inc.current is None)
step(); inc.observe(payload(off=N[:5]))
cur = inc.current
check('5 perdidos en un minuto: incidente abierto', cur is not None and cur['trigger'] == 'general' and [a['name'] for a in cur['affected']] == N[:5], cur and cur['affected'])
check('contexto con el error de Z2M', any('SRSP' in e['text'] for e in cur['context']), cur['context'])
check('coordinador y último mensaje', cur['coordinator'] is None and cur['last_message']['device'] == 'Radar')
files = list((d / 'incidents').glob('*.json'))
check('guardado al abrir', len(files) == 1 and json.loads(files[0].read_text())['status'] == 'open')
step(); inc.observe(payload(off=N[:6]))
check('otro caído durante el incidente: añadido', [a['name'] for a in cur['affected']] == N[:6])
step(); inc.action(m.t('inc_restart_requested'), restart=True)
step(); inc.observe(payload(off=['D0']))
inc.tick()
check('parcialmente recuperado: sigue abierto', inc.current is not None)
step(); inc.observe(payload())
inc.tick()
check('todo de vuelta: espera la estabilización', inc.current is not None)
step(3); inc.tick()
check('cerrado tras la estabilización', inc.current is None)
done = json.loads(files[0].read_text())
check('resultado: tras reinicio (se pidió reinicio)', done['status'] == 'closed' and done['outcome'] == 'restart' and done['duration_s'] == 7, done)
check('vueltas registradas', all(a['back'] for a in done['affected']), done['affected'])
check('línea de historial', HISTORY[-1] == ('SYSTEM', 'Incidente registrado: fallo general, 7 s, recuperado tras reinicio'), HISTORY)

# recovered by itself
inc, tr, d = recorder()
inc.z2m('online'); inc.observe(payload())
step(); inc.observe(payload(off=N[:5]))
step(); inc.observe(payload()); step(3); inc.tick()
check('recuperado solo', json.loads(next((d / 'incidents').glob('*.json')).read_text())['outcome'] == 'self')

# chain: one after another (each within NOTIFY_GROUP of the previous)
inc, tr, d = recorder()
inc.z2m('online'); inc.observe(payload())
for i in range(5):
    step(0.9 if i else 0); inc.observe(payload(off=N[:i + 1]))
    step(0.05)
check('cadena de caídas seguidas: incidente', inc.current is not None)

# ---------- Zigbee2MQTT down ----------
inc, tr, d = recorder()
inc.z2m('online'); inc.observe(payload())
step(); inc.z2m('offline')
step(60); inc.tick()
check('Z2M caído 1 min: sin incidente', inc.current is None)
step(61); inc.tick()
check('Z2M caído más de 2 min: incidente', inc.current is not None and inc.current['trigger'] == 'z2m')
step(); inc.z2m('online'); inc.observe(payload()); step(3); inc.tick()
z = json.loads(next((d / 'incidents').glob('*.json')).read_text())
check('Z2M vuelve: cerrado, tras reinicio', z['outcome'] == 'restart' and z['status'] == 'closed', z['outcome'])

# ---------- maximum duration and late recovery ----------
inc, tr, d = recorder()
inc.z2m('online'); inc.observe(payload())
step(); inc.observe(payload(off=N[:5]))
step(m.INCIDENT_MAX); inc.tick()
x = json.loads(next((d / 'incidents').glob('*.json')).read_text())
check('6 h sin recuperarse: no recuperado', x['outcome'] == 'not_recovered' and x['late_recovery'] is None)
step(); inc.observe(payload())
x = json.loads(next((d / 'incidents').glob('*.json')).read_text())
check('recuperación tardía anotada', x['late_recovery'] is not None)

# ---------- restart / power cut in the middle ----------
inc, tr, d = recorder()
inc.z2m('online'); inc.observe(payload())
step(); inc.observe(payload(off=N[:5]))
step(); inc.device_availability('D9', 'online'); inc.device_availability('D9', 'offline')
step(m.INCIDENT_FLUSH); inc.tick()
saved = json.loads(next((d / 'incidents').glob('*.json')).read_text())
check('corte de luz: lo reciente ya está en disco (≤5 s)', any(e['text'] == 'D9: offline' for e in saved['timeline']), saved['timeline'])
last_life = now[0]
step(600)  # the power comes back 10 minutes later
again = m.Incidents(m.Traffic(d / 't.json', clock=clock), directory=d / 'incidents', write=lambda *a: None, clock=clock)
check('al arrancar: retoma el incidente abierto', again.current is not None and again.resumed)
again.interruption(last_life)
check('anota la interrupción', again.current['timeline'][-1]['kind'] == 'gap' and again.current['interruptions'] == 1)
again.z2m('online'); again.observe(payload(off=N[:2]))
check('sigue caído: continúa abierto', again.current is not None)
again.observe(payload()); step(3); again.tick()
r = json.loads(next((d / 'incidents').glob('*.json')).read_text())
check('...y se cierra normalmente', r['outcome'] == 'self', r['outcome'])

inc, tr, d = recorder()
inc.z2m('online'); inc.observe(payload())
step(); inc.observe(payload(off=N[:5]))
inc.save()
step(300)
again = m.Incidents(None, directory=d / 'incidents', write=lambda *a: None, clock=clock)
again.interruption(now[0] - 300)
again.z2m('online'); again.observe(payload())
r = json.loads(next((d / 'incidents').glob('*.json')).read_text())
check('todo bien al volver: recuperado durante la interrupción', r['outcome'] == 'interruption', r['outcome'])

# ---------- coordinator entities ----------
inc, tr, d = recorder(entities=lambda: ['sensor.slzb_temp', 'sensor.nope'],
                      read_state=lambda e: {'state': '45.2', 'attributes': {'unit_of_measurement': '°C'}} if e == 'sensor.slzb_temp'
                      else (_ for _ in ()).throw(RuntimeError('404')))
inc.z2m('online'); inc.observe(payload())
step(); inc.observe(payload(off=N[:5]))
step(); inc.observe(payload()); step(3); inc.tick()
e = json.loads(next((d / 'incidents').glob('*.json')).read_text())['entities']
check('entidades al inicio y al final', e['start']['sensor.slzb_temp'] == {'state': '45.2', 'unit': '°C', 'name': None, 'last_changed': None}
      and e['end']['sensor.nope'] == {'error': 'RuntimeError'}, e)

# ---------- retention, summary, delete ----------
inc, tr, d = recorder()
inc.z2m('online'); inc.observe(payload())
for k in range(33):
    step(86400)
    inc.z2m_log(json.dumps({'level': 'error', 'message': 'SRSP - SYS - ping after %d ms' % (6000 + k)}))
    inc.observe(payload(off=N[:5])); step(); inc.observe(payload()); step(3); inc.tick()
items = inc.all()
check('se conservan los 30 últimos', len(items) == 30, len(items))
listing = inc.list()
s = listing['summary']
check('resumen: intervalo medio 1 día y error repetido', s['count'] == 30 and abs(s['mean_interval_s'] - 86400) <= 10
      and s['repeated_errors'][0][0] == 'error: SRSP - SYS - ping after # ms', s)
check('lista: más reciente primero', listing['incidents'][0]['id'] > listing['incidents'][-1]['id'])
step(); inc.observe(payload(off=N[:5]))
check('borrar uno', inc.delete(items[0]['id']) == 1 and len(inc.all()) == 30)  # 29 closed + 1 open
check('borrar todos: el abierto se conserva', inc.delete() == 29 and [i['status'] for i in inc.all()] == ['open'])
check('historial al borrar', HISTORY[-1] == ('SYSTEM', '29 informes de incidentes borrados'), HISTORY[-1])

# ---------- heartbeat ----------
hb_path = Path(TMP) / 'hb.json'
hb = m.Heartbeat(hb_path, clock=clock)
check('primer arranque: sin parada inesperada', hb.unexpected_stop() is None)
hb.beat()
check('tras un corte (clean false): parada inesperada', m.Heartbeat(hb_path, clock=clock).unexpected_stop() == now[0])
hb.beat(clean=True)
check('tras una parada ordenada: nada', m.Heartbeat(hb_path, clock=clock).unexpected_stop() is None)

# ---------- panel API ----------
inc, tr, d = recorder()
inc.z2m('online'); inc.observe(payload())
step(); inc.observe(payload(off=N[:5])); step(); inc.observe(payload()); step(3); inc.tick()
iid = inc.all()[0]['id']
srv = m.start_panel(m.Journal(Path(TMP) / 'e.log'), m.PanelState(), port=18895, incidents=inc)
base = 'http://127.0.0.1:18895/'
def get(path):
    with urllib.request.urlopen(base + path) as r:
        return r.status, dict(r.headers), r.read()
code, _, body = get('api/incidents')
check('API lista', code == 200 and json.loads(body)['incidents'][0]['id'] == iid)
code, _, body = get('api/incidents/' + iid)
check('API detalle', json.loads(body)['id'] == iid)
code, headers, body = get('api/incidents/%s/download' % iid)
check('API descarga uno', 'attachment' in headers.get('Content-Disposition', '') and json.loads(body)['id'] == iid, headers)
code, headers, body = get('api/incidents/download')
check('API descarga todos', len(json.loads(body)['incidents']) == 1 and 'attachment' in headers.get('Content-Disposition', ''))
for bad in ('api/incidents/../../x', 'api/incidents/nope'):
    try:
        get(bad); check('rechaza %s' % bad, False)
    except urllib.error.HTTPError as e:
        check('rechaza %s' % bad, e.code == 404, e.code)
def post(body, header='incidents'):
    headers = {'Content-Type': 'application/json'}
    if header:
        headers['X-Zigbee-Monitor'] = header
    req = urllib.request.Request(base + 'api/incidents/delete', data=json.dumps(body).encode(), method='POST', headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
check('borrar sin cabecera: 403', post({'id': iid}, header=None)[0] == 403)
check('borrar con id inválido: 400', post({'id': '../x'})[0] == 400 and post({})[0] == 400)
check('borrar uno por API', post({'id': iid}) == (200, {'deleted': 1}) and inc.all() == [])
html = m.render_page()
check('panel: pestañas en orden', html.index('tab-status') < html.index('tab-devices') < html.index('tab-traffic')
      < html.index('tab-incidents') < html.index('tab-settings') and 'innerHTML' not in html)
srv.shutdown()

# ---------- settings: coordinator entities ----------
st = m.Settings(Path(TMP) / 's.json')
srv = m.start_panel(m.Journal(Path(TMP) / 'e2.log'), m.PanelState(), port=18894, settings=st)
srv.RequestHandlerClass  # handler built with the default existence check: replace it for the test
srv.shutdown()
handler = m.make_handler(m.Journal(Path(TMP) / 'e3.log'), m.PanelState(), settings=st, check_entity=lambda e: e != 'sensor.nope')
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(('127.0.0.1', 18893), handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
def put(body):
    req = urllib.request.Request('http://127.0.0.1:18893/api/settings', data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'X-Zigbee-Monitor': 'settings'})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, None
code, data = put({'coordinator_entities': ['sensor.slzb_temp', 'sensor.slzb_uptime']})
check('entidades guardadas', code == 200 and data['coordinator_entities'] == ['sensor.slzb_temp', 'sensor.slzb_uptime'], data)
check('persisten', m.Settings(Path(TMP) / 's.json').coordinator_entities() == ['sensor.slzb_temp', 'sensor.slzb_uptime'])
code, data = put({'coordinator_entities': ['sensor.nope']})
check('entidad inexistente: 400 con cuál', code == 400 and data == {'unknown': ['sensor.nope']}, data)
code, data = put({'coordinator_entities': ['NO VALIDA']})
check('formato inválido: 400', code == 400)
check('los ajustes de notificación siguen igual', st.snapshot() == {'devices': True, 'general': True, 'system': True})
srv.shutdown()

# ---------- end to end ----------
data = Path(tempfile.mkdtemp())
m.DATA = data; m.OPTIONS = data / 'options.json'; m.DEVICES = data / 'devices.json'
m.EVENTS = data / 'eventos.log'; m.ALERTS = data / 'notificados.json'; m.SETTINGS = data / 'settings.json'
m.DeviceStore.__init__.__defaults__ = (m.DEVICES,)
m.Journal.__init__.__defaults__ = (m.EVENTS,)
m.Alerts.__init__.__defaults__ = (m.ALERTS, None, None, None)
m.Settings.__init__.__defaults__ = (m.SETTINGS,)
m.Traffic.__init__.__defaults__ = (data / 'traffic.json', time.time)
m.Heartbeat.__init__.__defaults__ = (data / 'heartbeat.json', time.time)
m.Incidents.__init__.__defaults__ = (None, data / 'incidents', m.journal_write, time.time, None, None, None, None)
(data / 'heartbeat.json').write_text(json.dumps({'time': time.time() - 3600, 'clean': False}))
m.SETTLE_MQTT = 1
ids = ['0x%016x' % (i + 1) for i in range(6)]
m.OPTIONS.write_text(json.dumps({'mqtt_host': 'broker', 'mqtt_port': 1883, 'mqtt_user': 'u', 'mqtt_password': 'p',
                                 'mqtt_tls': False, 'z2m_base_topic': 'zigbee2mqtt', 'language': 'es',
                                 'mqtt_discovery': True, 'notify_targets': []}))
m.DEVICES.write_text(json.dumps({'watched': ids, 'names': {i: 'D%d' % k for k, i in enumerate(ids)},
                                 'seen': {i: '2026-01-01T00:00:00' for i in ids}, 'initial': ids}))
m.supervisor = lambda path, body=None: {'slug': 'local_zigbee_monitor'} if path == 'addons/self/info' else (_ for _ in ()).throw(RuntimeError(path))
devs = json.dumps([{'type': 'Coordinator', 'ieee_address': '0x00', 'friendly_name': 'C'}] +
                  [{'type': 'Router', 'ieee_address': i, 'friendly_name': 'D%d' % k} for k, i in enumerate(ids)])
av = lambda k, s: ('msg', ('zigbee2mqtt/D%d/availability' % k, json.dumps({'state': s})))
fake.T0 = time.monotonic()
fake.SCRIPT[:] = [(0, 'msg', ('zigbee2mqtt/bridge/state', 'online')), (0, 'msg', ('zigbee2mqtt/bridge/devices', devs))] + \
    [(0, *av(k, 'online')) for k in range(6)] + \
    [(t, 'msg', ('zigbee2mqtt/D0', json.dumps({'state': 'ON'}))) for t in (2, 2.1, 2.2)] + \
    [(2.5, 'msg', ('zigbee2mqtt/bridge/logging', json.dumps({'level': 'error', 'message': 'SRSP - SYS - ping after 6000ms'})))] + \
    [(3, *av(k, 'offline')) for k in range(5)] + [(6, *av(k, 'online')) for k in range(5)]
threading.Thread(target=lambda: (time.sleep(12), os.kill(os.getpid(), signal.SIGTERM)), daemon=True).start()
m.main()
lines = [l.split(' | ', 2)[1:] for l in m.EVENTS.read_text().splitlines()]
for l in lines:
    print('   ', l)
check('parada inesperada anterior en el historial', lines[0][0] == 'SYSTEM' and lines[0][1].startswith('Parada inesperada detectada'), lines[0])
check('incidente registrado en el historial', any(l[1].startswith('Incidente registrado: fallo general') for l in lines), lines)
reports = [json.loads(p.read_text()) for p in (data / 'incidents').glob('*.json')]
check('un informe cerrado', len(reports) == 1 and reports[0]['status'] == 'closed' and reports[0]['outcome'] == 'self', reports)
rep = reports[0]
check('informe: error de Z2M previo y último mensaje', any('SRSP' in e['text'] for e in rep['context'])
      and rep['last_message']['device'] == 'D0', (rep['context'], rep['last_message']))
check('informe: dispositivo más activo antes', rep['traffic']['busiest'] and rep['traffic']['busiest'][0] == {'name': 'D0', 'messages': 3}, rep['traffic'])
check('tráfico contado y guardado al parar', json.loads((data / 'traffic.json').read_text())['hours'][ids[0]], None)
check('parada ordenada registrada', json.loads((data / 'heartbeat.json').read_text())['clean'] is True)

print('\nFAILS:', fails)
sys.exit(1 if fails else 0)
