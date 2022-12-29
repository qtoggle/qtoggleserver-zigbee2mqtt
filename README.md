 * [About](#about)
 * [Install](#install)
 * [Peripheral Driver & Parameters](#peripheral-driver--parameters)

---


## About

This is an add-on for [qToggleServer](https://github.com/qtoggle/qtoggleserver).

It provides a [peripheral driver](https://github.com/qtoggle/qtoggleserver/wiki/Peripheral-Drivers) that exposes ports
for Zigbee devices controlled by [Zigbee2MQTT](https://www.zigbee2mqtt.io/).


## Install

If you run [qToggleOS](https://github.com/qtoggle/qtoggleos), this add-on is already installed. You'll need, however, to
enable [Zigbee](https://github.com/qtoggle/qtoggleos/wiki/Zigbee) support.

Otherwise, install it using pip:

    pip install qtoggleserver-zigbee2mqtt


## Peripheral Driver & Parameters

### `driver`: `qtoggleserver.zigbee2mqtt.Zigbee2MQTTClient`

### `mqtt_server`

 * type: `string`
 * default: required
 * example: `"test.mosquitto.org"`

### `mqtt_port`

 * type: `integer`
 * default: `1883`

### `mqtt_username`

 * type: `string`
 * default: `null`

### `mqtt_password`

 * type: `string`
 * default: `null`

### `mqtt_reconnect_interval`

Represents the interval, in seconds, between two (re)connection attempts to the MQTT server.

 * type: `integer`
 * default: `5`

### `mqtt_logging`

Indicates whether the logs generated by the MQTT client will be added to the qToggleServer or not.

 * type: `boolean`
 * default: `false`

### `bridge_logging`

Indicates whether the logs generated by the Zigbee2MQTT bridge will be added to the qToggleServer or not.

 * type: `boolean`
 * default: `false`

### `bridge_request_timeout`

Indicates the timeout, in seconds, when waiting for a response (via MQTT) when sending a request (via MQTT) to the
bridge.

 * type: `integer`
 * default: `10`

### `permit_join_timeout`

Indicates the timeout, in seconds, to permit new Zigbee devices to join, once enabled.

 * type: `integer`
 * default: `3600`
