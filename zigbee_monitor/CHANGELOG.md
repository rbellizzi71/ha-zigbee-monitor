# Changelog

## 1.4.0

- New **Traffic** tab: messages Zigbee2MQTT publishes for each device in the last hour, day, week or
  month, busiest first, with the Watched / Not watched tag. Only counts are stored (hourly, 31 days).
- New **Incidents** tab: an automatic report of every general failure and every Zigbee2MQTT outage
  longer than 2 minutes, even with those notifications off. Each report has the affected devices,
  the coordinator, the 10 minutes before (Zigbee2MQTT warnings and errors, health, traffic, last
  message), a timeline and how it ended. Summary of all incidents, download and delete one or all;
  the last 30 are kept. Reports survive restarts and power cuts. A history line is written for each
  incident.
- Settings tab: Home Assistant entities of the coordinator saved in each incident.
- Unexpected stops (power cut, hang, forced restart) are reported in the history at the next start.
- Tab bar scrolls on narrow screens; tab order Status · Devices · Traffic · Incidents · Settings.

## 1.3.0

- New **Settings** tab in the panel: choose which notifications are sent (devices, general failure,
  system). Changes apply at once, without a restart, and are written to the history. All are on by
  default. With general failure off, the lost devices come in the device notification, with names.
  Only notifications change: history, log, panel and entities keep recording everything.
- The same settings as three Home Assistant switches in the Zigbee Monitor device
  (`switch.zigbee_monitor_notify_devices`, `…_notify_general_failure`, `…_notify_system`), in sync
  with the panel, for dashboards and automations.
- Devices tab: watched devices with a problem come first (offline, missing, no data, then online),
  each group sorted by name and started by a coloured separator with its count.
- The start line in the history lists the active notifications.
- Release workflow: a release created by hand on GitHub is updated instead of failing the run.

## 1.2.1

- Fix: a device that left and rejoined the network, was re-paired or was renamed could stay
  *No data* indefinitely (and an offline state reported at that moment was missed). The monitor
  discarded the device's availability when the device list changed, and Zigbee2MQTT does not
  always publish it again (never when a device leaves and rejoins by itself). Zigbee2MQTT itself
  clears the availability of renamed and removed devices, so it is no longer discarded.

## 1.2.0

- General failure: five devices lost within a minute (or one after another) are announced at
  once, without names, instead of one message per device. Nothing is notified device by device
  while it lasts; one *Network stable* message ends it, right away when the network is back as
  before, or 2 minutes after the last reconnection otherwise. It replaces the failure notification.
- Buttons in the general-failure notification (Home Assistant Companion app): *Restart
  Zigbee2MQTT*, *Wait 10 min* (reminds you if devices are still down) and *Open Home Assistant*.
  No automation needed.
- The panel shows the general failure with a *Restart Zigbee2MQTT* button.
- Grouping wait shortened to 20 s; groups of up to four devices keep their names.
- History: new *Recovered* type (green) for lines that only report recoveries; *Problem* is used
  only when something is lost. Devices reconnecting while Zigbee2MQTT settles after a restart are
  now recorded one by one, at the moment they come back.

## 1.1.0

- Device notifications are grouped: each change waits 25 s for more; when devices stop changing
  (or after 3 minutes at most), one message tells the net result. A device that drops and comes
  back within the wait is not notified. The history still records every change as it happens.
- Three or more devices lost in one message are announced as a general failure (possible problem
  with the coordinator, a router or something else).

## 1.0.2

- Fixed: stopping watching an offline device could, rarely, send a false "Recovered" notification.

## 1.0.1

- Configuration options translated for Spanish (Latin America). Home Assistant does not fall back
  from `es-419` to `es`, so those users saw the options in English.

## 1.0.0

First public release.

- Web panel in the sidebar: status, event history and device selection.
- Detects offline and missing devices; marks devices new in Zigbee2MQTT.
- Notifications to phones or other notify targets, without repeats and with a 2-minute grace period
  for Zigbee2MQTT and MQTT outages.
- Four Home Assistant entities created through MQTT discovery.
- Automatic connection to the Mosquitto broker add-on; external brokers supported.
- Configurable Zigbee2MQTT base topic.
- Warning when availability is disabled in Zigbee2MQTT.
- English and Spanish.
