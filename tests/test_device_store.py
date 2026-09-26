import json, os, sys, tempfile, time
from pathlib import Path
TMP = tempfile.mkdtemp(); os.environ['ZM_DATA'] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'zigbee_monitor'))
import monitor as m
fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %s' % detail)); fails += not cond
A, B, C, D = ('0x%016x' % i for i in (1, 2, 3, 4))
Z = {A: 'Luz A', B: 'Sensor B', C: 'Puerta C'}
# fresh install: devices already present are not NEW
st = m.DeviceStore(Path(TMP) / 'a.json')
st.observe(dict(Z))
w, u = st.view(Z, lambda i: 'online')
check('instalación nueva: ninguno marcado NUEVO', [x['new'] for x in u] == [False, False, False], u)
time.sleep(1.1)
Z2 = dict(Z, **{D: 'Enchufe D'})
check('uno nuevo después de instalar: línea', st.observe(Z2) == ['Enchufe D'])
w, u = st.view(Z2, lambda i: 'online')
check('solo el nuevo marcado NUEVO', {x['name']: x['new'] for x in u} == {'Luz A': False, 'Sensor B': False, 'Puerta C': False, 'Enchufe D': True}, u)
# unwatched device removed from Z2M: forgotten completely
Z3 = {k: v for k, v in Z2.items() if k != D}
st.observe(Z3)
check('no vigilado eliminado de Z2M: sin rastro', D not in st.seen and D not in st.names)
time.sleep(1.1)
check('si se vuelve a emparejar: NUEVO otra vez', st.observe(Z2) == ['Enchufe D'] and
      {x['name']: x['new'] for x in st.view(Z2, lambda i: 'x')[1]}['Enchufe D'] is True)
# watched device removed from Z2M: kept (still missing)
st.update([A], [], Z2)
Z4 = {k: v for k, v in Z2.items() if k != A}
st.observe(Z4)
check('vigilado eliminado de Z2M: se conserva como desaparecido', A in st.watched and st.names[A] == 'Luz A' and st.expected(Z4)[A] == 'Luz A')
# unwatching the missing one: purged at next snapshot
st.update([], [A], Z4); st.observe(Z4)
check('dejar de vigilarlo: se olvida', A not in st.seen and A not in st.names)
print('\nFALLOS:', fails); sys.exit(1 if fails else 0)
