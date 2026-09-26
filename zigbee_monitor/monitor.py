"""Zigbee Monitor. Supervisor owns options; this process never writes options.json."""
import collections
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import logging.handlers
import os
from pathlib import Path
import queue
import re
import signal
import ssl
import threading
import time
import urllib.error
import urllib.request

VERSION = '1.0.2'
DATA = Path(os.environ.get('ZM_DATA', '/data'))
OPTIONS = DATA / 'options.json'
DEVICES = DATA / 'devices.json'
EVENTS = DATA / 'eventos.log'
ALERTS = DATA / 'notificados.json'
ALERT_DELAY = int(os.environ.get('ZM_ALERT_DELAY', '120'))  # Z2M offline / MQTT lost must last this long
NOTIFY_TITLE = 'Zigbee Monitor'
NEW_DEVICE_HOURS = 24      # the panel marks unwatched devices first seen this recently as NEW
EVENTS_MAX_BYTES = 1_000_000
EVENTS_BACKUPS = 2
RECENT_EVENTS = 5          # recent_events published in the MQTT status
PANEL_EVENTS = 500         # lines shown in the web panel
SETTLE_MQTT = 10           # seconds: retained messages arrive after (re)connecting
SETTLE_Z2M = int(os.environ.get('ZM_SETTLE_Z2M', '120'))  # seconds after Z2M returns online
PANEL_PORT = int(os.environ.get('ZM_PANEL_PORT', '8099'))
INGRESS_PEERS = ('172.30.32.2', '127.0.0.1')
DEFAULT_BASE = 'zigbee2mqtt'  # Zigbee2MQTT base_topic (option z2m_base_topic)
STATUS = 'zigbee_monitor/status'
AVAILABILITY = 'zigbee_monitor/availability'
DISCOVERY_PREFIX = 'homeassistant'
LANGUAGES = ('es', 'en')
LOG = logging.getLogger('zigbee_monitor')
JOURNAL = None  # Journal instance, created in main()

# Event types stored in eventos.log (stable codes; the panel shows translated labels).
OK, PROBLEM, NO_DATA = 'OK', 'PROBLEM', 'NO_DATA'
Z2M, MQTT, NOTIFY, SYSTEM = 'Z2M', 'MQTT', 'NOTIFY', 'SYSTEM'
BASE_TOPIC_RE = re.compile(r'[^\s+#/]+(/[^\s+#/]+)*')


def devices(value):
    if not isinstance(value, list):
        raise ValueError('devices must be a list')
    result = {}
    names = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('Invalid device entry')
        ieee, name = item.get('ieee'), item.get('name')
        if not isinstance(ieee, str) or not re.fullmatch(r'0x[0-9a-fA-F]{16}', ieee):
            raise ValueError('Invalid IEEE: 0x followed by 16 hex digits required')
        if not isinstance(name, str) or not name.strip() or any(c in name for c in '\x00+#'):
            raise ValueError('Empty name or not valid for an MQTT topic')
        ieee = ieee.lower()
        if ieee in result or name in names:
            raise ValueError('Duplicated IEEE or name')
        result[ieee] = name
        names.add(name)
    return result


def read_options():
    data = json.loads(OPTIONS.read_text())
    if not isinstance(data, dict):
        raise ValueError('Invalid configuration')
    # MQTT fields are optional: an empty host means "use the broker of Home Assistant".
    data.setdefault('mqtt_host', '')
    data.setdefault('mqtt_port', 1883)
    data.setdefault('mqtt_user', '')
    data.setdefault('mqtt_password', '')
    data.setdefault('mqtt_tls', False)
    data.setdefault('z2m_base_topic', DEFAULT_BASE)
    for key in ('mqtt_host', 'mqtt_user', 'mqtt_password'):
        if not isinstance(data[key], str):
            raise ValueError('MQTT host and credentials must be strings')
    if type(data['mqtt_port']) is not int or not 1 <= data['mqtt_port'] <= 65535:
        raise ValueError('Invalid mqtt_port')
    if data['mqtt_host'].strip() and data['mqtt_password'] and not data['mqtt_user']:
        raise ValueError('MQTT password without user')
    if type(data['mqtt_tls']) is not bool:
        raise ValueError('mqtt_tls must be a boolean')
    if not isinstance(data['z2m_base_topic'], str) or not BASE_TOPIC_RE.fullmatch(data['z2m_base_topic']):
        raise ValueError('Invalid z2m_base_topic: no spaces, + or #, and no / at the start or end')
    if data.get('language', 'auto') not in ('auto',) + LANGUAGES:
        raise ValueError('Invalid language')
    if type(data.get('mqtt_discovery', True)) is not bool:
        raise ValueError('mqtt_discovery must be a boolean')
    notify_targets(data)
    return data


def notify_targets(options):
    """Validated notification targets (notify.<name>), duplicates removed, order kept."""
    targets = options.get('notify_targets', [])
    if not isinstance(targets, list):
        raise ValueError('notify_targets must be a list')
    for target in targets:
        if not isinstance(target, str) or not re.fullmatch(r'notify\.[a-z0-9_]+', target):
            raise ValueError('Invalid notification target: must be notify.<name>')
    return list(dict.fromkeys(targets))


def snapshot(payload):
    raw = json.loads(payload)
    if not isinstance(raw, list):
        raise ValueError('bridge/devices is not a list')
    rows = []
    coordinators = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('Invalid Z2M device')
        if item.get('type') == 'Coordinator':
            coordinators += 1
            continue
        if item.get('type') not in ('Router', 'EndDevice'):
            raise ValueError('Unknown Z2M device type')
        rows.append({'ieee': item.get('ieee_address'), 'name': item.get('friendly_name')})
    if coordinators != 1:
        raise ValueError('The snapshot must contain exactly one coordinator')
    return devices(rows)


def state(payload):
    text = payload.decode('utf-8').strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = text
    value = obj.get('state') if isinstance(obj, dict) else obj
    return value if value in ('online', 'offline') else None


def _open(request):
    # Disable environment proxies: never send the Supervisor token to a proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15)


def supervisor(path, body=None):
    """Supervisor API (e.g. 'addons/self/info', 'services/mqtt')."""
    token = os.environ.get('SUPERVISOR_TOKEN')
    if not token:
        raise RuntimeError('SUPERVISOR_TOKEN missing')
    request = urllib.request.Request(
        'http://supervisor/' + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
        method='GET' if body is None else 'POST')
    with _open(request) as response:
        data = json.load(response)
    if data.get('result') != 'ok':
        raise RuntimeError('Supervisor did not confirm the operation')
    return data.get('data', {})


def core_api(method, path, body=None):
    """Home Assistant Core REST API through the Supervisor proxy (needs homeassistant_api)."""
    token = os.environ.get('SUPERVISOR_TOKEN')
    if not token:
        raise RuntimeError('SUPERVISOR_TOKEN missing')
    request = urllib.request.Request(
        'http://supervisor/core/api/' + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
        method=method)
    with _open(request) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


class NoBroker(Exception):
    """No MQTT broker configured and none offered by Home Assistant."""


def mqtt_settings(options, service=None):
    """Where to connect. Empty mqtt_host = automatic: the broker Home Assistant offers to
    add-ons (the Mosquitto add-on), with credentials created for this add-on."""
    if options['mqtt_host'].strip():
        return {'mode': 'manual', 'host': options['mqtt_host'].strip(), 'port': options['mqtt_port'],
                'user': options['mqtt_user'], 'password': options['mqtt_password'], 'tls': options['mqtt_tls']}
    try:
        data = (service or (lambda: supervisor('services/mqtt')))()
    except Exception:
        raise NoBroker() from None
    if not isinstance(data, dict) or not isinstance(data.get('host'), str) or not data['host']:
        raise NoBroker()
    port = data.get('port', 1883)
    return {'mode': 'auto', 'host': data['host'], 'port': port if type(port) is int else 1883,
            'user': str(data.get('username') or ''), 'password': str(data.get('password') or ''),
            'tls': data.get('ssl') is True}


IEEE_RE = re.compile(r'0x[0-9a-f]{16}')


class DeviceStore:
    """Which Zigbee2MQTT devices are watched. Lives in /data/devices.json (managed from the panel).

    Only IEEE addresses decide; names follow Zigbee2MQTT (the last known one is kept for
    devices that disappear). 'seen' remembers when each device was first seen in Z2M, to show
    new devices and to write a single history line when one appears.
    """

    def __init__(self, path=DEVICES):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.version = 0
        data = self._load()
        self.exists = data is not None
        data = data or {}
        self.watched = {ieee for ieee in data.get('watched', []) if isinstance(ieee, str) and IEEE_RE.fullmatch(ieee)}
        self.names = {k: v for k, v in (data.get('names') or {}).items() if isinstance(v, str)}
        self.seen = {k: v for k, v in (data.get('seen') or {}).items() if isinstance(v, str)}
        # Devices present when the monitor first saw Zigbee2MQTT: they already existed,
        # so they are never shown as NEW.
        self.initial = {ieee for ieee in data.get('initial') or [] if isinstance(ieee, str)}

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            LOG.warning('devices.json unreadable; starting with no watched devices')
            return {}

    def _save(self):
        data = {'watched': sorted(self.watched), 'names': self.names, 'seen': self.seen, 'initial': sorted(self.initial)}
        temporary = self.path.with_suffix('.tmp')
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        self.exists = True
        self.version += 1

    def expected(self, actual=None):
        """{ieee: current name} of watched devices (Z2M name if present, else last known)."""
        with self.lock:
            return {ieee: (actual or {}).get(ieee) or self.names.get(ieee) or ieee for ieee in self.watched}

    def observe(self, actual):
        """New Z2M snapshot. Returns names of devices never seen before and not watched.
        The very first snapshot of a fresh store is recorded silently."""
        with self.lock:
            first = not self.seen
            now = time.strftime('%Y-%m-%dT%H:%M:%S')
            new = [ieee for ieee in actual if ieee not in self.seen]
            changed = bool(new) or first
            for ieee in new:
                self.seen[ieee] = now
                if first:
                    self.initial.add(ieee)
            for ieee, name in actual.items():
                if self.names.get(ieee) != name:
                    self.names[ieee] = name
                    changed = True
            # Forget devices that are neither in Zigbee2MQTT nor watched: nothing is kept of
            # them, so if one is paired again it shows up as NEW.
            for ieee in [i for i in set(self.seen) | set(self.names) if i not in actual and i not in self.watched]:
                self.seen.pop(ieee, None)
                self.names.pop(ieee, None)
                self.initial.discard(ieee)
                changed = True
            if changed:
                self._save()
            if first:
                return []
            return sorted(actual[ieee] for ieee in new if ieee not in self.watched)

    def update(self, add, remove, actual):
        """Apply a change from the panel. add: IEEEs present in Z2M; remove: watched IEEEs.
        Returns (added names, removed names)."""
        with self.lock:
            add = [ieee for ieee in add if ieee in actual and ieee not in self.watched]
            remove = [ieee for ieee in remove if ieee in self.watched]
            if not add and not remove:
                return [], []
            added = sorted(actual[ieee] for ieee in add)
            removed = sorted((actual.get(ieee) or self.names.get(ieee) or ieee) for ieee in remove)
            self.watched.update(add)
            self.watched.difference_update(remove)
            for ieee in add:
                self.names[ieee] = actual[ieee]
            self._save()
            return added, removed

    def view(self, actual, status_of):
        """Panel data: watched with status, and unwatched Z2M devices with first-seen date."""
        with self.lock:
            now = time.time()
            watched = [{'ieee': ieee, 'name': (actual or {}).get(ieee) or self.names.get(ieee) or ieee,
                        'status': status_of(ieee)} for ieee in self.watched]
            unwatched = []
            for ieee, name in (actual or {}).items():
                if ieee in self.watched:
                    continue
                first = self.seen.get(ieee, '')
                try:
                    recent = (ieee not in self.initial and
                              now - time.mktime(time.strptime(first, '%Y-%m-%dT%H:%M:%S')) < NEW_DEVICE_HOURS * 3600)
                except ValueError:
                    recent = False
                unwatched.append({'ieee': ieee, 'name': name, 'first_seen': first, 'new': recent})
        key = lambda item: item['name'].lower()
        return sorted(watched, key=key), sorted(unwatched, key=key)


