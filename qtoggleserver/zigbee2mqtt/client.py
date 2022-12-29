import asyncio
import logging
import re
import time

from typing import Any, Optional, Union

import asyncio_mqtt as aiomqtt

from qtoggleserver.core import ports as core_ports
from qtoggleserver.core.typing import GenericJSONDict, GenericJSONList
from qtoggleserver.peripherals import Peripheral, PeripheralPort
from qtoggleserver.utils import json as json_utils

from .exceptions import ClientNotConnected, ErrorResponse, RequestTimeout


class Zigbee2MQTTClient(Peripheral):
    DEFAULT_MQTT_PORT = 1883
    DEFAULT_MQTT_CLIENT_ID = 'qtoggleserver'
    DEFAULT_MQTT_BASE_TOPIC = 'zigbee2mqtt'
    DEFAULT_MQTT_RECONNECT_INTERVAL = 5  # seconds
    DEFAULT_BRIDGE_REQUEST_TIMEOUT = 10  # seconds

    # Used to adjust names of ports and attributes
    _NAME_MAPPING = {
        'linkquality': 'link_quality',
    }

    _TYPE_MAPPING = {
        'binary': core_ports.TYPE_BOOLEAN,
        'numeric': core_ports.TYPE_NUMBER,
        core_ports.TYPE_BOOLEAN: 'binary',
        core_ports.TYPE_NUMBER: 'numeric',
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

        self._mqtt_client: Optional[aiomqtt.Client] = None
        self._mqtt_base_topic_len: int = len(mqtt_base_topic)
        self._client_task: Optional[asyncio.Task] = None
        self._device_info_by_friendly_name: dict[str, GenericJSONDict] = {}
        self._device_state_by_friendly_name: dict[str, tuple[int, GenericJSONDict]] = {}
        self._device_online_by_friendly_name: dict[str, bool] = {}
        self._device_config_by_friendly_name: dict[str, GenericJSONDict] = {}
        self._bridge_info: Optional[GenericJSONDict] = None
        self._pending_requests: dict[str, dict[str, Any]] = {}
        self._update_ports_from_device_info_lock: asyncio.Lock = asyncio.Lock()

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
                    client_id=self.mqtt_client_id,
                    logger=self.mqtt_logger,
                ) as client:
                    self._mqtt_client = client
                    async with client.messages() as messages:
                        await client.subscribe(f'{self.mqtt_base_topic}/#')
                        async for message in messages:
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
        self._client_task = asyncio.create_task(self._client_loop())

    async def _stop_client_task(self) -> None:
        self._client_task.cancel()
        await self._client_task
        self._client_task = None

    async def handle_enable(self) -> None:
        if not self._client_task:
            self._start_client_task()

    async def handle_disable(self) -> None:
        if self._client_task:
            await self._stop_client_task()

    async def handle_cleanup(self) -> None:
        if self._client_task:
            await self._stop_client_task()

    async def handle_mqtt_message(self, topic: str, payload: bytes) -> None:
        try:
            payload_str = payload.decode()
            self.debug('incoming MQTT message on topic "%s": "%s"', topic, payload_str)
        except ValueError:
            payload_str = None
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

        await self.update_ports_from_device_info()

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
        await self.update_ports_from_device_info()

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
        self._device_online_by_friendly_name[friendly_name] = state == 'online'

    async def handle_device_state_message(self, friendly_name: str, payload_json: GenericJSONDict) -> None:
        processed_payload_json = {}
        for n, v in payload_json.items():
            n = self._NAME_MAPPING.get(n, n)
            processed_payload_json[n] = v

        self._device_state_by_friendly_name[friendly_name] = int(time.time()), processed_payload_json

    async def do_request(self, subtopic: str, payload_json: GenericJSONDict) -> tuple[str, GenericJSONDict]:
        if not self._mqtt_client:
            raise ClientNotConnected()

        transaction_id = self._make_transaction_id()
        payload_json = dict(payload_json)
        payload_json['transaction'] = transaction_id
        payload_str = json_utils.dumps(payload_json)

        topic = f'{self.mqtt_base_topic}/bridge/request/{subtopic}'
        self.debug('publishing MQTT message on topic "%s": "%s"', topic, payload_str)
        await self._mqtt_client.publish(topic, payload_str)

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

    async def set_device_property(self, friendly_name: str, property_name: str, value: Any) -> None:
        self.debug('setting device "%s" property "%s" to %s', friendly_name, property_name, json_utils.dumps(value))
        payload_json = {property_name: value}
        payload_str = json_utils.dumps(payload_json)

        topic = f'{self.mqtt_base_topic}/{friendly_name}/set'
        self.debug('publishing MQTT message on topic "%s": "%s"', topic, payload_str)
        await self._mqtt_client.publish(topic, payload_str)

    async def set_device_config(self, friendly_name: str, config: Any) -> None:
        self.debug('setting device "%s" config "%s"', friendly_name, json_utils.dumps(config))
        await self.do_request('device/options', {'id': friendly_name, 'options': config})

    def get_device_config(self, friendly_name: str) -> Optional[GenericJSONDict]:
        return self._device_config_by_friendly_name.get(friendly_name)

    def _make_transaction_id(self) -> str:
        return f'{self.mqtt_client_id}_{int(time.time() * 1000)}'

    def get_bridge_info(self) -> Optional[GenericJSONDict]:
        return self._bridge_info

    def get_device_info(self, friendly_name: str) -> Optional[GenericJSONDict]:
        return self._device_info_by_friendly_name.get(friendly_name)

    def get_device_state(self, friendly_name: str) -> Optional[tuple[int, GenericJSONDict]]:
        return self._device_state_by_friendly_name.get(friendly_name)

    def is_device_online(self, friendly_name: str) -> Optional[bool]:
        return self._device_online_by_friendly_name.get(friendly_name)

    async def set_device_enabled(self, friendly_name: str, enabled: bool) -> None:
        await self.set_device_config(friendly_name, {'qtoggleserver': {'enabled': enabled}})

    def is_device_enabled(self, friendly_name: str) -> bool:
        config = self.get_device_config(friendly_name)
        if not config:
            return False
        return config.get('qtoggleserver', {}).get('enabled', True)

    async def rename_device(self, old_friendly_name: str, new_friendly_name: str) -> None:
        self.debug('renaming device "%s" to "%s"', old_friendly_name, new_friendly_name)
        if re.match(r'^device_[a-f0-9]{16}$', old_friendly_name):
            old_friendly_name = f'0x{old_friendly_name[7:]}'

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

    async def make_port_args(self) -> list[dict[str, Any]]:
        return [
            {
                'driver': PermitJoinPort
            }
        ]

    async def update_ports_from_device_info(self) -> None:
        self.debug('updating ports from device info')
        async with self._update_ports_from_device_info_lock:
            port_args_list = self._port_args_from_device_info(self._device_info_by_friendly_name)
            ports_by_id = {p.get_initial_id(): p for p in self.get_device_ports()}
            port_args_list_by_id = {
                pa['id']: pa
                for pa in port_args_list
                if self.is_device_enabled(pa['device_friendly_name']) or issubclass(pa['driver'], DeviceControlPort)
            }

            # Remove all ports that no longer exist on the bridge
            for existing_id in ports_by_id:
                if existing_id not in port_args_list_by_id:
                    self.debug('port %s has been removed from the bridge', existing_id)
                    await self.remove_port(existing_id)

            # Add all ports that don't yet exist on the server
            for new_id, port_args in port_args_list_by_id.items():
                if new_id not in ports_by_id:
                    self.debug('new port %s detected', new_id)
                    await self.add_port(port_args)

    def _port_args_from_device_info(
        self,
        device_info_by_friendly_name: dict[str, GenericJSONDict],
    ) -> list[dict[str, Any]]:
        all_port_args_list = []
        for device_info in device_info_by_friendly_name.values():
            if not device_info.get('definition'):
                continue
            if device_info.get('type') != 'EndDevice':
                continue

            friendly_name = device_info['friendly_name']
            if re.match(r'^0x[a-f0-9]{16}$', friendly_name):
                friendly_name = f'device_{friendly_name[2:]}'

            # Build ports from exposed
            port_args_list = [
                {
                    'driver': DeviceControlPort,
                    'id': '',
                    'type': core_ports.TYPE_BOOLEAN,
                    'writable': True,
                    'device_friendly_name': friendly_name,
                    'property_name': '',
                }
            ]

            for exposed_info in device_info['definition'].get('exposes', []):
                name = exposed_info['name']
                name = self._NAME_MAPPING.get(name, name)
                type_ = self._TYPE_MAPPING.get(exposed_info['type'])
                if not type_:
                    continue
                port_args = {
                    'driver': DevicePort,
                    'id': name,
                    'display_name': name.replace('_', ' ').title(),
                    'type': type_,
                    'writable': bool(exposed_info['access'] & 2),
                    'unit': exposed_info.get('unit'),
                    'min': exposed_info.get('value_min'),
                    'max': exposed_info.get('value_max'),
                    'additional_attrdefs': {},
                    'device_friendly_name': friendly_name,
                    'property_name': name,
                    'value_on': exposed_info.get('value_on', True),
                    'value_off': exposed_info.get('value_off', False),
                }
                port_args_list.append(port_args)

            # Build additional attribute definitions from options
            for option_info in device_info['definition'].get('options', []):
                name = option_info['name']
                name = self._NAME_MAPPING.get(name, name)
                for pa in port_args_list:
                    if name.startswith(f'{pa["id"]}_'):
                        name = name[len(pa['id']) + 1:]
                        port_args = pa
                        break
                else:
                    port_args = port_args_list[0]

                type_ = self._TYPE_MAPPING.get(option_info['type'])
                if not type_:
                    continue
                port_args['additional_attrdefs'][name] = {
                    'display_name': name.replace('_', ' ').title(),
                    'description': option_info['description'],
                    'type': type_,
                    'modifiable': bool(option_info['access'] & 2),
                    'unit': option_info.get('unit'),
                    'min': option_info.get('value_min'),
                    'max': option_info.get('value_max'),
                    'persisted': False,
                }

            for pa in port_args_list:
                pa['id'] = f'{friendly_name}.{pa["id"]}' if pa['id'] else friendly_name

            all_port_args_list += port_args_list

        return all_port_args_list

    def get_device_ports(self, friendly_name: Optional[str] = None) -> list[PeripheralPort]:
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
