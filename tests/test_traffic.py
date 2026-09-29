"""1.4.0: Traffic tab. Messages per device (IEEE), per minute for the last hour and per hour for 31 days;
only counts are stored."""
import json, os, sys, tempfile, urllib.request, urllib.error
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(ZM_DATA=TMP)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'zigbee_monitor'))
import monitor as m
m.LANG = 'es'
fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %s' % (detail,)))
    fails += not cond

A, B, C = ('0x%016x' % i for i in (1, 2, 3))
now = [1_800_000_000.0]  # a fixed wall clock
clock = lambda: now[0]
path = Path(TMP) / 'traffic.json'
tr = m.Traffic(path, clock=clock)

for _ in range(30):
    tr.count(A)
for _ in range(5):
    tr.count(B)
counts, coverage, since = tr.totals('hour')
check('última hora: conteo exacto', counts == {A: 30, B: 5}, counts)
now[0] += 2 * 3600
tr.count(B)
counts, _, _ = tr.totals('hour')
check('última hora: lo de hace 2 h ya no cuenta', counts == {B: 1}, counts)
counts, coverage, since = tr.totals('day')
check('día: suma por horas', counts == {A: 30, B: 6}, counts)
check('cobertura: 2 horas de 24', abs(coverage - 2 / 24) < 1e-9, coverage)
check('datos desde la primera hora', since == int(1_800_000_000 // 3600) * 3600, since)
per_minute, busiest = tr.recent(10)
check('reciente: por minuto y más activos', per_minute[-1] == 1 and busiest == [(B, 1)], (per_minute, busiest))

tr.save()
data = json.loads(path.read_text())
check('en disco solo conteos por hora', set(data) == {'hours', 'running'} and data['hours'][A] == {str(int(1_800_000_000 // 3600)): 30}, data)
tr2 = m.Traffic(path, clock=clock)
check('persistencia tras reiniciar', tr2.totals('day')[0] == {A: 30, B: 6}, tr2.totals('day'))
check('los minutos no se guardan (última hora vacía tras reiniciar)', tr2.totals('hour')[0] == {}, tr2.totals('hour'))
now[0] += 32 * 24 * 3600
tr2.count(C)
tr2.save()
counts, _, _ = tr2.totals('month')
check('más de 31 días: se borra', counts == {C: 1} and A not in json.loads(path.read_text())['hours'], counts)
path.write_text('{roto')
check('archivo ilegible: empieza vacío', m.Traffic(path, clock=clock).totals('month')[0] == {})

# periodic save
tr3 = m.Traffic(Path(TMP) / 't3.json', clock=clock)
tr3.count(A); tr3.tick()
check('no guarda en cada mensaje', not (Path(TMP) / 't3.json').exists())
now[0] += m.TRAFFIC_SAVE
tr3.tick()
check('guarda cada pocos minutos', (Path(TMP) / 't3.json').exists())

# ---------- panel view ----------
class Store:
    watched = {A}
class Mon:
    store = Store()
    def name_of(self, ieee):
        return {A: 'Luz A', B: 'Radar B', C: 'Enchufe C'}.get(ieee, ieee)
tv = m.Traffic(Path(TMP) / 'tv.json', clock=clock)
for ieee, n in ((A, 10), (B, 30), (C, 10)):
    for _ in range(n):
        tv.count(ieee)
view = m.traffic_view(tv, Mon(), 'hour')
check('vista: de mayor a menor, empate por nombre', [r['name'] for r in view['rows']] == ['Radar B', 'Enchufe C', 'Luz A'], view['rows'])
check('vista: etiqueta vigilado', {r['name']: r['watched'] for r in view['rows']} == {'Radar B': False, 'Enchufe C': False, 'Luz A': True})
check('vista: %, por minuto, total', view['total'] == 50 and view['rows'][0]['pct'] == 60.0 and view['rows'][0]['per_min'] == 0.5, view)

m.Monitor.name_of  # exists
srv = m.start_panel(m.Journal(Path(TMP) / 'e.log'), m.PanelState(), port=18897, monitor=Mon(), traffic=tv)
with urllib.request.urlopen('http://127.0.0.1:18897/api/traffic?period=hour') as r:
    body = json.loads(r.read())
check('API tráfico', body['total'] == 50 and body['period'] == 'hour', body)
try:
    urllib.request.urlopen('http://127.0.0.1:18897/api/traffic?period=year'); check('periodo inválido: 400', False)
except urllib.error.HTTPError as e:
    check('periodo inválido: 400', e.code == 400)
srv.shutdown()

print('\nFAILS:', fails)
sys.exit(1 if fails else 0)
