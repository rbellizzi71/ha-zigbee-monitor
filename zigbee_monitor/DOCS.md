# Zigbee Monitor

Zigbee Monitor watches the Zigbee devices you choose in **Zigbee2MQTT** and tells you when one of them
goes **offline** or **disappears** from your network. It adds a panel to the Home Assistant sidebar,
creates sensors you can use in automations and dashboards, and can send you notifications.

It only watches: it never controls, reconfigures or re-pairs your devices.

## Requirements

- Home Assistant OS or Home Assistant Supervised (add-ons are not available on other installation types).
- **Zigbee2MQTT**, as an add-on or on another machine, connected to an MQTT broker.
- **Availability enabled in Zigbee2MQTT.** It is disabled by default. Without it, Zigbee2MQTT does not
  report whether a device is online, so offline devices cannot be detected (missing devices still can).
  Enable it in the Zigbee2MQTT settings (*Availability*) or in its `configuration.yaml`:

  ```yaml
  availability:
    enabled: true
  ```

  If it is disabled, Zigbee Monitor shows a warning in its panel and sends one notification.

Other Zigbee integrations (ZHA, deCONZ) are not supported.

## Installation

1. Add this repository to Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, and
   paste `https://github.com/rbellizzi71/ha-zigbee-monitor`.
2. Install **Zigbee Monitor** and start it.
3. Open **Zigbee Monitor** from the sidebar and choose the devices to watch (see *First use*).

With the Mosquitto broker add-on no MQTT settings are needed: the add-on connects automatically.

## First use

When nothing is watched yet, the panel shows a banner with two choices:

- **Watch all (N)**: watch every device currently in Zigbee2MQTT.
- **Choose devices**: open the *Devices* tab and pick them one by one.

Until at least one device is watched, the monitor state stays *No data* (never *OK*), and one reminder
notification is sent if notifications are configured.

## Configuration

