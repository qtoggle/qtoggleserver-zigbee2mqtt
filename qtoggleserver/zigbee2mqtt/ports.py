import abc
import asyncio
import copy

from typing import cast, Any, Optional

from qtoggleserver.peripherals import PeripheralPort
from qtoggleserver.core import ports as core_ports
from qtoggleserver.core.typing import Attribute, AttributeDefinitions, NullablePortValue, PortValue
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
    PERSISTED = None

    async def read_value(self) -> NullablePortValue:
        return self.get_peripheral().is_permit_join()

    @core_ports.skip_write_unavailable
    async def write_value(self, value: bool) -> None:
        await self.get_peripheral().set_permit_join(value)


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
        property_group_name: str = None,
        value_on: Any = True,
        value_off: Any = False,
        values: Optional[list[str]] = None,
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
        self._property_group_name: Optional[str] = property_group_name
        self._value_on: Any = value_on
        self._value_off: Any = value_off
        self._values: Optional[list[str]] = values

        if values:
            # This will determine the actual `choices` attribute value
            self._choices = [
                {
                    'value': i + 1,
                    'display_name': ' '.join(x.capitalize() for x in v.split('_'))
                }
                for i, v in enumerate(values)
            ]
            self._values_dict: dict[str, int] = {v: i for i, v in enumerate(values)}

    async def get_additional_attrdefs(self) -> AttributeDefinitions:
        return self.ADDITIONAL_ATTRDEFS | self._additional_attrdefs

    async def read_value(self) -> NullablePortValue:
        value = self.get_property_value(self.get_property_name(), self._property_group_name)

        if await self.get_type() == core_ports.TYPE_BOOLEAN:
            value = (value == self._value_on)
        elif self._values:  # map Z2M value to choice
            try:
                value = self._values_dict[value] + 1
            except KeyError:
                value = None

        return value

    @core_ports.skip_write_unavailable
    async def write_value(self, value: PortValue) -> None:
        if await self.get_type() == core_ports.TYPE_BOOLEAN:
            if value:
                value = self._value_on
            else:
                value = self._value_off
        elif self._values:  # map choice to Z2M value
            try:
                value = self._values[value - 1]
            except IndexError:
                raise ValueError(f'Invalid choice: {value}')

        await self.set_property_value(self.get_property_name(), value, self._property_group_name)

    async def attr_is_online(self) -> bool:
        return (
            self.get_peripheral().is_device_online(self._device_friendly_name)
            and await super().attr_is_online()
        )

    def get_device_friendly_name(self) -> str:
        return self._device_friendly_name

    def get_device_safe_friendly_name(self) -> str:
        return self.get_peripheral().get_device_safe_friendly_name(self._device_friendly_name)

    def get_property_name(self) -> str:
        return self._property_name

    def get_property_value(self, property_name: str, property_group_name: Optional[str] = None) -> Any:
        # First look for a group value
        group_value = None
        if property_group_name:
            group_value = self.get_peripheral().get_device_property(
                self.get_device_friendly_name(), property_group_name
            )
            if not isinstance(group_value, dict):
                group_value = None

        value = None
        if group_value:
            value = group_value.get(property_name)

        # Then look for a direct value
        if value is None:
            value = self.get_peripheral().get_device_property(self.get_device_friendly_name(), property_name)

        return value

    async def set_property_value(
        self, property_name: str, value: Any, property_group_name: Optional[str] = None
    ) -> None:
        if property_group_name:
            await self.get_peripheral().set_device_property(
                self.get_device_friendly_name(), property_group_name, {property_name: value}
            )
        else:
            await self.get_peripheral().set_device_property(self.get_device_friendly_name(), property_name, value)


