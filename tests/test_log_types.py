"""1.2.0: RECOVERED history lines and reconnections logged during the Zigbee2MQTT settle."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'zigbee_monitor'))
os.environ.setdefault('ZM_DATA', '/tmp/zm_test_logtypes')
import monitor as m
m.LANG = 'es'
FAILS = []
def check(name, ok, info=''):
    print(('ok   ' if ok else 'FAIL ') + name + ('' if ok else '  -> %r' % (info,)))
    if not ok: FAILS.append(name)

def payload(offline=(), missing=(), unknown=(), expected=10):
    offline, missing, unknown = sorted(offline), sorted(missing), sorted(unknown)
    state = m.PROBLEM if offline or missing else (m.OK if not unknown else m.NO_DATA)
    return {'state': state, 'data_valid': True, 'expected': expected, 'offline_names': offline,
            'missing_names': missing, 'unknown_names': unknown, 'z2m_availability': True}

def tracker(settle_z2m=0):
    log = []
    tr = m.EventTracker(write=lambda k, msg: log.append((k, msg)), settle_mqtt=0, settle_z2m=settle_z2m)
    tr.mqtt_up(0)
    return tr, log

# --- tipos de línea ---
tr, log = tracker()
tr.observe(payload(), 'online', 1)
tr.observe(payload(offline=['A', 'B']), 'online', 2)
check('pérdida -> PROBLEM', log[-1][0] == m.PROBLEM, log[-1])
tr.observe(payload(offline=['B']), 'online', 3)
check('solo recuperación, quedan caídos -> RECOVERED', log[-1][0] == m.RECOVERED and 'Recuperado: A' in log[-1][1], log[-1])
tr.observe(payload(offline=['C']), 'online', 4)
check('recupera uno y pierde otro -> PROBLEM', log[-1][0] == m.PROBLEM, log[-1])
tr.observe(payload(), 'online', 5)
check('todo en línea -> OK', log[-1][0] == m.OK, log[-1])
tr.observe(payload(missing=['D']), 'online', 6)
tr.observe(payload(), 'online', 7)
check('reaparece y todo en línea -> OK', log[-1][0] == m.OK, log[-1])
tr.observe(payload(missing=['D', 'E']), 'online', 8)
tr.observe(payload(missing=['E']), 'online', 9)
check('reaparece uno, sigue otro -> RECOVERED', log[-1][0] == m.RECOVERED, log[-1])

# --- reconexiones durante la estabilización de Zigbee2MQTT ---
tr, log = tracker(settle_z2m=120)
tr.observe(payload(), 'online', 1)
tr.observe(payload(offline=['A', 'B', 'C'], missing=['M']), 'online', 2)
tr.observe(payload(offline=['A', 'B', 'C'], missing=['M']), 'offline', 10)
tr.observe(payload(unknown=['A', 'B', 'C', 'X', 'Y', 'M']), 'online', 20)   # Z2M volvió: todos sin datos
n = len(log)
check('en estabilización, sin datos no escribe nada', len(log) == n and log[-1][0] == m.Z2M, log[-2:])
tr.observe(payload(unknown=['A', 'B', 'C', 'M']), 'online', 25)              # X, Y (nunca caídos) vuelven
check('dispositivos que nunca estuvieron caídos no se registran', len(log) == n, log[n:])
tr.observe(payload(offline=['B', 'C'], unknown=['M']), 'online', 30)          # A vuelve, B y C offline
check('reconexión registrada en su momento', log[-1] == (m.RECOVERED, 'Recuperado: A'), log[n:])
tr.observe(payload(offline=['B', 'C'], unknown=['M']), 'online', 31)
check('sin repetir la misma reconexión', len(log) == n + 1, log[n:])
tr.observe(payload(offline=['C']), 'online', 40)                              # B y M vuelven
check('varias a la vez en una línea', log[-1] == (m.RECOVERED, 'Recuperado: B · Reapareció: M'), log[-1])
tr.observe(payload(offline=['C']), 'online', 150)                             # fin de la estabilización
check('línea final de Z2M completada', (m.Z2M, m.t('z2m_settled')) in log[-2:], log[-3:])
check('estado completo al terminar', log[-1][0] == m.PROBLEM and 'C' in log[-1][1], log[-1])
tr.observe(payload(), 'online', 160)
check('después de la estabilización vuelve el comportamiento normal', log[-1][0] == m.OK, log[-1])

print('\nFAILS: %d' % len(FAILS)); sys.exit(1 if FAILS else 0)