| Option | Default | Description |
|---|---|---|
| `mqtt_host` | *(empty)* | Leave empty to use Home Assistant's broker (Mosquitto add-on) automatically. Fill in only for an external broker. |
| `mqtt_port` | `1883` | External broker port. |
| `mqtt_user` | *(empty)* | User for the external broker. |
| `mqtt_password` | *(empty)* | Password of that user. |
| `mqtt_tls` | `false` | Encrypts the connection to the external broker. |
| `z2m_base_topic` | `zigbee2mqtt` | Change it only if you changed `base_topic` in Zigbee2MQTT. |
| `language` | `auto` | Language of the panel, history, notifications and entity names: `auto` (Home Assistant's language), `en` or `es`. Other languages fall back to English. |
| `mqtt_discovery` | `true` | Creates and keeps the entities in Home Assistant. Turning it off removes them. |
| `notify_targets` | *(empty)* | Notification targets, e.g. `notify.mobile_app_my_phone` or a notify entity. Every target receives every notification. |

The MQTT options are optional: enable *Show unused optional configuration options* to see them.
If `mqtt_host` is empty and no broker is available in Home Assistant, the add-on stops and its log
explains why.

## The panel

### Status tab

- **State**: *OK* (all watched devices online), *Problem* (at least one offline or missing) or
  *No data* (see below).
- **Counters**: registered devices in Zigbee2MQTT, watched, not watched, offline and missing.
- The names of offline, missing and no-data devices.
- **History**: the event log, newest first, with a type filter and a search box.
  *Clear history* deletes it (after confirmation).

### Devices tab

Two lists, with search, checkboxes and *select all / none*:

- **Watched**, each with its status: *Online*, *Offline*, *Missing* or *No data*. Devices with a
  problem come first (offline, then missing, then no data, then online), each group sorted by name
  and started by a coloured separator with its count. When every device is online there are no
  separators.
- **Not watched**: the other devices in Zigbee2MQTT. Devices added to Zigbee2MQTT in the last 24 hours
  are marked **NEW**.

New devices are **never watched automatically**: a line in the history tells you one appeared, and you
decide whether to watch it. Names always follow Zigbee2MQTT: renaming a device there renames it here.

### Traffic tab

How many messages Zigbee2MQTT publishes for each device, to find the "chatty" ones (radar presence
sensors, for example, can send more than one message per second).

- Periods: **last hour**, **day**, **week**, **month**.
- Every Zigbee2MQTT device, tagged **Watched** or **Not watched**, sorted from most to least
  messages, with messages per minute, share of the total and a bar.
- Only counts are stored, never the messages: per minute for the last hour (memory) and per hour
  for 31 days (`/data/traffic.json`, a few hundred KB at most, saved every 5 minutes). Devices are
  tracked by IEEE address, so a rename keeps their history.
- "Data since …" tells when counting started; a notice appears when the monitor was not running
  for part of the period.
- A message is each update Zigbee2MQTT publishes for the device: a good measure of what reaches
  MQTT and Home Assistant, not of the raw Zigbee radio traffic.

### Incidents tab

An automatic report of every **general failure** and every time **Zigbee2MQTT is down for more
than 2 minutes**, so you do not have to be watching the logs when it happens. It is recorded even
when those notifications are turned off.

Each report contains:

- start, end, duration and how it ended: *recovered by itself*, *after a restart* (Zigbee2MQTT
  restarted or a restart was requested), *not recovered* (still down after 6 hours; a later
  recovery is noted), or *during an interruption* (it recovered while Zigbee Monitor was stopped);
- the affected devices, when each one dropped and came back;
- the coordinator (type, firmware revision, adapter, port) as reported by Zigbee2MQTT;
- the **10 minutes before**: Zigbee2MQTT warnings and errors (e.g. `SRSP - SYS - ping`),
  availability changes, Zigbee2MQTT state and health reports, MQTT connection, messages per minute,
  the busiest devices and the last message received before the silence;
- a timeline of everything during the incident, including actions (restart, *Wait 10 min*);
- optionally, the values of Home Assistant entities of your coordinator at the start and the end
  (chosen in the *Settings* tab), e.g. to see whether it rebooted or overheated.

Only relevant facts are kept: messages of chatty devices and Zigbee2MQTT's *info* lines are just
counted, and each part has a size limit (the newest lines are kept and the report says how many
were dropped). Zigbee2MQTT publishes its warnings and errors on `bridge/logging` with its default
log level (*info*); with a higher level there is less context.

The tab shows a summary (number of incidents, mean time between them, most frequent hours, errors
that repeat before incidents), the list and the detail of each report. Reports can be **downloaded**
(JSON) or **deleted**, one by one or all together; deleting writes a line in the history. The last
**30** are kept in `/data/incidents/`. Reports contain your device names: review them before sharing
them publicly.

An open incident is saved within seconds of every change, so a Home Assistant restart or a power
cut does not lose it: when Zigbee Monitor starts again it continues the report and notes the gap.

### Settings tab

**Notifications**: choose which alerts reach your notification targets. A change applies at once,
with no restart, is kept across restarts and updates, and writes a *System* line in the history
(saying whether it came from the panel or from Home Assistant).

| Setting | What it sends | Default |
|---|---|---|
| **Devices** | New offline, missing and recovered devices. | On |
| **General failure** | The general-failure notice (with its buttons) and *Network stable*. | On |
| **System** | Zigbee2MQTT or the MQTT connection down for more than 2 minutes, the reminder while no device is watched, availability disabled. | On |

- Only notifications change: the history, the add-on log, the panel and the entities always record
  everything.
- With **General failure** off, a mass failure is not silent: the lost devices come in the device
  notification, with their names (if **Devices** is on). Turning it off during a general failure
  ends that failure without the *Network stable* message.
- While a kind is off, Zigbee Monitor still remembers what happened: turning it back on does not
  send old news.
- The same three settings are Home Assistant switches (see *Entities*).
- They only matter when `notify_targets` has at least one target; otherwise the tab says so.
- The start line in the history lists the active notifications, e.g. *Notifications: all*.

**Incidents**: up to 10 Home Assistant entities of your coordinator (comma separated, e.g. its
temperature or uptime sensors) whose values are saved at the start and end of each incident.
Entities that do not exist are rejected.

### What the statuses mean

| Status | Meaning |
|---|---|
| **Online** | Zigbee2MQTT reports the device as available. |
| **Offline** | The device is still in Zigbee2MQTT, but Zigbee2MQTT reports it as unavailable. |
| **Missing** | A watched device is no longer in Zigbee2MQTT (it was removed or its pairing was lost). Zigbee Monitor keeps its last known name. |
| **No data** | No availability information yet (for example right after a restart), Zigbee2MQTT is offline, or availability is disabled in Zigbee2MQTT. |

If you stop watching a missing device, it is removed from Zigbee Monitor completely. If it is paired
again later, it shows up as a new device.

## Entities

With `mqtt_discovery` enabled, one device called **Zigbee Monitor** is created with these entities:

| Entity | Description |
|---|---|
| `sensor.zigbee_monitor_status` | `OK`, `PROBLEM` or `NO_DATA`. |
| `binary_sensor.zigbee_monitor_problem` | On when the state is `PROBLEM`, off when `OK`, unknown when `NO_DATA`. |
| `sensor.zigbee_monitor_offline` | Number of offline watched devices. |
| `sensor.zigbee_monitor_missing` | Number of missing watched devices. |
| `switch.zigbee_monitor_notify_devices` | Device notifications on/off. |
| `switch.zigbee_monitor_notify_general_failure` | General-failure notifications on/off. |
| `switch.zigbee_monitor_notify_system` | System notifications on/off. |

The switches are the settings of the panel's *Settings* tab (a change on either side shows on the
other) and appear in the *Configuration* section of the device page. Use them in dashboards or
automations, e.g. to turn device notifications off at night. To hide them, disable them in Home
Assistant (entity settings).