def availability_enabled(config):
    """Whether Zigbee2MQTT reports device availability, from the 'config' of bridge/info.
    True / False, or None when it cannot be told. 2.x: availability.enabled; 1.x: availability
    true or an object, or advanced.availability_timeout. Enabling it for a single device also
    counts (those devices report; the rest stay without data)."""
    if not isinstance(config, dict):
        return None
    value = config.get('availability')
    if isinstance(value, bool):
        enabled = value
    elif isinstance(value, dict):
        enabled = value.get('enabled', True) is not False
    else:
        enabled = False
    advanced = config.get('advanced')
    if isinstance(advanced, dict) and (advanced.get('availability_timeout') or 0) > 0:
        enabled = True
    devices = config.get('devices')
    if isinstance(devices, dict) and any(isinstance(d, dict) and d.get('availability') not in (None, False)
                                         for d in devices.values()):
        enabled = True
    return enabled


class Monitor:
    """Live view of Zigbee2MQTT (snapshot, bridge state, availability). The watched list comes
    from the DeviceStore. The HTTP thread reads it for the panel, hence the lock."""

    def __init__(self, store, base=DEFAULT_BASE):
        self.store = store
        self.base = base
        self.lock = threading.RLock()
        self.actual = None
        self.availability = {}
        self.availability_on = None  # from bridge/info: True, False or None (unknown)
        self.bridge = None
        self.invalid = False

    @property
    def expected(self):
        with self.lock:
            return self.store.expected(self.actual)

    def reset(self):
        with self.lock:
            self.actual = None
            self.availability.clear()
            self.availability_on = None
            self.bridge = None
            self.invalid = False

    def wants(self, topic):
        """Topics this monitor reads (everything else under the base topic is ignored)."""
        base = self.base + '/'
        return topic in (base + 'bridge/devices', base + 'bridge/state', base + 'bridge/info') or (
            topic.startswith(base) and topic.endswith('/availability'))

    def receive(self, topic, payload):
        """Returns True when a new valid device snapshot arrived."""
        base = self.base + '/'
        with self.lock:
            if topic == base + 'bridge/devices':
                try:
                    new = snapshot(payload)
                    # Invalidate old names on rename/reassignment.
                    if self.actual is not None:
                        for ieee, name in new.items():
                            if self.actual.get(ieee) != name:
                                self.availability.pop(name, None)
                    self.actual = new
                    self.invalid = False
                    return True
                except (ValueError, TypeError, UnicodeError):
                    self.invalid = True
                    LOG.warning(t('log_invalid_snapshot'))
            elif topic == base + 'bridge/state':
                new = state(payload)
                if new != 'online':
                    self.availability.clear()
                self.bridge = new
            elif topic == base + 'bridge/info':
                info = json.loads(payload)
                if isinstance(info, dict):
                    self.availability_on = availability_enabled(info.get('config'))
            elif topic.startswith(base) and topic.endswith('/availability'):
                name = topic[len(base):-len('/availability')]
                self.availability[name] = state(payload)
            return False

    @property
    def valid(self):
        return self.actual is not None and not self.invalid and self.bridge == 'online'

    def _available(self, name):
        # With availability disabled in Z2M, old retained messages may still say 'online': ignore them.
        return None if self.availability_on is False else self.availability.get(name)

    def status_of(self, ieee):
        """online / offline / missing / unknown for the panel."""
        with self.lock:
            if not self.valid:
                return 'unknown'
            if ieee not in self.actual:
                return 'missing'
            return self._available(self.actual[ieee]) or 'unknown'

    def devices_view(self):
        with self.lock:
            actual = dict(self.actual) if self.valid else None
            watched, unwatched = self.store.view(actual, self.status_of)
            return {'z2m_valid': actual is not None, 'z2m_state': self.bridge or 'unknown',
                    'watched': watched, 'unwatched': unwatched}

    def report(self):
        with self.lock:
            valid = self.valid
            expected = self.expected
            missing, offline, unknown = [], [], []
            if valid:
                for ieee, label in expected.items():
                    if ieee not in self.actual:
                        missing.append(label)
                    else:
                        availability = self._available(self.actual[ieee])
                        if availability == 'offline':
                            offline.append(label)
                        elif availability != 'online':
                            unknown.append(label)
            else:
                unknown = list(expected.values())
            report = {
                'state': PROBLEM if missing or offline else (OK if valid and not unknown else NO_DATA),
                'expected': len(expected),
                'registered': len(self.actual) if valid else None,
                'offline': len(offline) if valid else None,
                'missing': len(missing) if valid else None,
                'offline_names': sorted(offline),
                'missing_names': sorted(missing),
                'unknown': len(unknown), 'unknown_names': sorted(unknown),
                'data_valid': valid, 'z2m_state': self.bridge or 'unknown',
                'z2m_availability': self.availability_on,
                'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }
            if not expected:
                # Nothing chosen yet: never report OK.
                report.update(state=NO_DATA, reason='no_devices')
            elif self.availability_on is False:
                report['reason'] = 'z2m_availability_disabled'
            return report


class ChangeLogger:
    """stdout: log each change of the published status once (ignores timestamps and history)."""
    def __init__(self):
        self.previous = None

    def report(self, payload):
        signature = {key: value for key, value in payload.items()
                     if key not in ('updated', 'recent_events')}
        signature = json.dumps(signature, sort_keys=True, ensure_ascii=False)
        if signature == self.previous:
            return
        LOG.info(t('log_summary', expected=payload['expected'], registered=payload['registered'],
                   offline=payload['offline'], missing=payload['missing'], state=payload['state']))
        for key, label in (('offline_names', 'label_offline'), ('missing_names', 'label_missing')):
            if payload.get(key):
                LOG.info('%s: %s', t(label), json.dumps(payload[key], ensure_ascii=False))
        self.previous = signature


MESSAGES = {
    'es': {
        # history / stdout
        'started_one': 'Monitor iniciado (v{version}): {n} dispositivos vigilados, {t} destino de notificación · MQTT: {mqtt} · Z2M: {base}',
        'started_other': 'Monitor iniciado (v{version}): {n} dispositivos vigilados, {t} destinos de notificación · MQTT: {mqtt} · Z2M: {base}',
        'mqtt_auto': 'automático',
        'mqtt_manual': 'manual ({host})',
        'no_broker': 'No se encontró un broker MQTT en Home Assistant. Instala el add-on Mosquitto o completa los datos del broker en la configuración',
        'availability_off': 'La disponibilidad (availability) está desactivada en Zigbee2MQTT: no se puede saber si un dispositivo está offline',
        'availability_on': 'La disponibilidad (availability) de Zigbee2MQTT está activada',
        'alert_availability': 'Activa la disponibilidad (availability) en Zigbee2MQTT para que Zigbee Monitor pueda detectar dispositivos offline',
        'stopped': 'Monitor detenido',
        'stopped_error': 'Monitor detenido por error ({error})',
        'history_cleared': 'Historial borrado por el usuario',
        'no_devices': 'Ningún dispositivo seleccionado para vigilar',
        'watch_now': 'Ahora se vigila: {names}',
        'watch_stopped': 'Se dejó de vigilar: {names}',
        'new_unwatched': 'Nuevo en Zigbee2MQTT, sin vigilar: {names}',
        'alert_setup': 'Elige en el panel de Zigbee Monitor los dispositivos a vigilar',
        'mqtt_connected': 'Conectado al broker MQTT',
        'mqtt_recovered': 'Conexión con el broker recuperada',
        'mqtt_lost': 'Conexión con el broker perdida',
        'mqtt_failed': 'No se pudo conectar al broker MQTT',
        'z2m_offline': 'Zigbee2MQTT está offline',
        'z2m_back': 'Zigbee2MQTT volvió a estar online; esperando {s} s a que la red se estabilice',
        'z2m_settled': 'Reconexión de Zigbee2MQTT completada',
        'no_valid_data': 'Sin datos válidos de Zigbee2MQTT',
        'all_expected_ok_one': 'El único dispositivo vigilado está en línea',
        'all_expected_ok_other': 'Los {n} dispositivos vigilados están en línea',
        'all_online': 'Todos los dispositivos en línea',
        'total': 'Total: {offline} offline, {missing} desaparecidos',
        'and_more': ' y {n} más',
        'label_offline': 'Offline',
        'label_missing': 'Desaparecidos',
        'label_unknown': 'Sin datos',
        'new_offline': 'Nuevo offline',
        'recovered': 'Recuperado',
        'new_missing': 'Desaparecido',
        'reappeared': 'Reapareció',
        'new_unknown': 'Sin datos',
        'known_again': 'Con datos',
        'notify_sent_one': 'Enviado a {n} destino: {message}',
        'notify_sent_other': 'Enviado a {n} destinos: {message}',
        'notify_failed': 'Falló el envío a: {targets}',
        'alert_mqtt_down': 'El monitor no tiene conexión con el broker MQTT desde hace más de {m} minutos',
        'alert_mqtt_back': 'El monitor recuperó la conexión con el broker MQTT',
        'alert_z2m_down': 'Zigbee2MQTT está offline desde hace más de {m} minutos',
        'alert_z2m_back': 'Zigbee2MQTT volvió a estar online',
        # stdout only
        'log_start': 'Zigbee Monitor {version}; vigilados: {n}; idioma: {language}',
        'log_summary': 'Vigilados: {expected} | Registrados: {registered} | Offline: {offline} | Desaparecidos: {missing} | {state}',
        'log_mqtt_connected': 'MQTT conectado; esperando datos de Zigbee2MQTT',
        'log_mqtt_disconnected': 'MQTT desconectado',
        'log_mqtt_retry': 'No se pudo conectar a MQTT; reintento en 5 segundos',
        'log_mqtt_refused': 'El broker rechazó la conexión MQTT',
        'log_mqtt_acl': 'El broker rechazó una suscripción; revisar ACL MQTT',
        'log_invalid_message': 'Mensaje MQTT inválido ignorado',
        'log_invalid_snapshot': 'Instantánea de Z2M inválida; no se usa para declarar desaparecidos',
        'log_publish_failed': 'No se pudo encolar el estado MQTT',
        'log_history_cleared': 'Historial de eventos borrado por el usuario desde el panel',
        'log_history_write_failed': 'No se pudo escribir en eventos.log',
        'log_alerts_save_failed': 'No se pudo guardar el estado de notificaciones',
        'log_notify_failed': 'No se pudo enviar la notificación a: {targets}',
        'log_panel_failed': 'No se pudo iniciar el panel web en el puerto {port}; el monitoreo continúa',
        'log_fatal': 'Monitor detenido ({error}). Revisar configuración y estado de inicialización; no se importará por error.',
        # MQTT discovery entity names
        'entity_state': 'Estado',
        'entity_problem': 'Problema',
        'entity_offline': 'Offline',
        'entity_missing': 'Desaparecidos',
    },
    'en': {
        'started_one': 'Monitor started (v{version}): {n} watched devices, {t} notification target · MQTT: {mqtt} · Z2M: {base}',
        'started_other': 'Monitor started (v{version}): {n} watched devices, {t} notification targets · MQTT: {mqtt} · Z2M: {base}',
        'mqtt_auto': 'automatic',
        'mqtt_manual': 'manual ({host})',
        'no_broker': 'No MQTT broker found in Home Assistant. Install the Mosquitto add-on or fill in the broker settings in the configuration',
        'availability_off': 'Availability is disabled in Zigbee2MQTT: offline devices cannot be detected',
        'availability_on': 'Zigbee2MQTT availability is enabled',
        'alert_availability': 'Enable availability in Zigbee2MQTT so Zigbee Monitor can detect offline devices',
        'stopped': 'Monitor stopped',
        'stopped_error': 'Monitor stopped by an error ({error})',
        'history_cleared': 'History cleared by the user',
        'no_devices': 'No device selected to watch',
        'watch_now': 'Now watched: {names}',
        'watch_stopped': 'No longer watched: {names}',
        'new_unwatched': 'New in Zigbee2MQTT, not watched: {names}',
        'alert_setup': 'Choose the devices to watch in the Zigbee Monitor panel',
        'mqtt_connected': 'Connected to the MQTT broker',
        'mqtt_recovered': 'Connection to the broker restored',
        'mqtt_lost': 'Connection to the broker lost',
        'mqtt_failed': 'Could not connect to the MQTT broker',
        'z2m_offline': 'Zigbee2MQTT is offline',
        'z2m_back': 'Zigbee2MQTT is back online; waiting {s} s for the network to settle',
        'z2m_settled': 'Zigbee2MQTT reconnection completed',
        'no_valid_data': 'No valid data from Zigbee2MQTT',
        'all_expected_ok_one': 'The only watched device is online',
        'all_expected_ok_other': 'All {n} watched devices are online',
        'all_online': 'All devices online',
        'total': 'Total: {offline} offline, {missing} missing',
        'and_more': ' and {n} more',
        'label_offline': 'Offline',
        'label_missing': 'Missing',
        'label_unknown': 'No data',
        'new_offline': 'New offline',
        'recovered': 'Recovered',
        'new_missing': 'Missing',
        'reappeared': 'Reappeared',
        'new_unknown': 'No data',
        'known_again': 'Data again',
        'notify_sent_one': 'Sent to {n} target: {message}',
        'notify_sent_other': 'Sent to {n} targets: {message}',
        'notify_failed': 'Delivery failed to: {targets}',
        'alert_mqtt_down': 'The monitor has had no connection to the MQTT broker for more than {m} minutes',
        'alert_mqtt_back': 'The monitor restored its connection to the MQTT broker',
        'alert_z2m_down': 'Zigbee2MQTT has been offline for more than {m} minutes',
        'alert_z2m_back': 'Zigbee2MQTT is back online',
        'log_start': 'Zigbee Monitor {version}; watched: {n}; language: {language}',
        'log_summary': 'Watched: {expected} | Registered: {registered} | Offline: {offline} | Missing: {missing} | {state}',
        'log_mqtt_connected': 'MQTT connected; waiting for Zigbee2MQTT data',
        'log_mqtt_disconnected': 'MQTT disconnected',
        'log_mqtt_retry': 'Could not connect to MQTT; retrying in 5 seconds',
        'log_mqtt_refused': 'The broker refused the MQTT connection',
        'log_mqtt_acl': 'The broker refused a subscription; check the MQTT ACL',
        'log_invalid_message': 'Invalid MQTT message ignored',
        'log_invalid_snapshot': 'Invalid Z2M snapshot; not used to declare missing devices',
        'log_publish_failed': 'Could not queue the MQTT status',
        'log_history_cleared': 'Event history cleared by the user from the panel',
        'log_history_write_failed': 'Could not write to eventos.log',
        'log_alerts_save_failed': 'Could not save the notification state',
        'log_notify_failed': 'Could not send the notification to: {targets}',
        'log_panel_failed': 'Could not start the web panel on port {port}; monitoring continues',
        'log_fatal': 'Monitor stopped ({error}). Check the configuration and initialization state; nothing will be imported by mistake.',
        'entity_state': 'State',
        'entity_problem': 'Problem',
        'entity_offline': 'Offline',
        'entity_missing': 'Missing',
    },
}

PANEL_TEXTS = {
    'es': {
        'loading': 'Cargando…', 'no_data_yet': 'Sin datos todavía',
        'registered': 'registrados', 'stat_watched': 'vigilados', 'stat_unwatched': 'no vigilados',
        'offline': 'offline', 'missing': 'desaparecidos',
        'list_offline': 'Offline', 'list_missing': 'Desaparecidos', 'list_unknown': 'Sin datos',
        'last_publish': 'Última publicación', 'unknown': 'desconocido',
        'all': 'Todos', 'search': 'Buscar dispositivo o texto', 'clear': 'Borrar historial',
        'col_time': 'Fecha', 'col_type': 'Tipo', 'col_event': 'Evento',
        'no_match': 'Ningún evento coincide con el filtro.', 'empty': 'El historial está vacío.',
        'counter': '{shown} de {total} eventos (más recientes primero)',
        'refreshed': 'Actualizado {time}', 'refresh_failed': 'No se pudo actualizar ({error})',
        'confirm_clear': '¿Seguro que quieres borrar todo el historial de eventos? Esta acción no se puede deshacer.',
        'clear_failed': 'No se pudo borrar el historial: {error}',
        'tab_status': 'Estado', 'tab_devices': 'Dispositivos',
        'setup_banner': 'Todavía no vigilas ningún dispositivo. Elige cuáles vigilar para que el monitor pueda avisarte.',
        'choose_devices': 'Elegir dispositivos', 'watch_all_n': 'Vigilar todos ({n})',
        'reason_no_devices': 'Motivo: ningún dispositivo seleccionado',
        'availability_banner': 'La disponibilidad (availability) está desactivada en Zigbee2MQTT, por eso no se puede saber si un dispositivo está offline. Actívala en Zigbee2MQTT → Configuración → Disponibilidad.',
        'dev_search': 'Buscar por nombre o IEEE',
        'dev_summary': '{watched} vigilados · {unwatched} sin vigilar · Zigbee2MQTT: {z2m}',
        'watched': 'Vigilados', 'unwatched': 'Sin vigilar',
        'select_all': 'Seleccionar todos', 'select_none': 'Ninguno',
        'unwatch_n': 'Dejar de vigilar ({n})', 'watch_n': 'Vigilar ({n})', 'watch_all': 'Vigilar todos',
        'confirm_unwatch': '¿Dejar de vigilar {n} dispositivo(s)? No recibirás avisos de ellos:\n{names}',
        'confirm_unwatch_missing': 'Los desaparecidos ya no están en Zigbee2MQTT: se eliminan del monitor por completo y no pasarán a "Sin vigilar":\n{names}',
        'no_watched': 'Todavía no vigilas ningún dispositivo.',
        'all_watched': 'Todos los dispositivos de Zigbee2MQTT están vigilados.',
        'z2m_unavailable': 'Sin datos de Zigbee2MQTT: la lista "Sin vigilar" no está disponible por ahora.',
        'status': {'online': 'EN LÍNEA', 'offline': 'OFFLINE', 'missing': 'DESAPARECIDO', 'unknown': 'SIN DATOS'},
        'new_since': 'NUEVO · {when}', 'since': 'desde {when}',
        'update_failed': 'No se pudo actualizar la lista: {error}',
        'states': {'OK': 'OK', 'PROBLEM': 'PROBLEMA', 'NO_DATA': 'SIN DATOS'},
        'types': {'OK': 'OK', 'PROBLEM': 'PROBLEMA', 'NO_DATA': 'SIN DATOS', 'Z2M': 'Z2M', 'MQTT': 'MQTT',
                  'NOTIFY': 'NOTIF', 'SYSTEM': 'SISTEMA'},
    },
    'en': {
        'loading': 'Loading…', 'no_data_yet': 'No data yet',
        'registered': 'registered', 'stat_watched': 'watched', 'stat_unwatched': 'not watched',
        'offline': 'offline', 'missing': 'missing',
        'list_offline': 'Offline', 'list_missing': 'Missing', 'list_unknown': 'No data',
        'last_publish': 'Last publication', 'unknown': 'unknown',
        'all': 'All', 'search': 'Search device or text', 'clear': 'Clear history',
        'col_time': 'Time', 'col_type': 'Type', 'col_event': 'Event',
        'no_match': 'No event matches the filter.', 'empty': 'The history is empty.',
        'counter': '{shown} of {total} events (newest first)',
        'refreshed': 'Updated {time}', 'refresh_failed': 'Could not update ({error})',
        'confirm_clear': 'Are you sure you want to clear the whole event history? This cannot be undone.',
        'clear_failed': 'Could not clear the history: {error}',
        'tab_status': 'Status', 'tab_devices': 'Devices',
        'setup_banner': 'You are not watching any device yet. Choose which ones to watch so the monitor can alert you.',
        'choose_devices': 'Choose devices', 'watch_all_n': 'Watch all ({n})',
        'reason_no_devices': 'Reason: no device selected',
        'availability_banner': 'Availability is disabled in Zigbee2MQTT, so the monitor cannot tell whether a device is offline. Enable it in Zigbee2MQTT → Settings → Availability.',
        'dev_search': 'Search by name or IEEE',
        'dev_summary': '{watched} watched · {unwatched} not watched · Zigbee2MQTT: {z2m}',
        'watched': 'Watched', 'unwatched': 'Not watched',
        'select_all': 'Select all', 'select_none': 'None',
        'unwatch_n': 'Stop watching ({n})', 'watch_n': 'Watch ({n})', 'watch_all': 'Watch all',
        'confirm_unwatch': 'Stop watching {n} device(s)? You will not get alerts about them:\n{names}',
        'confirm_unwatch_missing': 'Missing devices are no longer in Zigbee2MQTT: they are removed from the monitor completely and will not move to "Not watched":\n{names}',
        'no_watched': 'You are not watching any device yet.',
        'all_watched': 'Every Zigbee2MQTT device is being watched.',
        'z2m_unavailable': 'No data from Zigbee2MQTT: the "Not watched" list is not available right now.',
        'status': {'online': 'ONLINE', 'offline': 'OFFLINE', 'missing': 'MISSING', 'unknown': 'NO DATA'},
        'new_since': 'NEW · {when}', 'since': 'since {when}',
        'update_failed': 'Could not update the list: {error}',
        'states': {'OK': 'OK', 'PROBLEM': 'PROBLEM', 'NO_DATA': 'NO DATA'},
        'types': {'OK': 'OK', 'PROBLEM': 'PROBLEM', 'NO_DATA': 'NO DATA', 'Z2M': 'Z2M', 'MQTT': 'MQTT',
                  'NOTIFY': 'NOTIFY', 'SYSTEM': 'SYSTEM'},
    },
}

LANG = 'en'  # set by main() from the language option


def t(key, **values):
    text = MESSAGES.get(LANG, MESSAGES['en']).get(key) or MESSAGES['en'][key]
    return text.format(**values) if values else text


def tn(key, count, **values):
    """Pick the singular or plural variant of a message."""
    return t(key + ('_one' if count == 1 else '_other'), **values)


def resolve_language(option, attempts=6, wait=5):
    """language option: 'es'/'en', or 'auto' = Home Assistant's language (fallback English)."""
    if option in LANGUAGES:
        return option
    for attempt in range(attempts):
        try:
            config = core_api('GET', 'config') or {}
            language = str(config.get('language') or '').lower().replace('_', '-').split('-')[0]
            return language if language in LANGUAGES else 'en'
        except Exception:
            if attempt + 1 < attempts:
                time.sleep(wait)
    LOG.warning('Could not read the Home Assistant language; using English')
    return 'en'


def parse_event(line):
    parts = line.rstrip('\n').split(' | ', 2)
    if len(parts) == 3:
        return {'time': parts[0], 'type': parts[1], 'message': parts[2]}
    return {'time': '', 'type': '', 'message': line.rstrip('\n')}


class Journal:
    """Durable event history in eventos.log (rotating). Survives restarts; the panel reads it."""

    def __init__(self, path=EVENTS):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.version = 0
        self.handler = logging.handlers.RotatingFileHandler(
            self.path, maxBytes=EVENTS_MAX_BYTES, backupCount=EVENTS_BACKUPS,
            encoding='utf-8', delay=True)
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        # Private logger (not in the global registry): writes only to eventos.log.
        self.logger = logging.Logger('zigbee_monitor.events', logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(self.handler)
        self.recent = collections.deque(self._read(RECENT_EVENTS), maxlen=RECENT_EVENTS)

    def files(self):
        """Oldest first: eventos.log.2, eventos.log.1, eventos.log."""
        backups = [self.path.with_name('%s.%d' % (self.path.name, i))
                   for i in range(EVENTS_BACKUPS, 0, -1)]
        return backups + [self.path]

    def _read(self, limit):
        lines = collections.deque(maxlen=limit)
        for file in self.files():
            try:
                with open(file, encoding='utf-8', errors='replace') as handle:
                    lines.extend(line for line in handle if line.strip())
            except FileNotFoundError:
                continue
        return [parse_event(line) for line in reversed(lines)]  # newest first

    def read(self, limit=PANEL_EVENTS):
        with self.lock:
            return self._read(limit)

    def write(self, kind, message):
        message = ' '.join(str(message).split())  # always a single line
        when = time.strftime('%Y-%m-%d %H:%M:%S')
        with self.lock:
            self.logger.info('%s | %s | %s', when, kind, message)
            self.recent.appendleft({'time': when, 'type': kind, 'message': message})
            self.version += 1

    def clear(self):
        """Delete eventos.log and all its backups, then leave a trace."""
        with self.lock:
            self.handler.acquire()
            try:
                if self.handler.stream:
                    self.handler.stream.close()
                    self.handler.stream = None  # reopened on the next write
                for file in self.files():
                    try:
                        file.unlink()
                    except FileNotFoundError:
                        pass
            finally:
                self.handler.release()
            self.recent.clear()
        LOG.warning(t('log_history_cleared'))
        self.write(SYSTEM, t('history_cleared'))


def journal_write(kind, message):
    if JOURNAL is None:
        return
    try:
        JOURNAL.write(kind, message)
    except OSError:
        LOG.warning(t('log_history_write_failed'))


def names(items, limit=10):
    items = list(items)
    text = ', '.join(items[:limit])
    if len(items) > limit:
        text += t('and_more', n=len(items) - limit)
    return text


# payload key, list label, "added" label, "removed" label
CATEGORIES = (
    ('offline_names', 'label_offline', 'new_offline', 'recovered'),
    ('missing_names', 'label_missing', 'new_missing', 'reappeared'),
    ('unknown_names', 'label_unknown', 'new_unknown', 'known_again'),
)


def describe(payload):
    """Full description of a status payload (baseline lines). The state goes in the type column."""
    if payload.get('reason') == 'no_devices':
        return t('no_devices')
    if not payload.get('data_valid'):
        return t('no_valid_data')
    parts = ['%s (%d): %s' % (t(label), len(payload[key]), names(payload[key]))
             for key, label, _, _ in CATEGORIES if payload.get(key)]
    if not parts:
        return tn('all_expected_ok', payload['expected'], n=payload['expected'])
    return ' · '.join(parts)


def state_type(payload):
    """Event type for a status line: OK, PROBLEM or NO_DATA."""
    return payload.get('state') if payload.get('data_valid') else NO_DATA


def delta(old, new):
    """What changed between two valid payloads, without calling a move between categories a recovery."""
    now_listed = set()
    for key, _, _, _ in CATEGORIES:
        now_listed.update(new.get(key) or [])
    changes = []
    for key, _, added_label, removed_label in CATEGORIES:
        before, after = set(old.get(key) or []), set(new.get(key) or [])
        added = sorted(after - before)
        removed = sorted((before - after) - now_listed)
        if added:
            changes.append('%s: %s' % (t(added_label), names(added)))
        if removed:
            changes.append('%s: %s' % (t(removed_label), names(removed)))
    return changes


def signature(payload):
    return (payload.get('state'), payload.get('data_valid'),
            tuple(payload.get('offline_names') or ()),
            tuple(payload.get('missing_names') or ()),
            tuple(payload.get('unknown_names') or ()))


class EventTracker:
    """Decides what reaches eventos.log: transitions only, never retry loops.

    After (re)connecting to MQTT the retained messages need a few seconds; after
    Zigbee2MQTT returns online the mesh needs minutes. During those settle windows
    the MQTT sensor keeps updating live, but nothing is written to the history;
    when the window closes a single line records the settled state.
    """

    def __init__(self, write=journal_write, settle_mqtt=None, settle_z2m=None, alerts=None):
        self.write = write
        self.alerts = alerts or NoAlerts()
        self.settle_mqtt = SETTLE_MQTT if settle_mqtt is None else settle_mqtt
        self.settle_z2m = SETTLE_Z2M if settle_z2m is None else settle_z2m
        self.mqtt = None          # None (never tried), 'up', 'down'
        self.ever_up = False
        self.bridge = None        # last known Zigbee2MQTT bridge state
        self.settle_until = None
        self.settle_is_z2m = False
        self.logged = None        # last payload written to the history
        self.logged_signature = None
        self.force_full = False
        self.availability = None  # last known Zigbee2MQTT availability setting

    def watch_list_changed(self, removed_names):
        """The panel changed the watched list: forget removed devices (no false 'recovered')
        and write the next state as a full line instead of a delta."""
        self.alerts.forget(removed_names)
        self.logged_signature = None
        self.force_full = True

    def _settle(self, now, seconds, z2m):
        end = now + seconds
        self.settle_until = end if self.settle_until is None else max(self.settle_until, end)
        self.settle_is_z2m = self.settle_is_z2m or z2m

    def mqtt_up(self, now):
        if self.mqtt != 'up':
            self.write(MQTT, t('mqtt_recovered') if self.ever_up else t('mqtt_connected'))
        self.mqtt, self.ever_up = 'up', True
        self._settle(now, self.settle_mqtt, z2m=False)

    def mqtt_down(self):
        if self.mqtt != 'down':
            self.write(MQTT, t('mqtt_lost') if self.mqtt == 'up' else t('mqtt_failed'))
        self.mqtt = 'down'

    def observe(self, payload, bridge, now, ready=True):
        if bridge is not None and bridge != self.bridge:
            if bridge == 'offline':
                self.write(Z2M, t('z2m_offline'))
            elif bridge == 'online' and self.bridge == 'offline':
                self.write(Z2M, t('z2m_back', s=self.settle_z2m))
                self._settle(now, self.settle_z2m, z2m=True)
            self.bridge = bridge
        if not ready or self.mqtt != 'up' or bridge != 'online':
            return
        if self.settle_until is not None:
            if now < self.settle_until:
                return
            z2m = self.settle_is_z2m
            self.settle_until, self.settle_is_z2m = None, False
            if z2m:
                self.write(Z2M, t('z2m_settled'))
                self._check_availability(payload.get('z2m_availability'))
                self._log(payload, full=True)
                return
        self._check_availability(payload.get('z2m_availability'))
        self._log(payload)

    def _check_availability(self, value):
        """One line (and one notice) when Zigbee2MQTT availability turns out disabled, one when enabled again."""
        if value is None or value == self.availability:
            return
        if value is False:
            self.write(SYSTEM, t('availability_off'))
        elif self.availability is False:
            self.write(SYSTEM, t('availability_on'))
        if self.availability is not None:
            self.force_full = True  # the whole picture changed: write the next state in full
        self.availability = value
        self.alerts.availability(value is False)

    def _log(self, payload, full=False):
        current = signature(payload)
        if not full and current == self.logged_signature:
            return
        full, self.force_full = full or self.force_full, False
        old = self.logged
        if full or old is None or not old.get('data_valid') or not payload.get('data_valid'):
            text = describe(payload)
        else:
            changes = delta(old, payload)
            if payload['state'] == OK:
                changes.append(t('all_online'))
            else:
                changes.append(t('total', offline=len(payload['offline_names']),
                                 missing=len(payload['missing_names'])))
            text = ' · '.join(changes)
        self.write(state_type(payload), text)
        self.logged, self.logged_signature = payload, current
        no_devices = payload.get('reason') == 'no_devices'
        self.alerts.setup(no_devices)
        if not no_devices:
            self.alerts.devices(payload)

    def tick(self, now):
        """Timers that must run even while disconnected (delayed system notifications)."""
        self.alerts.system(self.mqtt, self.bridge, now)


def send_notification(target, message):
    """target is notify.<name>: a notify entity (notify.send_message) or a legacy notify service."""
    try:
        core_api('GET', 'states/' + target)
        is_entity = True
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        is_entity = False
    if is_entity:
        core_api('POST', 'services/notify/send_message',
                 {'entity_id': target, 'title': NOTIFY_TITLE, 'message': message})
    else:
        core_api('POST', 'services/notify/' + target.split('.', 1)[1],
                 {'title': NOTIFY_TITLE, 'message': message})


def failure_reason(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return 'HTTP %d' % exc.code
    return type(exc).__name__


class Notifier:
    """Sends notifications in a background thread so HTTP calls never block the MQTT loop."""

    def __init__(self, targets, write=journal_write, send=None):
        self.targets = list(targets)
        self.write = write
        self.deliver = send or send_notification
        self.queue = queue.Queue()
        if self.targets:
            threading.Thread(target=self._worker, name='notifications', daemon=True).start()

    def send(self, message):
        if self.targets:
            self.queue.put(message)

    def flush(self, timeout):
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.1)

    def _worker(self):
        while True:
            message = self.queue.get()
            failed = []
            for target in self.targets:
                try:
                    self.deliver(target, message)
                except Exception as exc:  # one attempt per message: no retry loops
                    failed.append('%s (%s)' % (target, failure_reason(exc)))
            sent = len(self.targets) - len(failed)
            if sent:
                self.write(NOTIFY, tn('notify_sent', sent, n=sent, message=message))
            if failed:
                LOG.warning(t('log_notify_failed', targets=', '.join(failed)))
                self.write(NOTIFY, t('notify_failed', targets=', '.join(failed)))
            self.queue.task_done()


class Alerts:
    """Decides which notifications to send and remembers what was already notified.

    The memory lives in notificados.json, so a restart of the monitor does not
    repeat known problems, but still reports what recovered or failed meanwhile.
    Zigbee2MQTT offline and a lost MQTT connection are only notified if they last
    ALERT_DELAY seconds (a planned restart takes less).
    """

    def __init__(self, notifier, expected_names, path=ALERTS, delay=None):
        self.notifier = notifier
        self.path = Path(path)
        self.delay = ALERT_DELAY if delay is None else delay
        self.enabled = bool(notifier is not None and notifier.targets)
        saved = self._load() if self.enabled else {}
        problems = saved.get('problems')
        problems = problems if isinstance(problems, dict) else {}
        # Devices removed from (or renamed in) the list are forgotten silently.
        self.problems = {}
        for name, kind in problems.items():
            if name in expected_names and kind in ('offline', 'missing'):
                self.problems[name] = kind
        self.z2m_alerted = saved.get('z2m') is True
        self.mqtt_alerted = saved.get('mqtt') is True
        self.setup_alerted = saved.get('setup') is True
        self.availability_alerted = saved.get('availability') is True
        self.z2m_since = None
        self.mqtt_since = None

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save(self):
        data = {'problems': self.problems, 'z2m': self.z2m_alerted, 'mqtt': self.mqtt_alerted,
                'setup': self.setup_alerted, 'availability': self.availability_alerted}
        temporary = self.path.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True))
            os.replace(temporary, self.path)
        except OSError:
            LOG.warning(t('log_alerts_save_failed'))

    def forget(self, names):
        """Devices no longer watched: drop them silently from the memory."""
        if self.enabled and any(name in self.problems for name in names):
            for name in names:
                self.problems.pop(name, None)
            self._save()

    def setup(self, no_devices):
        """One reminder while nothing is watched (again only after something was chosen)."""
        if not self.enabled or no_devices == self.setup_alerted:
            return
        self.setup_alerted = no_devices
        self._save()
        if no_devices:
            self.notifier.send(t('alert_setup'))

    def availability(self, disabled):
        """One notice while Zigbee2MQTT availability is disabled (again only after it was enabled)."""
        if not self.enabled or disabled == self.availability_alerted:
            return
        self.availability_alerted = disabled
        self._save()
        if disabled:
            self.notifier.send(t('alert_availability'))

    def devices(self, payload):
        if not self.enabled or not payload.get('data_valid'):
            return
        current = {name: 'offline' for name in payload['offline_names']}
        current.update({name: 'missing' for name in payload['missing_names']})
        # Unknown availability is neither a new problem nor a recovery: keep what was notified.
        for name in payload.get('unknown_names') or []:
            if name in self.problems:
                current.setdefault(name, self.problems[name])
        new_offline = sorted(n for n, k in current.items() if k == 'offline' and self.problems.get(n) != 'offline')
        new_missing = sorted(n for n, k in current.items() if k == 'missing' and self.problems.get(n) != 'missing')
        recovered = sorted(n for n in self.problems if n not in current)
        if not (new_offline or new_missing or recovered):
            return
        parts = []
        if new_offline:
            parts.append('%s: %s' % (t('new_offline'), names(new_offline)))
        if new_missing:
            parts.append('%s: %s' % (t('new_missing'), names(new_missing)))
        if recovered:
            parts.append('%s: %s' % (t('recovered'), names(recovered)))
        offline = sum(1 for k in current.values() if k == 'offline')
        missing = len(current) - offline
        parts.append(t('total', offline=offline, missing=missing) if current else t('all_online'))
        self.problems = current
        self._save()
        self.notifier.send(' · '.join(parts))

    def system(self, mqtt, bridge, now):
        """mqtt: 'up'/'down'/None; bridge: last known Zigbee2MQTT state."""
        if not self.enabled:
            return
        minutes = max(1, self.delay // 60)
        if mqtt == 'down':
            self.mqtt_since = now if self.mqtt_since is None else self.mqtt_since
            if not self.mqtt_alerted and now - self.mqtt_since >= self.delay:
                self.mqtt_alerted = True
                self._save()
                self.notifier.send(t('alert_mqtt_down', m=minutes))
            return
        if mqtt == 'up':
            self.mqtt_since = None
            if self.mqtt_alerted:
                self.mqtt_alerted = False
                self._save()
                self.notifier.send(t('alert_mqtt_back'))
            if bridge == 'offline':
                self.z2m_since = now if self.z2m_since is None else self.z2m_since
                if not self.z2m_alerted and now - self.z2m_since >= self.delay:
                    self.z2m_alerted = True
                    self._save()
                    self.notifier.send(t('alert_z2m_down', m=minutes))
            elif bridge == 'online':
                self.z2m_since = None
                if self.z2m_alerted:
                    self.z2m_alerted = False
                    self._save()
                    self.notifier.send(t('alert_z2m_back'))


class NoAlerts:
    enabled = False

    def devices(self, payload):
        pass

    def forget(self, names):
        pass

    def setup(self, no_devices):
        pass

    def availability(self, disabled):
        pass

    def system(self, mqtt, bridge, now):
        pass


# ---------- MQTT discovery ----------

# Attributes of the main sensor: only what helps the user. The MQTT payload keeps every
# field (counters, panel and other consumers use it); without 'updated' and 'recent_events'
# the attributes only change when something really changes, so HA stops storing a row a minute.
STATE_ATTRIBUTES_TEMPLATE = (
    '{% set a = {"expected": value_json.expected, "offline_names": value_json.offline_names, '
    '"missing_names": value_json.missing_names, "unknown_names": value_json.unknown_names, '
    '"z2m_state": value_json.z2m_state, "z2m_availability": value_json.z2m_availability} %}'
    '{% if value_json.reason is defined %}{% set a = dict(a, reason=value_json.reason) %}{% endif %}'
    '{{ a | tojson }}')

def discovery_messages(enabled, slug=None):
    """(topic, payload) pairs announcing (or, if disabled, removing) the Home Assistant entities."""
    entities = (
        ('sensor', 'status'), ('binary_sensor', 'problem'), ('sensor', 'offline'), ('sensor', 'missing'),
    )
    topics = ['%s/%s/zigbee_monitor/%s/config' % (DISCOVERY_PREFIX, component, key)
              for component, key in entities]
    if not enabled:
        return [(topic, '') for topic in topics]  # empty retained payload removes the entity
    device = {'identifiers': ['zigbee_monitor'], 'name': 'Zigbee Monitor', 'manufacturer': 'Zigbee Monitor',
              'model': 'Home Assistant add-on', 'sw_version': VERSION}
    if slug:
        device['configuration_url'] = 'homeassistant://hassio/addon/%s/info' % slug
    common = {'state_topic': STATUS, 'availability_topic': AVAILABILITY,
              'payload_available': 'online', 'payload_not_available': 'offline',
              'device': device, 'origin': {'name': 'Zigbee Monitor', 'sw_version': VERSION}}
    configs = [
        dict(common, name=t('entity_state'), unique_id='zigbee_monitor_status',
             default_entity_id='sensor.zigbee_monitor_status', icon='mdi:zigbee',
             value_template='{{ value_json.state }}', json_attributes_topic=STATUS,
             json_attributes_template=STATE_ATTRIBUTES_TEMPLATE,
             device_class='enum', options=[OK, PROBLEM, NO_DATA]),
        dict(common, name=t('entity_problem'), unique_id='zigbee_monitor_problem',
             default_entity_id='binary_sensor.zigbee_monitor_problem', device_class='problem',
             value_template="{% if value_json.state == 'PROBLEM' %}ON{% elif value_json.state == 'OK' %}OFF"
                            "{% else %}None{% endif %}"),
        dict(common, name=t('entity_offline'), unique_id='zigbee_monitor_offline',
             default_entity_id='sensor.zigbee_monitor_offline', icon='mdi:lan-disconnect',
             state_class='measurement', value_template='{{ value_json.offline }}'),
        dict(common, name=t('entity_missing'), unique_id='zigbee_monitor_missing',
             default_entity_id='sensor.zigbee_monitor_missing', icon='mdi:help-network-outline',
             state_class='measurement', value_template='{{ value_json.missing }}'),
    ]
    return [(topic, json.dumps(config, ensure_ascii=False)) for topic, config in zip(topics, configs)]


# ---------- Web panel (Ingress) ----------

class PanelState:
    """Shared between the web panel (HTTP threads) and the main loop.

    A watch-list change is saved in the DeviceStore and queued for the main loop in one step
    under change_lock, and the main loop applies the queue and takes its status report under
    the same lock. Otherwise a report taken between both steps would already miss a removed
    offline device while the tracker still expects it, and announce a false recovery.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.payload = None
        self.changes = queue.Queue()  # watch-list changes, applied to tracker/journal by the main loop
        self.change_lock = threading.Lock()

    def set(self, payload):
        with self.lock:
            self.payload = payload

    def get(self):
        with self.lock:
            return self.payload

    def change_watch_list(self, store, add, remove, actual):
        """From the panel: save the change and queue it for the main loop, as one step."""
        with self.change_lock:
            added, removed = store.update(add, remove, actual)
            if added or removed:
                self.changes.put((added, removed))
            return added, removed

    def sync(self, monitor, tracker):
        """From the main loop: apply queued changes and take the status report, as one step.
        Returns (report, whether anything changed)."""
        changed = False
        with self.change_lock:
            while not self.changes.empty():
                added, removed = self.changes.get()
                for key, items in (('watch_now', added), ('watch_stopped', removed)):
                    if items:
                        LOG.info(t(key, names=names(items)))
                        journal_write(SYSTEM, t(key, names=names(items)))
                tracker.watch_list_changed(removed)
                changed = True
            return monitor.report(), changed


def render_page():
    texts = json.dumps(PANEL_TEXTS.get(LANG, PANEL_TEXTS['en']), ensure_ascii=False).replace('</', '<\\/')
    return PAGE.replace('__LANG__', LANG).replace('__TEXTS__', texts)


def parse_ieee_list(value):
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError('list expected')
    result = []
    for item in value:
        if not isinstance(item, str) or not IEEE_RE.fullmatch(item.lower()):
            raise ValueError('invalid IEEE')
        result.append(item.lower())
    return result


def make_handler(journal, panel, monitor=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'ZigbeeMonitor'
        sys_version = ''

        def log_message(self, *args):
            pass  # the panel polls every few seconds: never log requests

        def _allowed(self):
            if self.client_address[0] in INGRESS_PEERS:
                return True
            self.send_error(403)
            return False

        def _send(self, code, body, content_type):
            data = body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._allowed():
                return
            path = self.path.split('?', 1)[0]
            if path in ('/', '/index.html'):
                self._send(200, render_page(), 'text/html; charset=utf-8')
            elif path == '/api/events':
                body = {'version': VERSION, 'status': panel.get(), 'events': journal.read()}
                self._send(200, json.dumps(body, ensure_ascii=False), 'application/json; charset=utf-8')
            elif path == '/api/devices' and monitor is not None:
                self._send(200, json.dumps(monitor.devices_view(), ensure_ascii=False), 'application/json; charset=utf-8')
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._allowed():
                return
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(min(length, 65536)) if length else b''
            path = self.path.split('?', 1)[0]
            # A custom header cannot be sent cross-site without CORS: blocks CSRF.
            if path == '/api/clear':
                if self.headers.get('X-Zigbee-Monitor') != 'clear':
                    self.send_error(403)
                    return
                journal.clear()
                self._send(200, '{"ok": true}', 'application/json; charset=utf-8')
            elif path == '/api/devices' and monitor is not None:
                if self.headers.get('X-Zigbee-Monitor') != 'devices':
                    self.send_error(403)
                    return
                try:
                    body = json.loads(raw or b'{}')
                    add, remove = parse_ieee_list(body.get('add', [])), parse_ieee_list(body.get('remove', []))
                except (ValueError, AttributeError):
                    self.send_error(400)
                    return
                with monitor.lock:
                    actual = dict(monitor.actual) if monitor.valid else None
                if add and actual is None:
                    self.send_error(409)  # Zigbee2MQTT has no valid data: nothing can be added
                    return
                added, removed = panel.change_watch_list(monitor.store, add, remove, actual or {})
                self._send(200, json.dumps({'added': added, 'removed': removed}, ensure_ascii=False),
                           'application/json; charset=utf-8')
            else:
                self.send_error(404)

    return Handler


def start_panel(journal, panel, port=PANEL_PORT, monitor=None):
    try:
        server = ThreadingHTTPServer(('0.0.0.0', port), make_handler(journal, panel, monitor))
    except OSError:
        LOG.warning(t('log_panel_failed', port=port))
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name='panel', daemon=True).start()
    return server


def own_slug():
    try:
        return supervisor('addons/self/info').get('slug')
    except Exception:
        return None


def main():
    global JOURNAL, LANG
    import paho.mqtt.client as mqtt
    JOURNAL = Journal()
    store = DeviceStore()
    options = read_options()
    LANG = resolve_language(options.get('language', 'auto'))
    targets = notify_targets(options)
    discovery = options.get('mqtt_discovery', True)
    base = options['z2m_base_topic']
    try:
        broker = mqtt_settings(options)
    except NoBroker:
        LOG.error(t('no_broker'))
        journal_write(SYSTEM, t('no_broker'))
        raise SystemExit(1) from None
    mode = t('mqtt_auto') if broker['mode'] == 'auto' else t('mqtt_manual', host=broker['host'])
    journal_write(SYSTEM, tn('started', len(targets), version=VERSION, n=len(store.watched),
                             t=len(targets), mqtt=mode, base=base))
    monitor = Monitor(store, base)
    notifier = Notifier(targets)
    tracker = EventTracker(alerts=Alerts(notifier, set(monitor.expected.values())))
    panel = PanelState()
    start_panel(JOURNAL, panel, monitor=monitor)
    announcements = discovery_messages(discovery, own_slug() if discovery else None)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='zigbee-monitor-' + os.uname().nodename)
    if broker['user']:
        client.username_pw_set(broker['user'], broker['password'])
    if broker['tls']:
        client.tls_set_context(ssl.create_default_context())
    # If the process dies, the broker marks every entity as unavailable.
    client.will_set(AVAILABILITY, 'offline', qos=1, retain=True)
    unavailable = dict(monitor.report(), state=NO_DATA, reason='monitor_disconnected')
    connected = False
    dirty = True

    def on_connect(c, userdata, flags, reason, properties):
        nonlocal connected, dirty
        if reason.is_failure:
            LOG.error(t('log_mqtt_refused'))
            tracker.mqtt_down()
            return
        connected = True
        monitor.reset()
        dirty = True
        c.subscribe(base + '/#', qos=1)
        for topic, payload in announcements:
            c.publish(topic, payload, qos=1, retain=True)
        c.publish(AVAILABILITY, 'online', qos=1, retain=True)
        LOG.info(t('log_mqtt_connected'))
        tracker.mqtt_up(time.monotonic())

    def on_disconnect(c, userdata, flags, reason, properties):
        nonlocal connected
        if connected and running:  # a normal stop is not a lost connection
            LOG.warning(t('log_mqtt_disconnected'))
            tracker.mqtt_down()
        connected = False
        monitor.reset()

    def on_message(c, userdata, message):
        nonlocal dirty
        try:
            if monitor.wants(message.topic):
                if monitor.receive(message.topic, message.payload):
                    new = store.observe(dict(monitor.actual))
                    if new:
                        journal_write(SYSTEM, t('new_unwatched', names=names(new)))
                dirty = True
        except (ValueError, TypeError, UnicodeError):
            LOG.warning(t('log_invalid_message'))

    def on_subscribe(c, userdata, mid, reasons, properties):
        if any(reason.is_failure for reason in reasons):
            LOG.error(t('log_mqtt_acl'))
            c.disconnect()

    client.on_connect, client.on_disconnect = on_connect, on_disconnect
    client.on_message, client.on_subscribe = on_message, on_subscribe
    last_publish = 0
    journal_version = None
    change_log = ChangeLogger()
    LOG.info(t('log_start', version=VERSION, n=len(monitor.expected), language=LANG) + ' · MQTT: ' + mode + ' · Z2M: ' + base)
    try:
        while running:
            if not connected:
                try:
                    client.connect(broker['host'], broker['port'], keepalive=30)
                    deadline = time.monotonic() + 10
                    while running and not connected and time.monotonic() < deadline:
                        if client.loop(timeout=1) != mqtt.MQTT_ERR_SUCCESS:
                            break
                except (OSError, ValueError):
                    LOG.warning(t('log_mqtt_retry'))
                if not connected:
                    tracker.mqtt_down()
                    tracker.tick(time.monotonic())
                    time.sleep(5)
                    continue
            if client.loop(timeout=1) != mqtt.MQTT_ERR_SUCCESS:
                if connected:
                    LOG.warning(t('log_mqtt_disconnected'))
                    tracker.mqtt_down()
                connected = False
                continue
            report, changed = panel.sync(monitor, tracker)
            dirty = dirty or changed
            now = time.monotonic()
            tracker.observe(report, monitor.bridge, now)
            tracker.tick(now)
            if JOURNAL.version != journal_version:
                dirty = True
            if (dirty and now - last_publish >= 2) or now - last_publish >= 60:
                payload = monitor.report()
                journal_version = JOURNAL.version
                payload['recent_events'] = list(JOURNAL.recent)
                panel.set(payload)
                info = client.publish(STATUS, json.dumps(payload, ensure_ascii=False), qos=1, retain=True)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    LOG.warning(t('log_publish_failed'))
                else:
                    change_log.report(payload)
                    dirty, last_publish = False, now
        journal_write(SYSTEM, t('stopped'))
        notifier.flush(5)
    finally:
        if connected:
            final = dict(unavailable, recent_events=list(JOURNAL.recent))
            infos = [client.publish(STATUS, json.dumps(final, ensure_ascii=False), qos=1, retain=True),
                     client.publish(AVAILABILITY, 'offline', qos=1, retain=True)]
            deadline = time.monotonic() + 3
            while not all(i.is_published() for i in infos) and time.monotonic() < deadline:
                client.loop(timeout=0.2)
            client.disconnect()
            client.loop(timeout=0.2)


PAGE = r'''<!doctype html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zigbee Monitor</title>
<style>
:root {
  --bg: #f5f6f8; --card: #ffffff; --text: #1c1f23; --muted: #6b7280; --line: #e3e6ea;
  --ok: #1f8a4c; --ok-bg: #e5f5ec; --bad: #c62828; --bad-bg: #fdecec;
  --warn: #8a6d1f; --warn-bg: #fbf3dc; --info: #1f5f8a; --info-bg: #e3f0fa; --btn: #c62828;
  --z2m: #6b3fa0; --z2m-bg: #f0e8fa; --neutral-bg: #eceef1; --notif: #0f6e6e; --notif-bg: #dff3f1;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111418; --card: #1b1f24; --text: #e6e8eb; --muted: #9aa3ad; --line: #2c3238;
    --ok: #5fd08f; --ok-bg: #173323; --bad: #ff7b7b; --bad-bg: #3a1a1a;
    --warn: #e6c46b; --warn-bg: #3a3217; --info: #7cc1f0; --info-bg: #16303f; --btn: #d9534f;
    --z2m: #c4a2f0; --z2m-bg: #2c2140; --neutral-bg: #262b31; --notif: #6fd6cc; --notif-bg: #133331;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 16px; }
header { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
h1 { font-size: 20px; margin: 0; }
.version { color: var(--muted); font-size: 12px; }
.spacer { flex: 1; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px; margin-bottom: 16px; }
.status { display: flex; flex-wrap: wrap; gap: 16px; align-items: center; }
.badge { font-weight: 700; padding: 6px 12px; border-radius: 999px; font-size: 15px; }
.badge.OK { color: var(--ok); background: var(--ok-bg); }
.badge.PROBLEM { color: var(--bad); background: var(--bad-bg); }
.badge.NO_DATA, .badge.none { color: var(--warn); background: var(--warn-bg); }
.stats { display: flex; gap: 20px; flex-wrap: wrap; }
.stat b { display: block; font-size: 18px; }
.stat span { color: var(--muted); font-size: 12px; }
.stat.alert b { color: var(--bad); }
.lists { margin-top: 12px; display: grid; gap: 6px; }
.lists div { overflow-wrap: anywhere; }
.muted { color: var(--muted); font-size: 12px; }
.toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
select, input { background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; font: inherit; }
input { flex: 1; min-width: 160px; }
button { background: transparent; color: var(--btn); border: 1px solid var(--btn); border-radius: 6px; padding: 6px 12px; font: inherit; cursor: pointer; }
button:hover { background: var(--bad-bg); }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 12px; }
td.when { white-space: nowrap; font-variant-numeric: tabular-nums; color: var(--muted); }
td.msg { overflow-wrap: anywhere; }
.kind { font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 4px; white-space: nowrap; }
.kind.OK { color: var(--ok); background: var(--ok-bg); }
.kind.PROBLEM { color: var(--bad); background: var(--bad-bg); }
.kind.NO_DATA { color: var(--warn); background: var(--warn-bg); }
.kind.Z2M { color: var(--z2m); background: var(--z2m-bg); }
.kind.MQTT { color: var(--muted); background: var(--neutral-bg); }
.kind.SYSTEM { color: var(--info); background: var(--info-bg); }
.kind.NOTIFY { color: var(--notif); background: var(--notif-bg); }
.empty { color: var(--muted); padding: 16px 8px; }
.error { color: var(--bad); }
@media (max-width: 600px) { td.when { white-space: normal; } }
.tabs { display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 1px solid var(--line); }
.tab { padding: 8px 14px; border: none; border-bottom: 2px solid transparent; border-radius: 0; background: none; color: var(--muted); font: inherit; cursor: pointer; }
.tab.active { color: var(--text); border-bottom-color: var(--info); font-weight: 600; }
.tab:hover { background: none; color: var(--text); }
.banner { background: var(--warn-bg); color: var(--warn); border-radius: 8px; padding: 12px 14px; margin-bottom: 16px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.banner b { flex: 1; min-width: 200px; }
button.primary { background: var(--info); color: #fff; border-color: var(--info); }
button.primary:hover { background: var(--info); opacity: .9; }
button.neutral { color: var(--text); border-color: var(--line); }
button.neutral:hover { background: var(--neutral-bg); }
button:disabled { opacity: .45; cursor: default; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
.col h2 { font-size: 15px; margin: 0 0 8px; }
.col h2 .count { color: var(--muted); font-weight: 400; }
.bar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; font-size: 13px; }
.bar a { color: var(--info); cursor: pointer; }
.list { border: 1px solid var(--line); border-radius: 8px; max-height: 60vh; overflow: auto; }
.row { display: flex; align-items: center; gap: 10px; padding: 5px 10px; border-bottom: 1px solid var(--line); }
.row:last-child { border-bottom: none; }
.row input { flex: 0 0 auto; min-width: 0; width: auto; }
.row label { flex: 1; overflow-wrap: anywhere; cursor: pointer; }
.row .ieee { color: var(--muted); font-size: 11px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }
.pill { font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 4px; white-space: nowrap; }
.pill.online { color: var(--ok); background: var(--ok-bg); }
.pill.offline, .pill.missing { color: var(--bad); background: var(--bad-bg); }
.pill.unknown { color: var(--warn); background: var(--warn-bg); }
.pill.new { color: var(--info); background: var(--info-bg); }
.pill.since { color: var(--muted); background: var(--neutral-bg); }
.note { padding: 12px 10px; color: var(--muted); }
.hidden { display: none !important; }
</style>
</head>
<body>
<main>
  <header>
    <h1>Zigbee Monitor</h1><span class="version" id="version"></span>
    <span class="spacer"></span><span class="muted" id="refreshed"></span>
  </header>
  <nav class="tabs">
    <button class="tab active" id="tab-status" data-t="tab_status"></button>
    <button class="tab" id="tab-devices" data-t="tab_devices"></button>
  </nav>
  <div id="view-status">
    <div class="banner hidden" id="setup">
      <b data-t="setup_banner"></b>
      <button class="neutral" id="setup-choose" data-t="choose_devices"></button>
      <button class="primary" id="setup-all"></button>
    </div>
    <div class="banner hidden" id="availability-off"><b data-t="availability_banner"></b></div>
    <section class="card">
      <div class="status">
        <span class="badge none" id="state"></span>
        <div class="stats">
          <div class="stat"><b id="registered">–</b><span data-t="registered"></span></div>
          <div class="stat"><b id="watched-n">–</b><span data-t="stat_watched"></span></div>
          <div class="stat"><b id="unwatched-n">–</b><span data-t="stat_unwatched"></span></div>
          <div class="stat" id="stat-offline"><b id="offline">–</b><span data-t="offline"></span></div>
          <div class="stat" id="stat-missing"><b id="missing">–</b><span data-t="missing"></span></div>
        </div>
      </div>
      <div class="lists" id="lists"></div>
      <div class="muted" id="updated"></div>
    </section>
    <section class="card">
      <div class="toolbar">
        <select id="filter"></select>
        <input id="search" type="search">
        <button id="clear" type="button"></button>
      </div>
      <table>
        <thead><tr><th data-t="col_time"></th><th data-t="col_type"></th><th data-t="col_event"></th></tr></thead>
        <tbody id="events"></tbody>
      </table>
      <div class="muted" id="counter"></div>
    </section>
  </div>
  <div id="view-devices" class="hidden">
    <section class="card">
      <div class="toolbar">
        <input id="dev-search" type="search">
        <span class="muted" id="dev-summary"></span>
      </div>
      <div class="note hidden" id="z2m-note" data-t="z2m_unavailable"></div>
      <div class="grid">
        <div class="col">
          <h2><span data-t="watched"></span> <span class="count" id="w-count"></span></h2>
          <div class="bar"><a id="w-all" data-t="select_all"></a>·<a id="w-none" data-t="select_none"></a>
            <span class="spacer"></span><button class="neutral" id="unwatch" disabled></button></div>
          <div class="list" id="w-list"></div>
        </div>
        <div class="col">
          <h2><span data-t="unwatched"></span> <span class="count" id="u-count"></span></h2>
          <div class="bar"><a id="u-all" data-t="select_all"></a>·<a id="u-none" data-t="select_none"></a>
            <span class="spacer"></span><button class="neutral" id="watch" disabled></button>
            <button class="primary" id="watch-all" data-t="watch_all" disabled></button></div>
          <div class="list" id="u-list"></div>
        </div>
      </div>
    </section>
  </div>
</main>
<script>
const T = __TEXTS__;
const TYPES = ['OK', 'PROBLEM', 'NO_DATA', 'Z2M', 'MQTT', 'NOTIFY', 'SYSTEM'];
let events = [];
let devices = null;
const checked = { w: new Set(), u: new Set() };
const $ = (id) => document.getElementById(id);
const fmt = (text, values) => text.replace(/\{(\w+)\}/g, (m, k) => (k in values ? values[k] : m));

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setup() {
  document.querySelectorAll('[data-t]').forEach((node) => { node.textContent = T[node.dataset.t]; });
  $('state').textContent = T.loading;
  $('search').placeholder = T.search;
  $('search').setAttribute('aria-label', T.search);
  $('dev-search').placeholder = T.dev_search;
  $('dev-search').setAttribute('aria-label', T.dev_search);
  $('filter').setAttribute('aria-label', T.col_type);
  $('clear').textContent = T.clear;
  const all = el('option', '', T.all); all.value = ''; $('filter').append(all);
  for (const type of TYPES) { const o = el('option', '', T.types[type]); o.value = type; $('filter').append(o); }
}

function showTab(name) {
  $('tab-status').classList.toggle('active', name === 'status');
  $('tab-devices').classList.toggle('active', name === 'devices');
  $('view-status').classList.toggle('hidden', name !== 'status');
  $('view-devices').classList.toggle('hidden', name !== 'devices');
  if (name === 'devices') loadDevices();
}

function renderStatus(s) {
  const badge = $('state');
  if (!s) { badge.className = 'badge none'; badge.textContent = T.no_data_yet; return; }
  badge.className = 'badge ' + s.state;
  badge.textContent = T.states[s.state] || s.state;
  const num = (v) => (v === null || v === undefined ? '–' : v);
  $('registered').textContent = num(s.registered);
  $('watched-n').textContent = num(s.expected);
  $('offline').textContent = num(s.offline);
  $('missing').textContent = num(s.missing);
  $('stat-offline').classList.toggle('alert', s.offline > 0);
  $('stat-missing').classList.toggle('alert', s.missing > 0);
  const noDevices = s.reason === 'no_devices';
  $('setup').classList.toggle('hidden', !noDevices);
  $('availability-off').classList.toggle('hidden', s.z2m_availability !== false);
  const lists = $('lists');
  lists.replaceChildren();
  if (noDevices) { lists.append(el('div', 'muted', T.reason_no_devices)); }
  const groups = [[T.list_offline, s.offline_names], [T.list_missing, s.missing_names], [T.list_unknown, noDevices ? [] : s.unknown_names]];
  for (const [label, items] of groups) {
    if (items && items.length) {
      const row = el('div');
      row.append(el('b', '', label + ': '), document.createTextNode(items.join(', ')));
      lists.append(row);
    }
  }
  const when = s.updated ? new Date(s.updated).toLocaleString() : '–';
  const z2m = s.z2m_state === 'unknown' || !s.z2m_state ? T.unknown : s.z2m_state;
  $('updated').textContent = T.last_publish + ': ' + when + ' · Zigbee2MQTT: ' + z2m;
}

function renderEvents() {
  const filter = $('filter').value;
  const text = $('search').value.trim().toLowerCase();
  const body = $('events');
  body.replaceChildren();
  const shown = events.filter((ev) =>
    (!filter || ev.type === filter) && (!text || ev.message.toLowerCase().includes(text)));
  for (const ev of shown) {
    const tr = el('tr');
    tr.append(el('td', 'when', ev.time.replace(/-/g, '‑')));  // wrap only between date and time
    const td = el('td');
    td.append(el('span', 'kind ' + ev.type, T.types[ev.type] || ev.type || '—'));
    tr.append(td, el('td', 'msg', ev.message));
    body.append(tr);
  }
  if (!shown.length) {
    const tr = el('tr'); const td = el('td', 'empty', events.length ? T.no_match : T.empty);
    td.colSpan = 3; tr.append(td); body.append(tr);
  }
  $('counter').textContent = fmt(T.counter, { shown: shown.length, total: events.length });
}

function shortDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const today = new Date();
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { day: '2-digit', month: '2-digit' });
}

function deviceRow(kind, item) {
  const row = el('div', 'row');
  const box = el('input'); box.type = 'checkbox'; box.id = kind + '-' + item.ieee;
  box.checked = checked[kind].has(item.ieee);
  box.addEventListener('change', () => { box.checked ? checked[kind].add(item.ieee) : checked[kind].delete(item.ieee); updateButtons(); });
  const label = el('label'); label.htmlFor = box.id;
  label.append(document.createTextNode(item.name + ' '), el('span', 'ieee', '— ' + item.ieee));
  let pill;
  if (kind === 'w') pill = el('span', 'pill ' + item.status, T.status[item.status] || item.status);
  else pill = item.new ? el('span', 'pill new', fmt(T.new_since, { when: shortDate(item.first_seen) }))
                       : el('span', 'pill since', fmt(T.since, { when: shortDate(item.first_seen) }));
  row.append(box, label, pill);
  return row;
}

function visible(items) {
  const text = $('dev-search').value.trim().toLowerCase();
  return items.filter((i) => !text || i.name.toLowerCase().includes(text) || i.ieee.includes(text));
}

function renderDevices() {
  if (!devices) return;
  // drop selections of devices that are no longer in each list
  checked.w = new Set([...checked.w].filter((i) => devices.watched.some((d) => d.ieee === i)));
  checked.u = new Set([...checked.u].filter((i) => devices.unwatched.some((d) => d.ieee === i)));
  const z2m = devices.z2m_state === 'unknown' ? T.unknown : devices.z2m_state;
  $('dev-summary').textContent = fmt(T.dev_summary, { watched: devices.watched.length,
    unwatched: devices.z2m_valid ? devices.unwatched.length : '–', z2m });
  $('z2m-note').classList.toggle('hidden', devices.z2m_valid);
  $('w-count').textContent = '(' + devices.watched.length + ')';
  $('u-count').textContent = devices.z2m_valid ? '(' + devices.unwatched.length + ')' : '';
  $('unwatched-n').textContent = devices.z2m_valid ? devices.unwatched.length : '–';
  const w = $('w-list'); w.replaceChildren();
  const ws = visible(devices.watched);
  ws.forEach((d) => w.append(deviceRow('w', d)));
  if (!devices.watched.length) w.append(el('div', 'note', T.no_watched));
  const u = $('u-list'); u.replaceChildren();
  const us = visible(devices.unwatched);
  us.forEach((d) => u.append(deviceRow('u', d)));
  if (devices.z2m_valid && !devices.unwatched.length) u.append(el('div', 'note', T.all_watched));
  updateButtons();
}

function updateButtons() {
  if (!devices) return;
  $('unwatch').textContent = fmt(T.unwatch_n, { n: checked.w.size });
  $('unwatch').disabled = !checked.w.size;
  $('watch').textContent = fmt(T.watch_n, { n: checked.u.size });
  $('watch').disabled = !checked.u.size || !devices.z2m_valid;
  $('watch-all').disabled = !devices.unwatched.length || !devices.z2m_valid;
  const n = devices.z2m_valid ? devices.unwatched.length : 0;
  $('setup-all').textContent = fmt(T.watch_all_n, { n });
  $('setup-all').disabled = !n;
}

async function changeDevices(add, remove) {
  try {
    const r = await fetch('api/devices', { method: 'POST', headers: { 'X-Zigbee-Monitor': 'devices', 'Content-Type': 'application/json' },
      body: JSON.stringify({ add, remove }) });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    checked.w.clear(); checked.u.clear();
  } catch (err) {
    alert(fmt(T.update_failed, { error: err.message }));
  }
  await loadDevices();
  load();
  // the monitor applies the change and republishes within a couple of seconds
  setTimeout(load, 1500);
  setTimeout(load, 3000);
}

async function loadDevices() {
  try {
    const r = await fetch('api/devices', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    devices = await r.json();
    renderDevices();
  } catch (err) {
    $('refreshed').className = 'error';
    $('refreshed').textContent = fmt(T.refresh_failed, { error: err.message });
  }
}

async function load() {
  try {
    const r = await fetch('api/events', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    $('version').textContent = 'v' + data.version;
    events = data.events || [];
    renderStatus(data.status);
    renderEvents();
    $('refreshed').className = 'muted';
    $('refreshed').textContent = fmt(T.refreshed, { time: new Date().toLocaleTimeString() });
  } catch (err) {
    $('refreshed').className = 'error';
    $('refreshed').textContent = fmt(T.refresh_failed, { error: err.message });
  }
}

setup();
$('tab-status').addEventListener('click', () => showTab('status'));
$('tab-devices').addEventListener('click', () => showTab('devices'));
$('setup-choose').addEventListener('click', () => showTab('devices'));
$('setup-all').addEventListener('click', () => devices && changeDevices(devices.unwatched.map((d) => d.ieee), []));
$('filter').addEventListener('change', renderEvents);
$('search').addEventListener('input', renderEvents);
$('dev-search').addEventListener('input', renderDevices);
$('w-all').addEventListener('click', () => { visible(devices.watched).forEach((d) => checked.w.add(d.ieee)); renderDevices(); });
$('w-none').addEventListener('click', () => { checked.w.clear(); renderDevices(); });
$('u-all').addEventListener('click', () => { visible(devices.unwatched).forEach((d) => checked.u.add(d.ieee)); renderDevices(); });
$('u-none').addEventListener('click', () => { checked.u.clear(); renderDevices(); });
$('watch').addEventListener('click', () => changeDevices([...checked.u], []));
$('watch-all').addEventListener('click', () => changeDevices(devices.unwatched.map((d) => d.ieee), []));
$('unwatch').addEventListener('click', () => {
  const selected = devices.watched.filter((d) => checked.w.has(d.ieee));
  const names = selected.map((d) => d.name);
  const gone = selected.filter((d) => d.status === 'missing').map((d) => d.name);
  let text = fmt(T.confirm_unwatch, { n: names.length, names: names.join(', ') });
  if (gone.length) text += '\n\n' + fmt(T.confirm_unwatch_missing, { names: gone.join(', ') });
  if (!confirm(text)) return;
  changeDevices([], [...checked.w]);
});
$('clear').addEventListener('click', async () => {
  if (!confirm(T.confirm_clear)) return;
  try {
    const r = await fetch('api/clear', { method: 'POST', headers: { 'X-Zigbee-Monitor': 'clear' } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
  } catch (err) {
    alert(fmt(T.clear_failed, { error: err.message }));
  }
  load();
});
load();
loadDevices();
setInterval(() => { load(); loadDevices(); }, 10000);
</script>
</body>
</html>
'''


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        main()
    except Exception as exc:
        # Do not log exception bodies, API payloads, tokens, or credentials.
        LOG.error(t('log_fatal', error=type(exc).__name__))
        journal_write(SYSTEM, t('stopped_error', error=type(exc).__name__))
        raise SystemExit(1) from None