class DeviceControlPort(DevicePort):
    ADDITIONAL_ATTRDEFS = {
        'friendly_name': {
            'display_name': 'Friendly Name',
            'description': 'Use this attribute to rename your Zigbee device. Set to empty string to remove the device.',
            'type': 'string',
            'modifiable': True,
            'persisted': False,
        },
        'address': {
            'display_name': 'Address',
            'description': 'Zigbee IEEE Address',
            'type': 'string',
            'modifiable': False,
        }
    }

    STANDARD_ATTRDEFS = copy.deepcopy(DevicePort.STANDARD_ATTRDEFS)
    STANDARD_ATTRDEFS['display_name']['persisted'] = False

    PERSISTED = None

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
        current_friendly_name = self.get_device_friendly_name()
        current_safe_friendly_name = self.get_device_safe_friendly_name()

        if value:
            # Remember enabled ports before they are renamed (practically removed and re-added)
            all_enabled_port_ids = [
                p.get_initial_id() for p in self.get_peripheral().get_device_ports() if p.is_enabled()
            ]
            device_enabled_port_ids = []
            for port_id in all_enabled_port_ids:
                if port_id == current_safe_friendly_name:
                    port_id = value
                elif port_id.startswith(f'{current_safe_friendly_name}.'):
                    port_id = f'{value}.{port_id[len(current_safe_friendly_name) + 1:]}'
                else:
                    continue
                device_enabled_port_ids.append(port_id)

            # Delay the actual renaming call a bit, so that this attribute setter completes and triggers the
            # `port-update` event for this port *before* it is removed.

            asyncio_utils.fire_and_forget(
                asyncio_utils.await_later(1, self.get_peripheral().rename_device(current_friendly_name, value))
            )
            asyncio_utils.fire_and_forget(
                asyncio_utils.await_later(2, self.enable_renamed_ports(set(device_enabled_port_ids), value))
            )
        else:  # empty `value` means removal
            # Delay the actual removing call a bit, so that this attribute setter completes and triggers the
            # `port-update` event for this port *before* it is removed.

            asyncio_utils.fire_and_forget(
                asyncio_utils.await_later(1, self.get_peripheral().remove_device(current_friendly_name))
            )

    async def attr_get_friendly_name(self) -> str:
        return self.get_device_friendly_name()

    async def attr_get_value(self, name: str) -> Optional[Attribute]:
        attrdefs = await self.get_attrdefs()
        attrdef = attrdefs.get(name)
        if not attrdef:
            return None

        value = self.get_property_value(name, attrdef.get('property_group_name'))
        if value is None:
            return None

        if attrdef.get('type') == 'boolean':
            value = (value == attrdef.get('_value_on', True))
        elif attrdef.get('_values'):  # map Z2M value to choice
            try:
                value = attrdef['_values_dict'][value] + 1
            except KeyError:
                self.error('got an unexpected choice: %s', value)
                value = None

        return value

    async def attr_set_value(self, name: str, value: Attribute) -> None:
        attrdefs = await self.get_attrdefs()
        attrdef = attrdefs.get(name)
        if not attrdef:
            return

        if attrdef.get('type') == 'boolean':
            if value:
                value = attrdef.get('_value_on', True)
            else:
                value = attrdef.get('_value_off', False)
        elif attrdef.get('_values'):  # map choice to Z2M value
            try:
                value = attrdef.get('_values', [])[value - 1]
            except IndexError:
                raise ValueError(f'Invalid choice: {value}')

        await self.set_property_value(name, value, attrdef.get('property_group_name'))

    async def read_value(self) -> NullablePortValue:
        return self.get_peripheral().is_device_enabled(self.get_device_friendly_name())

    @core_ports.skip_write_unavailable
    async def write_value(self, value: bool) -> None:
        await self.get_peripheral().set_device_enabled(self.get_device_friendly_name(), value)

    async def attr_get_display_name(self) -> str:
        config = self.get_peripheral().get_device_config(self.get_device_friendly_name())
        info = self.get_peripheral().get_device_info(self.get_device_friendly_name()) or {}
        return config.get('description', info.get('definition', {}).get('description', ''))

    async def attr_set_display_name(self, value: str) -> None:
        await self.get_peripheral().set_device_config(self.get_device_friendly_name(), {'description': value})

    async def handle_enable(self) -> None:
        await super().handle_enable()
        self.get_peripheral().update_ports_from_device_info_asap(self)

    async def handle_disable(self) -> None:
        await super().handle_enable()
        self.get_peripheral().update_ports_from_device_info_asap(self)
