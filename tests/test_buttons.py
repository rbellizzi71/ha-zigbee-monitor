"""1.2.0: notification buttons, reconnection settle after a general failure, WebSocket listener."""
import base64, hashlib, json, os, queue, signal, socket, struct, sys, tempfile, threading, time, urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp()
os.environ.update(ZM_DATA=TMP, ZM_PANEL_PORT='18999', ZM_SETTLE_Z2M='2')
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

# ---------- where the buttons go ----------
calls = []
def core(method, path, body=None):
    calls.append((method, path, body))
    if path == 'states/notify.telefonos':
        return {}
    if path.startswith('states/'):
        raise urllib.error.HTTPError('u', 404, 'nf', {}, None)
extra = {'tag': 't', 'actions': [{'action': 'X', 'title': 'x'}]}
m.core_api = core
m.send_notification('notify.mobile_app_iphone', 'hola', extra)
m.send_notification('notify.telegram_casa', 'hola', extra)
m.send_notification('notify.telefonos', 'hola', extra)
posts = [c for c in calls if c[0] == 'POST']
check('app de HA: con botones', posts[0][2].get('data') == extra, posts[0])
check('otro servicio clásico: sin botones', 'data' not in posts[1][2], posts[1])
check('entidad notify: sin botones', posts[2][1] == 'services/notify/send_message' and 'data' not in posts[2][2], posts[2])

# ---------- general failure, buttons, settle, stable ----------
def payload(off=()):
    return {'state': 'PROBLEM' if off else 'OK', 'expected': 10, 'offline_names': sorted(off),
            'missing_names': [], 'unknown_names': [], 'data_valid': True}
class Notifier:
    targets = ['notify.mobile_app_x']
    def __init__(self): self.sent = []
    def send(self, msg, extra=None): self.sent.append((clock, msg, extra))
lines = []
def setup(names):
    global lines
    lines = []
    n = Notifier()
    al = m.Alerts(n, set(names), path=Path(tempfile.mkdtemp()) / 'a.json', delay=120)
    al.write = lambda k, msg: lines.append((k, msg))
    tr = m.EventTracker(write=lambda *a: None, settle_mqtt=0, alerts=al)
    tr.mqtt_up(0)
    return n, al, tr
clock = 0
def run(tr, events, start, until):
    global clock
    state = events.get('initial', payload())
    for clock in range(start, until):
        state = events.get(clock, state)
        tr.observe(state, 'online', clock)
        tr.tick(clock)

devs = ['A', 'B', 'C', 'D', 'E', 'OLD']
# OLD was already offline (and notified) before the failure
n, al, tr = setup(devs)
run(tr, {1: payload(['OLD']), 100: payload(['OLD', 'A', 'B', 'C', 'D', 'E']), 200: payload(['OLD', 'B', 'C', 'D', 'E']),
         210: payload(['OLD', 'D', 'E']), 230: payload(['OLD'])}, 0, 500)
first = n.sent[1]
check('falla general al instante, sin nombres', first[0] == 100 and first[1].startswith('Falla general: 5 dispositivos') and 'A' not in first[1].split('.')[0][30:], first[:2])
acts = first[2]['actions']
check('tres botones: reiniciar, esperar, abrir panel',
      [a['title'] for a in acts] == ['Reiniciar Zigbee2MQTT', 'Esperar 10 min', 'Abrir Home Assistant'] and
      acts[2] == {'action': 'URI', 'title': 'Abrir Home Assistant', 'uri': '/lovelace'}, acts)
check('tocar la notificación no fuerza ninguna dirección (evita Safari en iPhone)', 'url' not in first[2] and 'clickAction' not in first[2], first[2])
check('botones con código único y etiqueta', acts[0]['action'].startswith('ZIGBEE_MONITOR_RESTART_') and first[2]['tag'] == m.NOTIFY_TAG, first[2])
check('durante la falla general: nada por dispositivo; red estable al volver al estado anterior, al instante',
      [s[:2] for s in n.sent[2:]] == [(230, 'Red estable: se recuperaron 5 dispositivos · Sigue offline: OLD')], [s[:2] for s in n.sent])
