# Changelog

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
