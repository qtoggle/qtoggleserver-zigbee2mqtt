import abc
import asyncio

from typing import cast, Any, Optional

from qtoggleserver.peripherals import PeripheralPort
from qtoggleserver.core import ports as core_ports
from qtoggleserver.core.typing import AttributeDefinitions, NullablePortValue, PortValue
from qtoggleserver.utils import asyncio as asyncio_utils

from .client import Zigbee2MQTTClient


class Zigbee2MQTTPort(PeripheralPort, metaclass=abc.ABCMeta):
    def get_peripheral(self) -> Zigbee2MQTTClient:
        return cast(Zigbee2MQTTClient, super().get_peripheral())


class PermitJoinPort(Zigbee2MQTTPort):
    ID = 'permit_join'
    TYPE = core_ports.TYPE_BOOLEAN
    WRITABLE = True
    DISPLAY_NAME = 'Permit Join'

    async def read_value(self) -> NullablePortValue:
        bridge_info = self.get_peripheral().get_bridge_info()
        if not bridge_info:
            return None

        return bridge_info.get('permit_join', False)

    async def write_value(self, value: PortValue) -> None:
        await self.get_peripheral().do_request('permit_join', {'value': value})


class DevicePort(Zigbee2MQTTPort):
    def __init__(
        self,
        *,
        id: str,
        display_name: str = '',
        type: str,
        writable: bool,
        unit: Optional[str] = None,
        min: Optional[float] = None,
        max: Optional[float] = None,
        additional_attrdefs: Optional[AttributeDefinitions] = None,
        device_friendly_name: str,
        property_name: str,
        value_on: Any = True,
        value_off: Any = False,
        **kwargs,
    ) -> None:
        super().__init__(id=id, **kwargs)

        # Following properties will dictate the initial value of their corresponding port attributes
        self._display_name: str = display_name
        self._type: str = type
        self._writable: bool = writable
        self._unit: Optional[str] = unit
        self._min: Optional[float] = min
        self._max: Optional[float] = max

        self._additional_attrdefs: AttributeDefinitions = additional_attrdefs or {}
        self._device_friendly_name: str = device_friendly_name
        self._property_name: str = property_name
        self._value_on: Any = value_on
        self._value_off: Any = value_off

    def _get_additional_attrdefs(self) -> AttributeDefinitions:
        return self._additional_attrdefs

    ADDITIONAL_ATTRDEFS = property(_get_additional_attrdefs)

    async def read_value(self) -> NullablePortValue:
        timestamped_state = self.get_peripheral().get_device_state(self.get_device_friendly_name())
        if not timestamped_state:
            return

        timestamp, state = timestamped_state
        value = state.get(self.get_property_name())
        if value is None:
            return

        if await self.get_type() == core_ports.TYPE_BOOLEAN:
            return value == self._value_on
        else:
            return value

    async def write_value(self, value: PortValue) -> None:
        if await self.get_type() == core_ports.TYPE_BOOLEAN:
            if value:
                value = self._value_on
            else:
                value = self._value_off

        await self.get_peripheral().set_device_property(
            self.get_device_friendly_name(), self.get_property_name(), value
        )

    async def attr_is_online(self) -> bool:
        return (
            self.get_peripheral().is_device_online(self._device_friendly_name) and
            await super().attr_is_online()
        )

    def get_device_friendly_name(self) -> str:
        return self._device_friendly_name

    def get_property_name(self) -> str:
        return self._property_name


class DeviceControlPort(DevicePort):
    ADDITIONAL_ATTRDEFS = {
        'friendly_name': {
            'display_name': 'Friendly Name',
            'description': 'Use this attribute to rename your Zigbee device',
            'type': 'string',
            'modifiable': True,
            'persisted': False,
        }
    }

    STANDARD_ATTRDEFS = dict(DevicePort.STANDARD_ATTRDEFS)
    STANDARD_ATTRDEFS['display_name']['persisted'] = False

    _MAX_RENAME_ATTEMPTS = 10

    async def enable_renamed_ports(self, enabled_port_ids: set[str], new_friendly_name: str, attempt: int = 1) -> None:
        self.debug('enabling renamed ports: %s (attempt %d)', ', '.join(enabled_port_ids), attempt)

        ports = self.get_peripheral().get_device_ports(new_friendly_name)
        if not ports:
            if attempt < self._MAX_RENAME_ATTEMPTS:
                self.debug('renamed ports not added yet, retrying later')
                await asyncio.sleep(1)
                await self.enable_renamed_ports(enabled_port_ids, new_friendly_name, attempt + 1)
            else:
                self.error('timeout waiting for renamed ports to be added')
            return

        for port in ports:
            if port.get_initial_id() in enabled_port_ids:
                await port.enable()
                port.invalidate_attrs()
                await port.trigger_update()
                await port.save()

    async def attr_set_friendly_name(self, value: str) -> None:
        old_device_friendly_name = self.get_device_friendly_name()

        # Remember enabled ports before they are renamed (practically removed and re-added)
        all_enabled_port_ids = [p.get_initial_id() for p in self.get_peripheral().get_device_ports() if p.is_enabled()]
        device_enabled_port_ids = []
        for port_id in all_enabled_port_ids:
            if port_id == old_device_friendly_name:
                port_id = value
            elif port_id.startswith(f'{old_device_friendly_name}.'):
                port_id = f'{value}.{port_id[len(old_device_friendly_name) + 1:]}'
            else:
                continue
            device_enabled_port_ids.append(port_id)

        # Delay the actual renaming call a bit, so that this attribute setter completes and triggers the `port-update`
        # event for this port *before* it is removed.
        asyncio.create_task(
            asyncio_utils.await_later(1, self.get_peripheral().rename_device(old_device_friendly_name, value))
        )

        asyncio.create_task(
            asyncio_utils.await_later(2, self.enable_renamed_ports(set(device_enabled_port_ids), value))
        )

    async def attr_get_friendly_name(self) -> str:
        return self.get_device_friendly_name()

    async def read_value(self) -> NullablePortValue:
        return self.get_peripheral().is_device_enabled(self.get_device_friendly_name())

    async def write_value(self, value: PortValue) -> None:
        await self.get_peripheral().set_device_enabled(self.get_device_friendly_name(), value)

    async def attr_get_display_name(self) -> str:
        config = self.get_peripheral().get_device_config(self.get_device_friendly_name()) or {}
        info = self.get_peripheral().get_device_info(self.get_device_friendly_name()) or {}
        return config.get('description', info.get('definition', {}).get('description', ''))

    async def attr_set_display_name(self, value: str) -> None:
        await self.get_peripheral().set_device_config(self.get_device_friendly_name(), {'description': value})

    # This disables the persisted attribute
    async def attr_is_persisted(self) -> None:
        return None