Attributes of `sensor.zigbee_monitor_status`:

| Attribute | Description |
|---|---|
| `expected` | Number of watched devices. |
| `offline_names` | Offline devices. |
| `missing_names` | Missing devices. |
| `unknown_names` | Devices without data. |
| `z2m_state` | `online`, `offline` or `unknown`. |
| `z2m_availability` | Whether availability is enabled in Zigbee2MQTT (`true`, `false`, or `null` if not known yet). |
| `reason` | Only when relevant: `no_devices` (nothing watched), `z2m_availability_disabled`, `monitor_disconnected`. |

If the add-on stops, all entities become *unavailable*.

The full status is also published, retained, on the MQTT topic `zigbee_monitor/status`.

## Notifications

Notifications are sent only when something changes, never repeatedly for the same problem. Each
kind below can be turned off in the *Settings* tab (all are on by default):

- A watched device goes offline, goes missing, or recovers. These are grouped: each change waits
  20 seconds for more, and when devices stop changing a single message tells the net result, with
  names and totals. A device that drops and comes back within the wait is not notified.
- A **general failure**: five or more devices lost within a minute, or one after another. See below.
- Zigbee2MQTT has been offline for more than 2 minutes, and when it is back. A planned restart of
  Zigbee2MQTT that takes less than 2 minutes sends nothing.
- Zigbee Monitor has lost its MQTT connection for more than 2 minutes, and when it recovers.
- One reminder while no device is watched.
- One notice if availability is disabled in Zigbee2MQTT.

After Zigbee2MQTT restarts, devices reconnect gradually. Zigbee Monitor waits 2 minutes for the network
to settle and then notifies only the real difference from before the restart.

### General failure