check('el aviso final reemplaza al de falla', n.sent[-1][2] == {'tag': m.NOTIFY_TAG})
check('la falla general se cierra', al.general is None)

# some never come back: stable 120 s after the last reconnection
n, al, tr = setup(devs)
run(tr, {10: payload(['A', 'B', 'C', 'D', 'E']), 200: payload(['B', 'C', 'D', 'E']), 230: payload(['D', 'E'])}, 0, 600)
check('si faltan algunos: red estable 120 s después de la última reconexión',
      [s[:2] for s in n.sent[1:]] == [(350, 'Red estable: se recuperaron 3 dispositivos · Siguen offline: D, E')], [s[:2] for s in n.sent])
# no reconnection at all: no stable message
n, al, tr = setup(devs)
run(tr, {10: payload(['A', 'B', 'C', 'D', 'E'])}, 0, 2000)
check('si no vuelve ninguno: solo el aviso de falla', len(n.sent) == 1 and al.general is not None, [s[:2] for s in n.sent])
# reconnections that never stop: cap of 10 min after the first one
n, al, tr = setup(['R%d' % i for i in range(20)])
ev = {10: payload(['R%d' % i for i in range(20)])}
for i in range(1, 15):
    ev[100 + i * 90] = payload(['R%d' % k for k in range(i, 20)])
run(tr, ev, 0, 2000)
check('reconexiones sin pausa: aviso al tope de 10 min desde la primera', n.sent[1][0] == 190 + 600 and n.sent[1][1].startswith('Red estable'), [s[:2] for s in n.sent[:3]])
check('después, las reconexiones siguientes se avisan normal', n.sent[2][1].startswith('Recuperado: R7'), n.sent[2:3])

# ---------- buttons ----------
n, al, tr = setup(devs)
run(tr, {10: payload(devs[:5])}, 0, 40)
nonce = al.general['nonce']
r1 = al.action('ZIGBEE_MONITOR_RESTART_' + nonce, 50)
r2 = al.action('ZIGBEE_MONITOR_RESTART_' + nonce, 51)
check('reiniciar: pide el reinicio una sola vez', (r1, r2) == ('restart', None), (r1, r2))
check('reiniciar: confirmación al teléfono con la etiqueta', n.sent[-1][1] == m.t('restart_requested') and n.sent[-1][2] == {'tag': m.NOTIFY_TAG}, n.sent[-1])
check('reiniciar: línea en el historial', ('SYSTEM', m.t('action_restart')) in lines, lines)
check('botón de otra notificación: ignorado', al.action('ZIGBEE_MONITOR_RESTART_ffffffff', 60) is None and ('SYSTEM', m.t('action_expired')) in lines)
check('acciones ajenas: ignoradas sin rastro', al.action('OTRA_COSA', 61) is None and len(lines) == 2, lines)
check('botón del panel durante la falla: reinicia', al.action(m.PANEL_RESTART, 62) == 'restart' and ('SYSTEM', m.t('panel_restart')) in lines)
n, al, tr = setup(devs)
check('botón del panel sin falla general: nada', al.action(m.PANEL_RESTART, 5) is None)
run(tr, {10: payload(devs[:5])}, 0, 40)
check('botón vencido (más de 1 h): ignorado', al.action('ZIGBEE_MONITOR_RESTART_' + al.general['nonce'], 10 + m.ACTION_TTL + 1) is None)

# wait: reminder if still down, nothing if recovered
n, al, tr = setup(devs)
run(tr, {10: payload(devs[:5])}, 0, 40)
al.action('ZIGBEE_MONITOR_WAIT_' + al.general['nonce'], 40)
check('esperar: línea en el historial', ('SYSTEM', 'Espera de 10 min pedida desde la notificación') in lines, lines)
run(tr, {'initial': payload(devs[:5])}, 40, 700)
reminder = [s for s in n.sent if s[1].startswith('Siguen sin responder')]
check('esperar: recordatorio a los 10 min con botones nuevos', len(reminder) == 1 and reminder[0][0] == 640 and
      reminder[0][1] == 'Siguen sin responder 5 dispositivos' and
      reminder[0][2]['actions'][0]['action'] == 'ZIGBEE_MONITOR_RESTART_' + al.general['nonce'], reminder)
