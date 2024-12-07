from __future__ import annotations

import asyncio
import logging
import re
import time

from typing import Any, Optional, Union

import aiomqtt

from qtoggleserver.core import ports as core_ports
from qtoggleserver.core.typing import GenericJSONDict, GenericJSONList
from qtoggleserver.peripherals import Peripheral
from qtoggleserver.utils import json as json_utils

from .exceptions import ClientNotConnected, ErrorResponse, RequestTimeout


class Zigbee2MQTTClient(Peripheral):
    DEFAULT_MQTT_PORT = 1883
    DEFAULT_MQTT_CLIENT_ID = 'qtoggleserver'
    DEFAULT_MQTT_BASE_TOPIC = 'zigbee2mqtt'
    DEFAULT_MQTT_RECONNECT_INTERVAL = 5  # seconds
    DEFAULT_BRIDGE_REQUEST_TIMEOUT = 10  # seconds
    DEFAULT_PERMIT_JOIN_TIMEOUT = 3600  # seconds

    _MAX_INCOMING_QUEUE_SIZE = 256
    _MAX_OUTGOING_QUEUE_SIZE = 256

    # Used to adjust names of ports and attributes
    _NAME_MAPPING = {
        'linkquality': 'link_quality',
    }
    _REV_NAME_MAPPING = {v: k for k, v in _NAME_MAPPING.items()}

    _PORT_TYPE_MAPPING = {
        'binary': core_ports.TYPE_BOOLEAN,
        'numeric': core_ports.TYPE_NUMBER,
        'enum': core_ports.TYPE_NUMBER,
    }

    _ATTR_TYPE_MAPPING = {
        'binary': 'boolean',  # TODO: use constants once they are defined in core
        'numeric': 'number',
        'enum': 'number',
        'text': 'string',
    }

    def __init__(
        self,
        *,
        mqtt_server: str,
        mqtt_port: int = DEFAULT_MQTT_PORT,
        mqtt_username: Optional[str] = None,
        mqtt_password: Optional[str] = None,
        mqtt_client_id: str = DEFAULT_MQTT_CLIENT_ID,
        mqtt_base_topic: str = DEFAULT_MQTT_BASE_TOPIC,
        mqtt_reconnect_interval: int = DEFAULT_MQTT_RECONNECT_INTERVAL,
        mqtt_logging: bool = False,
        bridge_logging: bool = False,
        bridge_request_timeout: int = DEFAULT_BRIDGE_REQUEST_TIMEOUT,
        permit_join_timeout: int = DEFAULT_PERMIT_JOIN_TIMEOUT,
        device_config: Optional[dict[str, GenericJSONDict]] = None,
        **kwargs,
    ) -> None:
        self.mqtt_server: str = mqtt_server
        self.mqtt_port: int = mqtt_port
        self.mqtt_username: Optional[str] = mqtt_username
        self.mqtt_password: Optional[str] = mqtt_password
        self.mqtt_client_id: str = mqtt_client_id
        self.mqtt_base_topic: str = mqtt_base_topic
        self.mqtt_reconnect_interval: int = mqtt_reconnect_interval
        self.mqtt_logging: bool = mqtt_logging
        self.bridge_logging: bool = bridge_logging
        self.bridge_request_timeout: int = bridge_request_timeout
        self.permit_join_timeout: int = permit_join_timeout
        self.static_device_config: dict[str, GenericJSONDict] = device_config or {}

        self._mqtt_client: Optional[aiomqtt.Client] = None
        self._mqtt_base_topic_len: int = len(mqtt_base_topic)
        self._client_task: Optional[asyncio.Task] = None
        self._device_info_by_friendly_name: dict[str, GenericJSONDict] = {}
        self._device_state_by_friendly_name: dict[str, tuple[int, GenericJSONDict]] = {}
        self._device_online_by_friendly_name: dict[str, bool] = {}
        self._device_config_by_friendly_name: dict[str, GenericJSONDict] = {}
        self._endpoints_by_friendly_name: dict[str, set] = {}  # used as a hack to get some property states
        self._safe_friendly_name_dict: dict[str, str] = {}
        self._bridge_info: Optional[GenericJSONDict] = None
        self._pending_requests: dict[str, dict[str, Any]] = {}
        self._update_ports_from_device_info_task: Optional[asyncio.Task] = None
        self._update_ports_from_device_info_scheduled: set[Union[DevicePort, None]] = set()

        super().__init__(**kwargs)

        self.mqtt_logger: logging.Logger = self.logger.getChild('mqtt')
        self.bridge_logger: logging.Logger = self.logger.getChild('bridge')

        if not self.mqtt_logging:
            self.mqtt_logger.setLevel(logging.CRITICAL)

    async def _client_loop(self) -> None:
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.mqtt_server,
                    port=self.mqtt_port,
                    username=self.mqtt_username,
                    password=self.mqtt_password,
                    identifier=self.mqtt_client_id,
                    logger=self.mqtt_logger,
                    max_queued_incoming_messages=self._MAX_INCOMING_QUEUE_SIZE,
                    max_queued_outgoing_messages=self._MAX_OUTGOING_QUEUE_SIZE,
                ) as client:
                    self._mqtt_client = client
                    await client.subscribe(f'{self.mqtt_base_topic}/#')
                    async for message in client.messages:
                        try:
                            await self.handle_mqtt_message(str(message.topic), message.payload)
                        except Exception:
                            self.error('failed to handle MQTT message', exc_info=True)
            except asyncio.CancelledError:
                self.debug('client task cancelled')
                self._mqtt_client = None
                break
            except Exception:
                self.error(
                    'MQTT client error; reconnecting in %s seconds', self.mqtt_reconnect_interval, exc_info=True
                )
                self._mqtt_client = None
                await asyncio.sleep(self.mqtt_reconnect_interval)

    def _start_client_task(self) -> None:
        if not self._client_task:
            self._client_task = asyncio.create_task(self._client_loop())

    def _start_update_ports_from_device_info_task(self) -> None:
        if not self._update_ports_from_device_info_task:
            self._update_ports_from_device_info_task = asyncio.create_task(
                self._update_ports_from_device_info_loop()
            )

    async def _stop_client_task(self) -> None:
        if self._client_task:
            self._client_task.cancel()
            await self._client_task
            self._client_task = None

    async def _stop_update_ports_from_device_info_task(self) -> None:
        if self._update_ports_from_device_info_task:
            self._update_ports_from_device_info_task.cancel()
            await self._update_ports_from_device_info_task
            self._update_ports_from_device_info_task = None

    async def handle_enable(self) -> None:
        self._start_client_task()
        self._start_update_ports_from_device_info_task()

    async def handle_disable(self) -> None:
        await self._stop_client_task()
        await self._stop_update_ports_from_device_info_task()

    async def handle_cleanup(self) -> None:
        await self._stop_client_task()
        await self._stop_update_ports_from_device_info_task()

    async def publish_mqtt_message(self, topic: str, payload: Any) -> None:
        if isinstance(payload, str):
            payload_str = payload
        else:
            payload_str = json_utils.dumps(payload)

        if self.mqtt_logging:
            self.debug('publishing MQTT message on topic "%s": "%s"', topic, payload_str)
        await self._mqtt_client.publish(topic, payload_str)

    async def handle_mqtt_message(self, topic: str, payload: bytes) -> None:
        try:
            payload_str = payload.decode()
            if self.mqtt_logging:
                self.debug('incoming MQTT message on topic "%s": "%s"', topic, payload_str)
        except ValueError:
            payload_str = None
            if self.mqtt_logging:
                self.debug('incoming MQTT message on topic "%s": "%s"', topic, payload)

        try:
            payload_json = json_utils.loads(payload)
        except ValueError:
            payload_json = None

        if topic.startswith(f'{self.mqtt_base_topic}/bridge'):
            await self.handle_bridge_message(topic[self._mqtt_base_topic_len + 8:], payload_str, payload_json)
        else:
            parts = topic[self._mqtt_base_topic_len + 1:].split('/', 1)
            if len(parts) == 2:
                friendly_name, subtopic = parts
            else:
                friendly_name, subtopic = parts[0], None
            await self.handle_device_message(friendly_name, subtopic, payload_str, payload_json)

    async def handle_bridge_message(
        self,
        subtopic: str,
        payload_str: Optional[str],
        payload_json: Union[GenericJSONDict, GenericJSONList, None],
    ) -> None:
        if subtopic == 'state':
            await self.handle_bridge_state_message(payload_str, payload_json)
        elif subtopic == 'info':
            await self.handle_bridge_info_message(payload_json)
        elif subtopic == 'logging':
            await self.handle_bridge_logging_message(payload_json)
        elif subtopic == 'log':
            await self.handle_bridge_log_message(payload_json)
        elif subtopic == 'config':
            await self.handle_bridge_config_message(payload_json)
        elif subtopic == 'devices':
            await self.handle_bridge_devices_message(payload_json)
        elif subtopic == 'groups':
            await self.handle_bridge_groups_message(payload_json)
        elif subtopic == 'event':
            await self.handle_bridge_event_message(payload_json)
        elif subtopic == 'extensions':
            await self.handle_bridge_extensions_message(payload_json)
        elif subtopic.startswith('definitions'):
            pass  # Ignore clusters definitions
        elif subtopic.startswith('request/'):
            pass  # Ignore request messages (normally sent by ourselves)
        elif subtopic.startswith('response/'):
            await self.handle_bridge_response_message(subtopic[9:], payload_json)
        else:
            self.warning('got MQTT bridge message on unexpected subtopic "%s"', subtopic)

    async def handle_bridge_state_message(
        self,
        payload_str: Optional[str],
        payload_json: Optional[GenericJSONDict]
    ) -> None:
        if payload_str is not None:
            state = payload_str
        else:
            state = payload_json['state']

        self.debug('bridge state is now "%s"', state)
        self.set_online(state == 'online')

    async def handle_bridge_info_message(self, payload_json: GenericJSONDict) -> None:
        self._bridge_info = payload_json

        self._device_config_by_friendly_name.clear()
        devices_config = payload_json.get('config', {}).get('devices')
        for ieee_address, config in devices_config.items():
            friendly_name = config.get('friendly_name', ieee_address)
            config['ieee_address'] = ieee_address
            self._device_config_by_friendly_name[friendly_name] = config

        self.update_ports_from_device_info_asap()

    async def handle_bridge_logging_message(self, payload_json: GenericJSONDict) -> None:
        if not self.bridge_logging:
            return
        level_str = payload_json['level']
        level = logging.getLevelName(level_str.upper())
        message = payload_json['message']
        self.bridge_logger.log(level, message)

    async def handle_bridge_log_message(self, payload_json: GenericJSONDict) -> None:
        pass

    async def handle_bridge_config_message(self, payload_json: GenericJSONDict) -> None:
        pass

    async def handle_bridge_devices_message(self, payload_json: GenericJSONList) -> None:
        self._device_info_by_friendly_name = {d['friendly_name']: d for d in payload_json}
        self.update_ports_from_device_info_asap()

    async def handle_bridge_groups_message(self, payload_json: GenericJSONList) -> None:
        pass

    async def handle_bridge_event_message(self, payload_json: GenericJSONDict) -> None:
        pass

    async def handle_bridge_extensions_message(self, payload_json: GenericJSONList) -> None:
        pass

    async def handle_bridge_response_message(self, subtopic: str, payload_json: GenericJSONDict) -> None:
        data = payload_json['data']
        error = payload_json.get('error')
        transaction = payload_json.get('transaction')
        if not transaction:
            self.debug('received response without transaction "%s"', transaction)
            return  # not our response, as we always request a transaction

        if transaction not in self._pending_requests:
            self.debug('received response with unknown transaction "%s"', transaction)
            return  # not our response (or arrived too late)

        condition = self._pending_requests[transaction]['condition']
        async with condition:
            self._pending_requests[transaction]['response'] = (subtopic, error, data)
            condition.notify()

    async def handle_device_message(
        self,
        friendly_name: str,
        subtopic: str,
        payload_str: Optional[str],
        payload_json: Optional[GenericJSONDict]
    ) -> None:
        # We receive an empty payload when renaming a device
        if not payload_str and not payload_json:
            return

        if subtopic == 'availability':
            await self.handle_device_availability_message(friendly_name, payload_str, payload_json)
        elif subtopic == 'get':
            await self.handle_device_get_message(friendly_name, payload_json)
        elif subtopic == 'set':
            await self.handle_device_set_message(friendly_name, payload_json)
        elif not subtopic:
            await self.handle_device_state_message(friendly_name, payload_json)
        else:
            self.warning('got MQTT device message from "%s" on unexpected subtopic "%s"', friendly_name, subtopic)

    async def handle_device_availability_message(
        self,
        friendly_name: str,
        payload_str: Optional[str],
        payload_json: Optional[GenericJSONDict]
    ) -> None:
        if payload_str is not None:
            state = payload_str
        else:
            state = payload_json['state']

        self.debug('device "%s" is now "%s"', friendly_name, state)
        self.trigger_port_update_fire_and_forget()
        self._device_online_by_friendly_name[friendly_name] = (state == 'online')

    async def handle_device_get_message(self, friendly_name: str, payload_json: GenericJSONDict) -> None:
        pass

    async def handle_device_set_message(self, friendly_name: str, payload_json: GenericJSONDict) -> None:
        pass

    async def handle_device_state_message(self, friendly_name: str, payload_json: GenericJSONDict) -> None:
        self.debug('got device "%s" state: "%s"', friendly_name, json_utils.dumps(payload_json))
        processed_payload_json = {}
        for n, v in payload_json.items():
            n = self._NAME_MAPPING.get(n, n)
            processed_payload_json[n] = v

        _, state = self._device_state_by_friendly_name.get(friendly_name, (0, {}))
        old_state = dict(state)
        state.update(processed_payload_json)
        self._device_state_by_friendly_name[friendly_name] = int(time.time()), state

        device_ports = self.get_device_ports(friendly_name)

        # Gather all properties that represent port values
        value_properties = set()
        for port in device_ports:
            if not isinstance(port, DeviceControlPort):
                value_properties.add(port.get_property_name())

        # Gather all properties that have just changed
        changed_properties = (
            set(k for k, v in state.items() if v != old_state.get(k)) |
            set(k for k, v in old_state.items() if v != state.get(k))
        )

        # If only value properties have changed, don't trigger unnecessary `port-update` events
        only_values_changed = not (changed_properties - value_properties)
        if only_values_changed:
            return

        # Trigger `port-update` on all affected ports
        for port in device_ports:
            attr_properties = set(await port.get_additional_attrdefs())
            if not (attr_properties & changed_properties):
                continue
            port.invalidate_attrs()
            if port.is_enabled():
                await port.trigger_update()
                port.save_asap()

    async def do_request(self, subtopic: str, payload_json: GenericJSONDict) -> tuple[str, GenericJSONDict]:
        if not self._mqtt_client:
            raise ClientNotConnected()

        topic = f'{self.mqtt_base_topic}/bridge/request/{subtopic}'
        transaction_id = self._make_transaction_id()
        payload_json = dict(payload_json)
        payload_json['transaction'] = transaction_id
        await self.publish_mqtt_message(topic, payload_json)

        condition = asyncio.Condition()
        self._pending_requests[transaction_id] = {
            'response': None,
            'condition': condition
        }

        async with condition:
            try:
                await asyncio.wait_for(condition.wait(), timeout=self.bridge_request_timeout)
                subtopic, error, data = self._pending_requests[transaction_id]['response']
                if error:
                    raise ErrorResponse(error)
                return data
            except asyncio.TimeoutError:
                self.error('timeout waiting for response on subtopic "%s"', subtopic)
                raise RequestTimeout(f'Timeout waiting for response on subtopic "{subtopic}"')
            finally:
                self._pending_requests.pop(transaction_id, None)

    async def query_device_state(self, friendly_name: str, properties: Optional[list[str]]) -> None:
        config = self.get_device_config(friendly_name)
        topic = f'{self.mqtt_base_topic}/{friendly_name}/get'
        state_property = config.get('get_state_property', 'state')
        if not state_property:
            return  # state querying explicitly disabled

        self.debug('querying device "%s" state', friendly_name)
        payload_json = {state_property: ''}
        if properties:
            payload_json.update({p: '' for p in properties})

        await self.publish_mqtt_message(topic, payload_json)

    async def set_device_property(self, friendly_name: str, property_name: str, value: Any) -> None:
        self.debug('setting device "%s" property "%s" to %s', friendly_name, property_name, json_utils.dumps(value))
        topic = f'{self.mqtt_base_topic}/{friendly_name}/set'
        property_name = self._REV_NAME_MAPPING.get(property_name, property_name)
        payload_json = {property_name: value}
        await self.publish_mqtt_message(topic, payload_json)

    async def set_device_config(self, friendly_name: str, config: Any) -> None:
        self.debug('setting device "%s" config "%s"', friendly_name, json_utils.dumps(config))
        await self.do_request('device/options', {'id': friendly_name, 'options': config})

    def get_device_config(self, friendly_name: str) -> GenericJSONDict:
        return (
            self.static_device_config.get(friendly_name, {}) |
            self._device_config_by_friendly_name.get(friendly_name, {})
        )

    def _make_transaction_id(self) -> str:
        return f'{self.mqtt_client_id}_{int(time.time() * 1000)}'

    def get_bridge_info(self) -> Optional[GenericJSONDict]:
        return self._bridge_info

    async def set_permit_join(self, value: bool) -> None:
        await self.do_request('permit_join', {'value': value, 'time': self.permit_join_timeout})

    def is_permit_join(self) -> Optional[bool]:
        if not self._bridge_info:
            return

        return self._bridge_info.get('permit_join', False)

    def get_device_info(self, friendly_name: str) -> Optional[GenericJSONDict]:
        return self._device_info_by_friendly_name.get(friendly_name)

    def get_device_safe_friendly_name(self, friendly_name: str) -> str:
        return self._safe_friendly_name_dict.get(friendly_name, friendly_name)

    def get_device_property(self, friendly_name: str, property_name: str) -> Any:
        timestamped_state = self._device_state_by_friendly_name.get(friendly_name)
        if not timestamped_state:
            return

        timestamp, state = timestamped_state
        value = state.get(property_name)
        if value is None:
            # Try the variant where the property name is suffixed with one of the endpoints in the state dict,
            # such as `power_on_behavior_l1`, but the exposed property name is simply `power_on_behavior`.
            for endpoint in self._endpoints_by_friendly_name.get(friendly_name, []):
                value = state.get(f'{property_name}_{endpoint}')
                if value is not None:
                    break

        if value is None and property_name == 'address':
            device_info = self._device_info_by_friendly_name.get(friendly_name, {})
            value = device_info.get('ieee_address')

        return value

    def is_device_online(self, friendly_name: str) -> Optional[bool]:
        return self._device_online_by_friendly_name.get(friendly_name, False)

    async def set_device_enabled(self, friendly_name: str, enabled: bool) -> None:
        await self.set_device_config(friendly_name, {'qtoggleserver': {'enabled': enabled}})

    def is_control_port_enabled(self, friendly_name: str) -> bool:
        safe_friendly_name = self.get_device_safe_friendly_name(friendly_name)
        control_port = self.get_port(safe_friendly_name)
        if not control_port:
            return False
        if not control_port.is_enabled():
            return False

        return True

    def is_device_enabled(self, friendly_name: str) -> bool:
        # First ensure our device control port is enabled
        if not self.is_control_port_enabled(friendly_name):
            return False

        config = self.get_device_config(friendly_name)
        return config.get('qtoggleserver', {}).get('enabled', False)

    async def rename_device(self, old_friendly_name: str, new_friendly_name: str) -> None:
        self.debug('renaming device "%s" to "%s"', old_friendly_name, new_friendly_name)

        device_dicts = [
            self._device_online_by_friendly_name,
            self._device_config_by_friendly_name,
            self._device_info_by_friendly_name,
            self._device_state_by_friendly_name,
        ]
        for device_dict in device_dicts:
            if old_friendly_name in device_dict:
                device_dict[new_friendly_name] = device_dict[old_friendly_name]

        await self.do_request('device/rename', {'from': old_friendly_name, 'to': new_friendly_name})

    async def remove_device(self, friendly_name: str) -> None:
        self.debug('removing device "%s"', friendly_name)

        await self.do_request('device/remove', {'id': friendly_name, 'force': True})

    async def make_port_args(self) -> list[dict[str, Any]]:
        return [
            {
                'driver': PermitJoinPort
            }
        ]

    def update_ports_from_device_info_asap(self, changed_port: Optional[DevicePort] = None) -> None:
        if changed_port:
            self.debug('will update ports from device info asap (changed port = %s)', changed_port)
        else:
            self.debug('will update ports from device info asap')
        self._update_ports_from_device_info_scheduled.add(changed_port)

    async def _update_ports_from_device_info_loop(self) -> None:
        try:
            while True:
                try:
                    if self._update_ports_from_device_info_scheduled:
                        self._update_ports_from_device_info_scheduled.clear()
                        await self._update_ports_from_device_info()
                except Exception:
                    self.error('error while updating ports from device info', exc_info=True)

                await asyncio.sleep(1)
        except asyncio.CancelledError:
            self.debug('updating ports from device info task cancelled', exc_info=True)

    async def _update_ports_from_device_info(self) -> None:
        self.debug('updating ports from device info')
        port_args_list = self._port_args_from_device_info(self._device_info_by_friendly_name)
        ports_by_id = {p.get_initial_id(): p for p in self.get_device_ports()}
        port_args_by_id = {
            pa['id']: pa
            for pa in port_args_list
            if self.is_device_enabled(pa['device_friendly_name']) or issubclass(pa['driver'], DeviceControlPort)
        }
        port_args_list_by_friendly_name: dict[str, list[dict]] = {}
        for port_args in port_args_list:
            port_args_list_by_friendly_name.setdefault(port_args['device_friendly_name'], []).append(port_args)

        # Remove all ports that no longer exist on the bridge
        for existing_id in ports_by_id:
            if existing_id not in port_args_by_id:
                self.debug('port %s has been removed from the bridge', existing_id)
                await self.remove_port(existing_id)

        # Add all ports that don't yet exist on the server
        changed_friendly_names = set(
            p.get_device_friendly_name()
            for p in self._update_ports_from_device_info_scheduled
            if p
        )
        for new_id, port_args in port_args_by_id.items():
            if new_id not in ports_by_id:
                self.debug('new port %s detected', new_id)
                changed_friendly_names.add(port_args['device_friendly_name'])
                await self.add_port(port_args)

        if self._mqtt_client:
            for friendly_name, online in list(self._device_online_by_friendly_name.items()):
                if not online:
                    continue
                if friendly_name not in changed_friendly_names:
                    continue
                if not self.is_control_port_enabled(friendly_name):
                    continue
                pal = port_args_list_by_friendly_name.get(friendly_name, [])
                properties = set()
                for port_args in pal:
                    attrdefs = port_args.get('additional_attrdefs', {})
                    properties = properties.union(self._REV_NAME_MAPPING.get(a, a) for a in attrdefs)
                    if port_args.get('property_name'):
                        properties.add(port_args['property_name'])
                await self.query_device_state(friendly_name, list(properties))

    def _port_args_from_device_info(
        self,
        device_info_by_friendly_name: dict[str, GenericJSONDict],
    ) -> list[dict[str, Any]]:
        all_port_args_list = []
        for device_info in device_info_by_friendly_name.values():
            if not device_info.get('definition'):
                continue
            if device_info.get('type') not in ('EndDevice', 'Router'):
                continue

            friendly_name = device_info['friendly_name']

            # Ensure we have a device friendly name that's safe for being used as a port id.
            safe_friendly_name = friendly_name
            if re.match(r'^0x[a-f0-9]{16}$', safe_friendly_name):
                safe_friendly_name = f'device_{safe_friendly_name[2:]}'
            self._safe_friendly_name_dict[friendly_name] = safe_friendly_name

            endpoints = self._endpoints_by_friendly_name[friendly_name] = set()

            device_config = self.get_device_config(friendly_name)
            force_attribute_properties = device_config.get('force_attribute_properties', set())
            force_port_properties = device_config.get('force_port_properties', set())

            # Build ports from exposed
            control_port_args = {
                'driver': DeviceControlPort,
                'id': safe_friendly_name,
                'type': core_ports.TYPE_BOOLEAN,
                'writable': True,
                'device_friendly_name': friendly_name,
                'property_name': '',
                'additional_attrdefs': {},
            }

            # Using a dict instead of a list here ensures unique property names (and thus ids), only considering the
            # latest entry.
            port_args_by_id: dict[str, dict] = {}

            # Build ports from features
            exposed_non_features = []
            force_attribute_features = []
            for exposed_info in device_info['definition'].get('exposes', []):
                if 'endpoint' in exposed_info:
                    endpoints.add(exposed_info['endpoint'])

                features = exposed_info.get('features', [])
                exposed_name = exposed_info.get('property') or exposed_info.get('name')
                if exposed_name in force_port_properties:
                    # If this exposed info has been forced as a port, consider it a feature itself
                    features.append(exposed_info)
                if not features:
                    # Exposed info without features becomes attribute of control port
                    exposed_non_features.append(exposed_info)
                    continue

                for feature in features:
                    name = feature.get('property') or feature.get('name')
                    if not name:
                        continue
                    name = self._NAME_MAPPING.get(name, name)
                    type_ = self._PORT_TYPE_MAPPING.get(feature.get('type'))
                    if not type_:
                        continue
                    if name in force_attribute_properties:
                        feature['exposed_name'] = exposed_name
                        force_attribute_features.append(feature)
                        continue

                    port_args = {
                        'driver': DevicePort,
                        'id': name,
                        'display_name': name.replace('_', ' ').title(),
                        'type': type_,
                        'writable': bool(feature['access'] & 2),
                        'unit': feature.get('unit'),
                        'min': feature.get('value_min'),
                        'max': feature.get('value_max'),
                        'additional_attrdefs': {},
                        'device_friendly_name': friendly_name,
                        'property_name': name,
                        'property_group_name': exposed_name,
                        'value_on': feature.get('value_on', True),
                        'value_off': feature.get('value_off', False),
                        'values': feature.get('values')
                    }
                    port_args_by_id[port_args['id']] = port_args

            # Build additional attribute definitions from options and exposed non-features
            for info in exposed_non_features + force_attribute_features:
                name = info.get('property') or info.get('name')
                name = self._NAME_MAPPING.get(name, name)
                if name in force_port_properties:
                    continue

                type_ = self._ATTR_TYPE_MAPPING.get(info['type'])
                if not type_:
                    continue

                choices = None
                values = info.get('values')
                values_dict = None
                if values:
                    choices = [
                        {
                            'value': i + 1,
                            'display_name': ' '.join(x.capitalize() for x in v.split('_'))
                        }
                        for i, v in enumerate(values)
                    ]
                    values_dict = {v: i for i, v in enumerate(values)}

                attrdef = {
                    'display_name': name.replace('_', ' ').title(),
                    'type': type_,
                    'modifiable': bool(info.get('access', 0) & 2),
                    'persisted': False,
                    'property_group_name': info.get('exposed_name'),
                }
                if info.get('description'):
                    attrdef['description'] = info['description']
                if info.get('unit'):
                    attrdef['unit'] = info['unit']
                if info.get('value_min') is not None:
                    attrdef['min'] = info['value_min']
                if info.get('value_max') is not None:
                    attrdef['max'] = info['value_max']
                if type_ == 'boolean':
                    attrdef['_value_on'] = info.get('value_on', True)
                    attrdef['_value_off'] = info.get('value_off', False)
                if values:
                    attrdef['_values'] = values
                    attrdef['_values_dict'] = values_dict
                    attrdef['choices'] = choices

                control_port_args['additional_attrdefs'][name] = attrdef

            # Ensure port id prefix
            for pa in port_args_by_id.values():
                pa['id'] = f'{safe_friendly_name}.{pa["id"]}'

            all_port_args_list += [control_port_args] + list(port_args_by_id.values())

        return all_port_args_list

    def get_device_ports(self, friendly_name: Optional[str] = None) -> list[DevicePort]:
        if friendly_name:
            return [
                p for p in self.get_ports()
                if isinstance(p, DevicePort) and (
                    p.get_initial_id().startswith(f'{friendly_name}.') or
                    p.get_initial_id() == friendly_name
                )
            ]
        else:
            return [p for p in self.get_ports() if isinstance(p, DevicePort)]


from .ports import PermitJoinPort, DeviceControlPort, DevicePort  # noqa: E402
