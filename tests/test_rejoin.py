"""1.2.1: a device that leaves and rejoins, or is renamed, keeps its availability. Zigbee2MQTT
publishes the availability of the new name before the new snapshot; it must not be discarded."""
import json, os, sys, tempfile
from pathlib import Path
TMP = tempfile.mkdtemp(); os.environ['ZM_DATA'] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'zigbee_monitor'))
import monitor as m
m.LANG = 'es'
fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %r' % (detail,))); fails += not cond

A, B, C = ('0x%016x' % i for i in (1, 2, 3))
BASE = 'zigbee2mqtt/'

def monitor(devices):
    store = m.DeviceStore(Path(tempfile.mkdtemp()) / 'devices.json')
    mon = m.Monitor(store)
    mon.receive(BASE + 'bridge/state', b'{"state":"online"}')
    snap(mon, devices)
    store.update(list(devices), [], dict(mon.actual))
    for name in devices.values():
        av(mon, name, 'online')
    return mon

def snap(mon, devices):
    rows = [{'type': 'Coordinator', 'ieee_address': '0x00', 'friendly_name': 'Coordinator'}]
    rows += [{'type': 'EndDevice', 'ieee_address': i, 'friendly_name': n} for i, n in devices.items()]
    return mon.receive(BASE + 'bridge/devices', json.dumps(rows).encode())

def av(mon, name, state):
    mon.receive(BASE + name + '/availability', json.dumps({'state': state}).encode() if state else b'')

# ---------- Leaves the network and rejoins with the same name ----------
mon = monitor({A: 'Puerta de la Sala', B: 'Luz'})
check('al inicio: online', mon.status_of(A) == 'online' and mon.report()['state'] == 'OK', mon.report())
snap(mon, {B: 'Luz'})
check('sale de la red: desaparecido', mon.status_of(A) == 'missing' and mon.report()['missing_names'] == ['Puerta de la Sala'])
av(mon, 'Puerta de la Sala', 'online')   # Z2M: availability first...
snap(mon, {A: 'Puerta de la Sala', B: 'Luz'})  # ...then the snapshot
r = mon.report()
check('vuelve: online, no sin datos', mon.status_of(A) == 'online' and r['state'] == 'OK' and not r['unknown_names'], r)

# ---------- Removed (force), rejoins with a temporary name, renamed back ----------
mon = monitor({A: 'Puerta de Prueba', B: 'Luz'})
av(mon, 'Puerta de Prueba', None)        # Z2M clears the retained topic on removal
snap(mon, {B: 'Luz'})
check('eliminado: desaparecido', mon.status_of(A) == 'missing')
av(mon, A, 'online')                     # rejoins as 0x...
snap(mon, {A: A, B: 'Luz'})
check('vuelve con nombre temporal: online', mon.status_of(A) == 'online' and mon.expected[A] == A, mon.report())
av(mon, A, None)                         # rename: Z2M clears the old topic...
av(mon, 'Puerta de Prueba', 'online')    # ...publishes the new one...
snap(mon, {A: 'Puerta de Prueba', B: 'Luz'})  # ...then the snapshot
r = mon.report()
check('renombrado al nombre viejo: online', mon.status_of(A) == 'online' and r['state'] == 'OK' and not r['unknown_names'], r)
check('con el nombre viejo', mon.expected[A] == 'Puerta de Prueba')

# ---------- Normal rename, in both orders ----------
mon = monitor({A: 'Sensor', B: 'Luz'})
av(mon, 'Sensor nuevo', 'online')
snap(mon, {A: 'Sensor nuevo', B: 'Luz'})
check('renombrado (disponibilidad antes): online', mon.status_of(A) == 'online')
snap(mon, {A: 'Sensor 3', B: 'Luz'})
check('renombrado (disponibilidad después): sin datos mientras tanto', mon.status_of(A) == 'unknown')
av(mon, 'Sensor 3', 'online')
check('renombrado (disponibilidad después): online', mon.status_of(A) == 'online')

# ---------- A problem that arrives with the new name is not lost ----------
mon = monitor({A: 'Sensor', B: 'Luz'})
av(mon, 'Sensor nuevo', 'offline')
snap(mon, {A: 'Sensor nuevo', B: 'Luz'})
r = mon.report()
check('offline antes de la instantánea: problema', r['state'] == 'PROBLEM' and r['offline_names'] == ['Sensor nuevo'], r)

# ---------- The old name does not keep a stale value ----------
mon = monitor({A: 'X', B: 'Luz'})
av(mon, 'X', 'offline')
snap(mon, {A: 'Y', B: 'Luz'})
check('nombre viejo olvidado', 'X' not in mon.availability, mon.availability)
snap(mon, {A: 'Y', B: 'Luz', C: 'X'})    # another device takes the old name
check('otro dispositivo con el nombre viejo: sin el dato antiguo', mon.status_of(C) == 'unknown')
mon = monitor({A: 'Puerta', B: 'Luz'})
av(mon, 'Puerta', 'offline')
snap(mon, {B: 'Luz'})
av(mon, 'Puerta', 'online')
snap(mon, {A: 'Puerta', B: 'Luz'})
check('eliminado y vuelto: no conserva el offline antiguo', mon.status_of(A) == 'online')

# ---------- Names swapped between two devices ----------
mon = monitor({A: 'Uno', B: 'Dos'})
av(mon, 'Uno', 'online'); av(mon, 'Dos', 'offline')
snap(mon, {A: 'Dos', B: 'Uno'})
av(mon, 'Dos', 'online'); av(mon, 'Uno', 'offline')
check('nombres intercambiados: siguen a Z2M', mon.status_of(A) == 'online' and mon.status_of(B) == 'offline')

print('\nFAILS:', fails)
sys.exit(1 if fails else 0)
