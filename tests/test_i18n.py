import json, os, sys, tempfile
from pathlib import Path
os.environ['ZM_DATA'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'zigbee_monitor'))
import monitor as m
fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %s' % detail)); fails += not cond
for lang, expected in (('es', 'es'), ('es-419', 'es'), ('ES', 'es'), ('en-GB', 'en'), ('pt-BR', 'en'), ('', 'en')):
    m.core_api = lambda method, path, body=None, lang=lang: {'language': lang}
    check('auto con HA en %r → %s' % (lang, expected), m.resolve_language('auto') == expected)
calls = []
def down(method, path, body=None):
    calls.append(path); raise OSError('core down')
m.core_api = down
check('HA no responde → inglés tras reintentos', m.resolve_language('auto', attempts=3, wait=0) == 'en' and len(calls) == 3, calls)
check('idioma fijo no consulta HA', m.resolve_language('es') == 'es' and len(calls) == 3)
# every key exists in both languages, same placeholders
import re
for key in m.MESSAGES['en']:
    es, en = m.MESSAGES['es'].get(key), m.MESSAGES['en'][key]
    if es is None or set(re.findall(r'\{(\w+)\}', es)) != set(re.findall(r'\{(\w+)\}', en)):
        check('clave %s coherente en es/en' % key, False, (es, en))
check('catálogos completos', set(m.MESSAGES['es']) == set(m.MESSAGES['en']) and set(m.PANEL_TEXTS['es']) == set(m.PANEL_TEXTS['en']))
off = m.discovery_messages(False)
check('descubrimiento desactivado = 4 mensajes vacíos (borra entidades)', len(off) == 4 and all(p == '' for _, p in off), off)
m.LANG = 'es'
on = dict(m.discovery_messages(True, 'local_zigbee_monitor'))
dev = json.loads(on['homeassistant/sensor/zigbee_monitor/status/config'])['device']
check('dispositivo con enlace a la App', dev['configuration_url'] == 'homeassistant://hassio/addon/local_zigbee_monitor/info' and dev['sw_version'] == m.VERSION, dev)
b = json.loads(on['homeassistant/binary_sensor/zigbee_monitor/problem/config'])
check('binary_sensor problem', b['device_class'] == 'problem' and "value_json.state == 'PROBLEM'" in b['value_template'], b)
base = {'mqtt_host': 'h', 'mqtt_port': 1883, 'mqtt_user': '', 'mqtt_password': '', 'mqtt_tls': False, 'devices': []}
for bad in ({'language': 'fr'}, {'mqtt_discovery': 'yes'}):
    Path(os.environ['ZM_DATA'], 'options.json').write_text(json.dumps(dict(base, **bad)))
    m.OPTIONS = Path(os.environ['ZM_DATA'], 'options.json')
    try:
        m.read_options(); check('rechaza %r' % bad, False)
    except ValueError:
        check('rechaza %r' % bad, True)
Path(os.environ['ZM_DATA'], 'options.json').write_text(json.dumps(base))
got = m.read_options()
check('opciones nuevas opcionales (defaults)', {k: got[k] for k in base} == base and got['z2m_base_topic'] == 'zigbee2mqtt', got)
print('\nFALLOS:', fails); sys.exit(1 if fails else 0)