When five or more watched devices are lost within a minute (or one right after another), one
message says so at once, with the number of devices but no names: a possible problem with the
coordinator, a router or something else. While the failure lasts, nothing is notified device by
device (the panel and the history still show everything). It ends with one *Network stable*
message, which replaces the failure notification on the phone:

- right away, when the network is back as it was before the failure;
- otherwise 2 minutes after the last reconnection (10 minutes at most), naming what is still offline.

The panel shows the failure with a **Restart Zigbee2MQTT** button.

### Notification buttons

With the Home Assistant Companion app (`notify.mobile_app_...` targets), the general-failure
notification has three buttons. No automation is needed:

- **Restart Zigbee2MQTT**: Zigbee Monitor asks Zigbee2MQTT to restart (useful when it lost the
  coordinator and does not recover by itself). You get a confirmation, and later the *Network stable*
  message.
- **Wait 10 min**: if the devices are still down after 10 minutes, the notification comes back.
- **Open Home Assistant**: opens the app on your main dashboard; Zigbee Monitor is in the sidebar.
  (The app cannot open an add-on panel straight from a notification.)

Tapping any button closes the notification (that is how phones work); the panel keeps its own
restart button. Buttons older than one hour are ignored. Other notification targets receive the same
message without buttons.

The history in the panel is not grouped: it records every change the moment it happens.

Zigbee Monitor remembers what it already notified, so restarting the add-on does not repeat alerts.

## History

The panel's history records transitions only. Each line has a type:

| Type | Used for |
|---|---|
| **OK** / **Problem** / **No data** | The state of the watched devices changed (*Problem*: something was lost). |
| **Recovered** | Devices came back and none was lost. Also written for each device that reconnects while Zigbee2MQTT settles after a restart. |
| **Z2M** | Zigbee2MQTT went offline, came back, and finished reconnecting. |
| **MQTT** | Zigbee Monitor connected to, lost or recovered the MQTT broker. |
| **Notify** | A notification was sent, or failed to send. |
| **System** | Start and stop, unexpected stops (power cut, hang, forced restart), watch-list changes, new devices in Zigbee2MQTT, availability setting, setting changes, incidents recorded or deleted, history cleared. |

The history keeps about 3 MB (a 1 MB file plus two older copies) and survives restarts and updates.

**Unexpected stops**: when Zigbee Monitor starts after it was not stopped in an orderly way (power
cut, hang, forced restart), a *System* line tells when it stopped working (it saves a sign of life
every minute).

## Example automation

Notify a phone with the names of offline devices whenever a problem starts:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.zigbee_monitor_problem
    to: "on"
actions:
  - action: notify.mobile_app_my_phone
    data:
      title: Zigbee problem
      message: >
        Offline: {{ state_attr('sensor.zigbee_monitor_status', 'offline_names') | join(', ') or 'none' }}.
        Missing: {{ state_attr('sensor.zigbee_monitor_status', 'missing_names') | join(', ') or 'none' }}.
```

You don't need this if you use the `notify_targets` option; it is useful for custom messages or
other actions.

## Troubleshooting

- **Every device shows *No data*.** Check that Zigbee2MQTT is running and that availability is enabled.
  Check that `z2m_base_topic` matches Zigbee2MQTT's `base_topic`.
- **The add-on stops with "No MQTT broker found".** Install and start the Mosquitto broker add-on, or
  fill in the MQTT options for your external broker.
- **A device appears as *Missing* but it is working.** It was probably re-paired and has a new IEEE
  address in Zigbee2MQTT. Stop watching the missing entry and watch the new one.
- **Notifications don't arrive.** Check the history: *Notify* lines say whether each delivery worked.
  Targets must exist in Home Assistant (*Developer tools → Actions*, search `notify.`).

## Uninstalling

Turn off `mqtt_discovery` and restart the add-on first: this removes the entities from Home Assistant.
Then uninstall the add-on. The watch list, history and notification memory are deleted with it.
