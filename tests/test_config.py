"""config.yaml, translations and code agree (options, version, image)."""
import re, sys
from pathlib import Path
import yaml
ROOT = Path(__file__).resolve().parent.parent
ADDON = ROOT / 'zigbee_monitor'
fails = 0
def check(name, cond, detail=''):
    global fails
    print(('OK   ' if cond else 'FAIL ') + name + ('' if cond else '  -> %s' % (detail,)))
    fails += not cond
config = yaml.safe_load((ADDON / 'config.yaml').read_text())
code = (ADDON / 'monitor.py').read_text()
version = re.search(r"^VERSION = '([^']+)'", code, re.M).group(1)
check('config.yaml version == monitor.py VERSION', config['version'] == version, (config['version'], version))
check('every default option is in the schema', set(config['options']) <= set(config['schema']))
for lang in ('en', 'es', 'es-419'):
    names = set(yaml.safe_load((ADDON / 'translations' / ('%s.yaml' % lang)).read_text())['configuration'])
    check('translations/%s.yaml covers the schema' % lang, names == set(config['schema']), names ^ set(config['schema']))
es_text = (ADDON / 'translations' / 'es.yaml').read_text()
check('es-419.yaml is identical to es.yaml', (ADDON / 'translations' / 'es-419.yaml').read_text() == es_text)
check('image uses {arch}', '{arch}' in config.get('image', ''))
check('CHANGELOG mentions the version', ('## ' + version) in (ADDON / 'CHANGELOG.md').read_text())
repo = yaml.safe_load((ROOT / 'repository.yaml').read_text())
check('repository.yaml url == config url', repo['url'] == config['url'])
print('\nFAILS:', fails)
sys.exit(1 if fails else 0)