n, al, tr = setup(devs)
run(tr, {10: payload(devs[:5])}, 0, 40)
al.action('ZIGBEE_MONITOR_WAIT_' + al.general['nonce'], 40)
run(tr, {'initial': payload(devs[:5]), 100: payload()}, 40, 800)
check('esperar: si se recuperaron antes, sin recordatorio', not any(s[1].startswith('Siguen') for s in n.sent) and
      n.sent[-1][1] == 'Red estable: todo en línea', [s[:2] for s in n.sent])

# ---------- WebSocket listener against a fake Home Assistant ----------
def ws_server(port, script, ready):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', port)); srv.listen(1); ready.set()
    conn, _ = srv.accept()
    head = b''
    while b'\r\n\r\n' not in head:
        head += conn.recv(4096)
    key = [l.split(':', 1)[1].strip() for l in head.decode().split('\r\n') if l.lower().startswith('sec-websocket-key')][0]
    accept = base64.b64encode(hashlib.sha1((key + m.WebSocket.GUID).encode()).digest()).decode()
    conn.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                  'Sec-WebSocket-Accept: %s\r\n\r\n' % accept).encode())
    def send(obj, opcode=1):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        hdr = bytes([0x80 | opcode]) + (bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack('>H', len(data)))
        conn.sendall(hdr + data)
    def recv():
        b1, b2 = conn.recv(2)
        n = b2 & 0x7F
        if n == 126: n = struct.unpack('>H', conn.recv(2))[0]
        mask = conn.recv(4); data = b''
        while len(data) < n: data += conn.recv(n - len(data))
        return bytes(b ^ mask[i % 4] for i, b in enumerate(data)), b1 & 0x0F
    script(send, recv)
    time.sleep(0.3); conn.close(); srv.close()

got = {}
def ha(send, recv):
    send({'type': 'auth_required'})
    got['auth'] = json.loads(recv()[0])
    send({'type': 'auth_ok'})
    got['sub'] = json.loads(recv()[0])
    send({'id': 1, 'type': 'result', 'success': True})
    send(b'ping', opcode=9)
    got['pong'] = recv()
    send({'id': 1, 'type': 'event', 'event': {'event_type': 'mobile_app_notification_action', 'data': {'action': 'OTRA'}}})
    big = {'id': 1, 'type': 'event', 'event': {'event_type': 'mobile_app_notification_action',
           'data': {'action': 'ZIGBEE_MONITOR_WAIT_abcd1234', 'padding': 'x' * 300}}}
    send(big)
ready = threading.Event()
threading.Thread(target=ws_server, args=(18777, ha, ready), daemon=True).start()
ready.wait(2)
os.environ['SUPERVISOR_TOKEN'] = 'secreto'
q = queue.Queue()
listener = m.ActionListener(q, connect=lambda: m.WebSocket('127.0.0.1', 18777, '/core/websocket'), retry=0.1)
def once():
    try:
        listener._session()
    except ConnectionError:
        pass
t = threading.Thread(target=once, daemon=True); t.start(); t.join(5)
check('WebSocket: autenticación con el token del Supervisor', got.get('auth') == {'type': 'auth', 'access_token': 'secreto'}, got.get('auth'))
check('WebSocket: suscripción al evento de botones', got.get('sub') == {'id': 1, 'type': 'subscribe_events', 'event_type': 'mobile_app_notification_action'}, got.get('sub'))
check('WebSocket: responde ping con pong', got.get('pong') == (b'ping', 10), got.get('pong'))
check('WebSocket: solo pasa botones del monitor', list(q.queue) == ['ZIGBEE_MONITOR_WAIT_abcd1234'], list(q.queue))

