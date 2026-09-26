"""Fresh install of 4.0.0 against the fake broker: nothing watched → choose from the panel →
a new device appears in Z2M → stop watching one → Z2M restart."""
import json, os, signal, sys, tempfile, threading, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp()
os.environ.update(ZM_DATA=TMP, ZM_SETTLE_Z2M='3', ZM_PANEL_PORT='18699')
sys.path.insert(0, str(HERE / 'fakepaho'))
sys.path.insert(0, str(HERE.parent / 'zigbee_monitor'))
import paho.mqtt.client as fake
import monitor as m
m.SETTLE_MQTT = 1
SENT = []
m.send_notification = lambda target, msg: SENT.append(msg)
(Path(TMP) / 'options.json').write_text(json.dumps({
    'mqtt_host': 'broker', 'mqtt_port': 1883, 'mqtt_user': 'u', 'mqtt_password': 'p', 'mqtt_tls': False,
    'language': 'es', 'mqtt_discovery': True, 'notify_targets': ['notify.x']}))

def devices(*extra):
    rows = [(10, 'luz_a'), (11, 'sensor_b'), (12, 'puerta_c')] + list(extra)
    return json.dumps([{'type': 'Coordinator', 'ieee_address': '0x00', 'friendly_name': 'Coordinator'}] + [
        {'type': 'Router', 'ieee_address': '0x%016x' % n, 'friendly_name': name} for n, name in rows])
av = lambda name, state: ('msg', ('zigbee2mqtt/%s/availability' % name, json.dumps({'state': state})))
bridge = lambda state: ('msg', ('zigbee2mqtt/bridge/state', json.dumps({'state': state})))
fake.SCRIPT[:] = [(0, *bridge('online')), (0, 'msg', ('zigbee2mqtt/bridge/devices', devices())),
                  (0, *av('luz_a', 'online')), (0, *av('sensor_b', 'offline')), (0, *av('puerta_c', 'online')),
                  (6, 'msg', ('zigbee2mqtt/bridge/devices', devices((13, 'enchufe_d')))), (6, *av('enchufe_d', 'online')),
                  (10, *bridge('offline')), (11, *bridge('online')), (11, *av('luz_a', 'offline')), (11.5, *av('luz_a', 'online'))]
BASE = 'http://127.0.0.1:18699'
def post(add=(), remove=()):
    req = urllib.request.Request(BASE + '/api/devices', data=json.dumps({'add': list(add), 'remove': list(remove)}).encode(),
                                 method='POST', headers={'X-Zigbee-Monitor': 'devices'})
    return json.load(urllib.request.urlopen(req))
seen = {}
def user():
    time.sleep(3.5)
    seen['before'] = json.load(urllib.request.urlopen(BASE + '/api/devices'))
    post(add=[d['ieee'] for d in seen['before']['unwatched']])          # "Vigilar todos"
    time.sleep(4)
    seen['after_new'] = json.load(urllib.request.urlopen(BASE + '/api/devices'))
    post(remove=['0x%016x' % 11])                                         # stop watching the offline one
    time.sleep(12)
    os.kill(os.getpid(), signal.SIGTERM)
threading.Thread(target=user, daemon=True).start()
m.logging.basicConfig(level=m.logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
m.main()

lines = (Path(TMP) / 'eventos.log').read_text().splitlines()
print('\n===== eventos.log =====')
for line in lines:
    print(line)
body = [l.split(' | ', 2)[1:] for l in lines]
fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %s' % detail))
    fails += not cond
check('arranque sin vigilados', body[0] == ['SYSTEM', 'Monitor iniciado (v%s): 0 dispositivos vigilados, 1 destino de notificación · MQTT: manual (broker) · Z2M: zigbee2mqtt' % m.VERSION], body[0])
check('estado NO_DATA sin dispositivos', ['NO_DATA', 'Ningún dispositivo seleccionado para vigilar'] in body, body)
check('panel mostró 3 sin vigilar antes de elegir', len(seen['before']['unwatched']) == 3 and seen['before']['watched'] == [], seen.get('before'))
check('vigilar todos → línea de sistema', ['SYSTEM', 'Ahora se vigila: luz_a, puerta_c, sensor_b'] in body, body)
i = [b[1] for b in body].index('Ahora se vigila: luz_a, puerta_c, sensor_b')
check('estado completo justo después', body[i + 1] == ['PROBLEM', 'Offline (1): sensor_b'], body[i + 1:i + 2])
check('dispositivo nuevo en Z2M → una línea', [b for b in body if 'Nuevo en Zigbee2MQTT' in b[1]] == [['SYSTEM', 'Nuevo en Zigbee2MQTT, sin vigilar: enchufe_d']], body)
check('nuevo aparece en "sin vigilar" marcado', [(d['name'], d['new']) for d in seen['after_new']['unwatched']] == [('enchufe_d', True)], seen['after_new'])
j = [b[1] for b in body].index('Se dejó de vigilar: sensor_b')
check('dejar de vigilar el offline → OK sin "Recuperado"', body[j + 1] == ['OK', 'Los 2 dispositivos vigilados están en línea'] and
      not any('Recuperado' in b[1] for b in body), body[j:j + 2])
check('reinicio Z2M con ventana', ['Z2M', 'Reconexión de Zigbee2MQTT completada'] in body)
check('avisos: configuración, offline; nada al quitarlo', SENT == ['Elige en el panel de Zigbee Monitor los dispositivos a vigilar',
      'Nuevo offline: sensor_b · Total: 1 offline, 0 desaparecidos'], SENT)
stored = json.loads((Path(TMP) / 'devices.json').read_text())
check('devices.json final', sorted(stored['watched']) == ['0x%016x' % 10, '0x%016x' % 12] and len(stored['seen']) == 4, stored)
status = [p for _, p in fake.PUBLISHED if isinstance(p, dict) and 'state' in p]
check('sin vigilados nunca publicó OK', all(p['state'] != 'OK' for p in status if p.get('expected') == 0))
print('\nFALLOS:', fails)
sys.exit(1 if fails else 0)
