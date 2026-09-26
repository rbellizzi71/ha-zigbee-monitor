"""Minimal fake of paho-mqtt 2.x used by monitor.py, driven by a timed script."""
import json, os, time

MQTT_ERR_SUCCESS = 0
MQTT_ERR_NO_CONN = 4

class CallbackAPIVersion:
    VERSION2 = 2

class Reason:
    def __init__(self, failure=False):
        self.is_failure = failure

class Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else payload.encode()

class Info:
    rc = 0
    def is_published(self):
        return True

T0 = time.monotonic()
SCRIPT = []      # (t, kind, data) kinds: msg(topic,payload) | drop | refuse_until
PUBLISHED = []
RETAINED = {}
TOPICS = []
WILL = []
CONNECTS = []   # (host, port, user)

class Client:
    def __init__(self, *a, **k):
        self.connected = False
        self.pending_connect = False
        self.i = 0
        self.refuse_until = 0
        self.user = None
    def username_pw_set(self, user, password=None): self.user = user
    def tls_set_context(self, *a): pass
    def will_set(self, topic, payload=None, qos=0, retain=False):
        WILL.append((topic, payload, retain))
    def subscribe(self, *a, **k): pass
    def disconnect(self):
        if self.connected:
            self.connected = False
            self.on_disconnect(self, None, None, Reason(False), None)
    def connect(self, host, port, keepalive=60):
        if time.monotonic() - T0 < self.refuse_until:
            raise OSError('refused')
        CONNECTS.append((host, port, self.user))
        self.pending_connect = True
    def publish(self, topic, payload, qos=0, retain=False):
        try:
            data = json.loads(payload)
        except ValueError:
            data = payload
        PUBLISHED.append((time.monotonic() - T0, data))
        TOPICS.append((topic, payload, retain))
        return Info()
    def loop(self, timeout=1.0):
        time.sleep(0.05)
        now = time.monotonic() - T0
        while self.i < len(SCRIPT) and SCRIPT[self.i][0] <= now:
            _, kind, data = SCRIPT[self.i]; self.i += 1
            if kind == 'drop':
                self.refuse_until = now + data
                if self.connected:
                    self.connected = False
                    self.on_disconnect(self, None, None, Reason(True), None)
                    return MQTT_ERR_NO_CONN
            elif kind == 'msg':
                RETAINED[data[0]] = data[1]
                if self.connected:
                    self.on_message(self, None, Msg(*data))
        if self.pending_connect:
            self.pending_connect = False
            self.connected = True
            self.on_connect(self, None, None, Reason(False), None)
            for topic, payload in list(RETAINED.items()):
                self.on_message(self, None, Msg(topic, payload))
        return MQTT_ERR_SUCCESS if self.connected else MQTT_ERR_NO_CONN
