 * [About](#about)
 * [Install](#install)
 * [Peripheral Driver & Parameters](#peripheral-driver--parameters)
 * [Device Configuration](#device-configuration)

---


## About

This is an add-on for [qToggleServer](https://github.com/qtoggle/qtoggleserver).

It provides a [peripheral driver](https://github.com/qtoggle/qtoggleserver/wiki/Peripheral-Drivers) that exposes ports
for Zigbee devices controlled by [Zigbee2MQTT](https://www.zigbee2mqtt.io/).


## Install

If you run [qToggleOS](https://github.com/qtoggle/qtoggleos), you'll need
[Zigbee](https://github.com/qtoggle/qtoggleos/wiki/Zigbee) support.

Install using pip:

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

### `mqtt_client_id`

 * type: `string`
 * default: `qtoggleserver`

### `mqtt_reconnect_interval`

Represents the interval, in seconds, between two (re)connection attempts to the MQTT server.

 * type: `integer`
 * default: `5`

### `mqtt_base_topic`

 * type: `string`
 * default: `"zigbee2mqtt"`

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
 * default: `240`

### `device_config`

# TODO document wildcards

Allows specifying static configuration for particular Zigbee devices. Each entry in the dictionary is the (friendly)
name of a device associated to a configuration dictionary or a wildcard pattern that friendly names of devices must
match. See [Device Configuration](#device-configuration) for details on the available configuration options.

 * type: `dictionary`
 * default: `{}`


## Device Configuration

The following configuration options are available for a device:

### `get_state_property`

Some Zigbee devices won't allow obtaining the current state by doing a `get {"state": ""}`. Instead, they need one
of the specific properties to be requested, such as `get {"temperature": ""}`. This option allows setting the name of
this property. If set to `null`, disables querying for state. See 
[zigbee2mqtt/FRIENDLY_NAME/get](https://www.zigbee2mqtt.io/guide/usage/mqtt_topics_and_messages.html#zigbee2mqtt-friendly-name-get)
for details.

 * type: `string`
 * default: `"state"`

### `force_port_properties`

By default, exposed capabilities with category set to `config` or `diagnostic`, along with exposed options, will be
treated as attributes of the corresponding qToggle control port. This option allows specifying a list of property names
that will be associated to dedicated qToggle ports, regardless of their category (see
[zigbee2mqtt exposes](https://www.zigbee2mqtt.io/guide/usage/exposes.html#exposes) for details). Capability (property)
names are dot separated when they are part of a composite type (e.g. `color.hue`). Wildcards are also supported (e.g.
`color.*` will match all the properties inside the `color` composite type).

 * type: `[string]`
 * default: `[]`

### `force_attribute_properties`

By default, exposed capabilities with no category set will be treated as standalone qToggle ports. This option allows
specifying a list of property names that will become attributes of the corresponding control port, regardless of their
category (see
[zigbee2mqtt exposes](https://www.zigbee2mqtt.io/guide/usage/exposes.html#exposes) for details). Capability (property)
names are dot separated when they are part of a composite type (e.g. `color.hue`). Wildcards are also supported (e.g.
`color.*` will match all the properties inside the `color` composite type).

 * type: `[string]`
 * default: `[]`