# ---------- panel restart button ----------
import urllib.request
pstate = m.PanelState()
srv = m.start_panel(m.Journal(Path(tempfile.mkdtemp()) / 'e.log'), pstate, port=18998)
def restart_post(header='restart'):
    req = urllib.request.Request('http://127.0.0.1:18998/api/restart', data=b'', method='POST', headers={'X-Zigbee-Monitor': header})
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code
pstate.set({'state': 'OK', 'general_failure': False})
check('panel: reinicio sin falla general → 409', restart_post() == 409 and pstate.actions.empty())
pstate.set({'state': 'PROBLEM', 'general_failure': True})
check('panel: sin cabecera → 403', restart_post('otra') == 403)
check('panel: durante la falla general → pedido al bucle principal', restart_post() == 200 and pstate.actions.get_nowait() == m.PANEL_RESTART)
html = m.render_page()
check('panel: aviso de falla general con botón y confirmación', 'id="general"' in html and 'restart-z2m' in html and 'confirm(T.confirm_restart)' in html)
srv.shutdown()

# ---------- end to end: a tap restarts Zigbee2MQTT over MQTT ----------
m.DATA = Path(tempfile.mkdtemp()); m.OPTIONS = m.DATA / 'options.json'
m.DeviceStore.__init__.__defaults__ = (m.DATA / 'devices.json',)
m.Journal.__init__.__defaults__ = (m.DATA / 'eventos.log',)
m.Alerts.__init__.__defaults__ = (m.DATA / 'notificados.json', None, None)
m.NOTIFY_GROUP = 1
m.OPTIONS.write_text(json.dumps({'mqtt_host': 'broker', 'mqtt_tls': False, 'language': 'es', 'mqtt_discovery': False,
                                 'notify_targets': ['notify.mobile_app_x']}))
ids = ['0x%016x' % i for i in range(1, 6)]
(m.DATA / 'devices.json').write_text(json.dumps({'watched': ids, 'names': {i: 'D%d' % k for k, i in enumerate(ids)},
                                                 'seen': {i: '2026-01-01T00:00:00' for i in ids}, 'initial': ids}))
SENT = []
m.send_notification = lambda target, msg, extra=None: SENT.append((msg, extra))
m.supervisor = lambda path, body=None: {'slug': 'local_zigbee_monitor'}
opened = []
orig_open = m.Alerts._new_nonce
def spy(self, now):
    r = orig_open(self, now); opened.append(self.general['nonce']); return r
m.Alerts._new_nonce = spy
class FakeListener:
    def __init__(self, actions): self.actions = actions
    def start(self):
        def tap():
            while not opened: time.sleep(0.1)
            self.actions.put('ZIGBEE_MONITOR_RESTART_' + opened[0])
        threading.Thread(target=tap, daemon=True).start()
m.ActionListener = FakeListener
devices = json.dumps([{'type': 'Coordinator', 'ieee_address': '0x00', 'friendly_name': 'C'}] +
                     [{'type': 'Router', 'ieee_address': i, 'friendly_name': 'D%d' % k} for k, i in enumerate(ids)])
fake.T0 = time.monotonic()
fake.SCRIPT[:] = [(0, 'msg', ('zigbee2mqtt/bridge/state', 'online')), (0, 'msg', ('zigbee2mqtt/bridge/devices', devices))] + \
    [(0, 'msg', ('zigbee2mqtt/D%d/availability' % k, 'online')) for k in range(5)] + \
    [(2, 'msg', ('zigbee2mqtt/D%d/availability' % k, 'offline')) for k in range(5)]
threading.Thread(target=lambda: (time.sleep(8), os.kill(os.getpid(), signal.SIGTERM)), daemon=True).start()
m.SETTLE_MQTT = 0
m.main()
topics = [t for t, p, r in fake.TOPICS]
check('e2e: el botón publica el reinicio de Zigbee2MQTT', 'zigbee2mqtt/bridge/request/restart' in topics, topics[-5:])
check('e2e: aviso con botón de panel apuntando al add-on', any(e and any(a.get('uri') == '/lovelace'
      for a in e.get('actions', [])) for _, e in SENT), SENT)
check('e2e: confirmación de reinicio enviada', any(msg == m.t('restart_requested') for msg, _ in SENT), [s for s, _ in SENT])

print('\nFAILS:', fails)
sys.exit(1 if fails else 0)
