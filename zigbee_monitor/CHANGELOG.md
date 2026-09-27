# Changelog

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
