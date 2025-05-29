import abc
import asyncio
import copy

from typing import Any, cast

from qtoggleserver.core import ports as core_ports
from qtoggleserver.core.typing import Attribute, AttributeDefinitions, NullablePortValue, PortValue
from qtoggleserver.peripherals import PeripheralPort
from qtoggleserver.utils import asyncio as asyncio_utils

from .client import Zigbee2MQTTClient


class Zigbee2MQTTPort(PeripheralPort, metaclass=abc.ABCMeta):
    def get_peripheral(self) -> Zigbee2MQTTClient:
        return cast(Zigbee2MQTTClient, super().get_peripheral())


class BaseDevicePort(Zigbee2MQTTPort, metaclass=abc.ABCMeta):
    def __init__(self, *, device_friendly_name: str, **kwargs) -> None:
        self._device_friendly_name = device_friendly_name

        super().__init__(**kwargs)

    def get_peripheral(self) -> Zigbee2MQTTClient:
        return cast(Zigbee2MQTTClient, super().get_peripheral())

    async def attr_is_online(self) -> bool:
        return await super().attr_is_online() and self.get_peripheral().is_device_online(
            self.get_device_friendly_name()
        )

    def get_device_friendly_name(self) -> str:
        return self._device_friendly_name

    def get_device_safe_friendly_name(self) -> str:
        return self.get_peripheral().get_device_safe_friendly_name(self._device_friendly_name)

    def get_state_property(self, property_path: list[str]) -> Any:
        assert len(property_path) > 0

        value = self.get_peripheral().get_device_state(self.get_device_friendly_name()) or {}
        for property_name in property_path:
            value = value.get(property_name)
            if value is None:
                return None

        return value

    async def set_state_property(self, property_path: list[str], value: Any) -> None:
        assert len(property_path) > 0

        for property_name in reversed(property_path):
            value = {property_name: value}

        await self.get_peripheral().set_device_state(self.get_device_friendly_name(), value)

    def get_config_property(self, property_path: list[str]) -> Any:
        assert len(property_path) > 0

        value = self.get_peripheral().get_device_config(self.get_device_friendly_name()) or {}
        for property_name in property_path:
            value = value.get(property_name)
            if value is None:
                return None

        return value

    async def set_config_property(self, property_path: list[str], value: Any) -> None:
        assert len(property_path) > 0

        for property_name in reversed(property_path):
            value = {property_name: value}

        await self.get_peripheral().set_device_config(self.get_device_friendly_name(), value)


class PermitJoinPort(Zigbee2MQTTPort):
    ID = "permit_join"
    TYPE = core_ports.TYPE_BOOLEAN
    WRITABLE = True
    DISPLAY_NAME = "Permit Join"
    PERSISTED = None

    async def read_value(self) -> NullablePortValue:
        return self.get_peripheral().is_permit_join()

    @core_ports.skip_write_unavailable
    async def write_value(self, value: bool) -> None:
        await self.get_peripheral().set_permit_join(value)


class DevicePort(BaseDevicePort):
    def __init__(
        self,
        *,
        id: str,
        display_name: str = "",
        type: str,
        writable: bool,
        unit: str | None = None,
        min: float | None = None,
        max: float | None = None,
        property_path: list[str],
        storage: str,
        device_friendly_name: str,
        value_on: Any = True,
        value_off: Any = False,
        values: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(id=id, device_friendly_name=device_friendly_name, **kwargs)

        # Following properties will dictate the initial value of their corresponding port attributes
        self._display_name: str = display_name
        self._type: str = type
        self._writable: bool = writable
        self._unit: str | None = unit
        self._min: float | None = min
        self._max: float | None = max

        self._property_path: list[str] = property_path
        self._storage: str = storage
        self._value_on: Any = value_on
        self._value_off: Any = value_off
        self._values: list[str] | None = values

        if values:
            # This will determine the actual `choices` attribute value
            self._choices = [
                {"value": i + 1, "display_name": " ".join(x.capitalize() for x in v.split("_"))}
                for i, v in enumerate(values)
            ]
            self._values_dict: dict[str, int] = {v: i for i, v in enumerate(values)}

    async def read_value(self) -> NullablePortValue:
        if self._storage == "state":
            value = self.get_state_property(self.get_property_path())
        else:
            value = self.get_config_property(self.get_property_path())

        if await self.get_type() == core_ports.TYPE_BOOLEAN:
            value = value == self._value_on
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
                raise ValueError(f"Invalid choice: {value}")

        if self._storage == "state":
            await self.set_state_property(self.get_property_path(), value)
        else:
            await self.set_config_property(self.get_property_path(), value)

    def get_property_path(self) -> list[str]:
        return self._property_path


class DeviceControlPort(BaseDevicePort):
    ADDITIONAL_ATTRDEFS = {
        "friendly_name": {
            "display_name": "Friendly Name",
            "description": "Use this attribute to rename your Zigbee device. Set to empty string to remove the device.",
            "type": "string",
            "modifiable": True,
            "persisted": False,
        },
        "ieee_address": {
            "display_name": "Address",
            "description": "Zigbee IEEE Address",
            "type": "string",
            "modifiable": False,
            "property_path": ["ieee_address"],
            "storage": "config",
        },
    }

    # Force `display_name` not persisted (by qToggleServer) as it's actually persisted by Z2M
    STANDARD_ATTRDEFS = copy.deepcopy(DevicePort.STANDARD_ATTRDEFS)
    STANDARD_ATTRDEFS["display_name"]["persisted"] = False

    TYPE = core_ports.TYPE_BOOLEAN
    WRITABLE = True
    PERSISTED = None

    _MAX_RENAME_ATTEMPTS = 10

    def __init__(
        self,
        *,
        id: str,
        additional_attrdefs: AttributeDefinitions | None,
        device_friendly_name: str,
        **kwargs,
    ) -> None:
        super().__init__(id=id, device_friendly_name=device_friendly_name, **kwargs)

        self._additional_attrdefs: AttributeDefinitions = additional_attrdefs or {}

    async def get_additional_attrdefs(self) -> AttributeDefinitions:
        return self.ADDITIONAL_ATTRDEFS | self._additional_attrdefs

    async def enable_renamed_ports(self, enabled_port_ids: set[str], new_friendly_name: str, attempt: int = 1) -> None:
        self.debug("enabling renamed ports: %s (attempt %d)", ", ".join(enabled_port_ids), attempt)

        ports: list[BaseDevicePort] = self.get_peripheral().get_device_ports(new_friendly_name)
        control_port = self.get_peripheral().get_control_port(new_friendly_name)
        if control_port:
            ports.append(control_port)

        if not ports:
            if attempt < self._MAX_RENAME_ATTEMPTS:
                self.debug("renamed ports not added yet, retrying later")
                await asyncio.sleep(1)
                await self.enable_renamed_ports(enabled_port_ids, new_friendly_name, attempt + 1)
            else:
                self.error("timeout waiting for renamed ports to be added")
            return

        for port in ports:
            if port.get_initial_id() in enabled_port_ids:
                await port.enable()
                port.invalidate_attrs()
                await port.trigger_update()
                await port.save()

    async def attr_get_friendly_name(self) -> str:
        return self._device_friendly_name

    async def attr_set_friendly_name(self, value: str) -> None:
        current_friendly_name = self._device_friendly_name
        current_safe_friendly_name = self.get_peripheral().get_device_safe_friendly_name(self._device_friendly_name)

        if value:
            # Remember enabled ports before they are renamed (practically removed and re-added)
            all_enabled_port_ids = [
                p.get_initial_id()
                for p in self.get_peripheral().get_device_ports() + self.get_peripheral().get_control_ports()
                if p.is_enabled()
            ]
            device_enabled_port_ids = []
            for port_id in all_enabled_port_ids:
                if port_id == current_safe_friendly_name:
                    port_id = value
                elif port_id.startswith(f"{current_safe_friendly_name}."):
                    port_id = f"{value}.{port_id[len(current_safe_friendly_name) + 1 :]}"
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

    async def attr_get_value(self, name: str) -> Attribute | None:
        attrdefs = await self.get_additional_attrdefs()
        attrdef = attrdefs.get(name)
        if not attrdef:
            return None

        property_path = attrdef.get("property_path")
        if not property_path:
            return None

        # At this point we know this attribute is a Zigbee device property
        if attrdef["storage"] == "state":
            value = self.get_state_property(property_path)
        else:
            value = self.get_config_property(property_path)

        if value is None:
            # Supply a default value if not found in state
            if attrdef.get("type") == "boolean":
                value = attrdef.get("_value_off", False)
            elif attrdef.get("_values"):  # map Z2M value to choice
                value = attrdef["_values"][0]  # first value in enum
            elif attrdef.get("min") is not None:
                value = attrdef.get("min")
            elif attrdef.get("max") is not None:
                value = attrdef.get("max")
            else:
                value = 0

        if attrdef.get("type") == "boolean":
            value = value == attrdef.get("_value_on", True)
        elif attrdef.get("_values"):  # map Z2M value to choice
            try:
                value = attrdef["_values_dict"][value] + 1
            except KeyError:
                self.error("got an unexpected choice: %s", value)
                value = None

        return value

    async def attr_set_value(self, name: str, value: Attribute) -> None:
        attrdefs = await self.get_additional_attrdefs()
        attrdef = attrdefs.get(name)
        if not attrdef:
            return

        # Ensure this is a Zigbee property
        if not attrdef.get("property_path"):
            return

        if attrdef.get("type") == "boolean":
            if value:
                value = attrdef.get("_value_on", True)
            else:
                value = attrdef.get("_value_off", False)
        elif attrdef.get("_values"):  # map choice to Z2M value
            try:
                value = attrdef.get("_values", [])[value - 1]
            except IndexError:
                raise ValueError(f"Invalid choice: {value}")

        if attrdef["storage"] == "state":
            await self.set_state_property(attrdef["property_path"], value)
        else:
            await self.set_config_property(attrdef["property_path"], value)

    async def read_value(self) -> NullablePortValue:
        return self.get_peripheral().is_device_enabled(self.get_device_friendly_name())

    @core_ports.skip_write_unavailable
    async def write_value(self, value: bool) -> None:
        await self.get_peripheral().set_device_enabled(self.get_device_friendly_name(), value)

    async def attr_get_display_name(self) -> str:
        config_description = self.get_config_property(["description"])
        if config_description is not None:
            return config_description

        info = self.get_peripheral().get_device_info(self.get_device_friendly_name()) or {}
        info_description = info.get("definition", {}).get("description")
        if info_description is not None:
            return info_description

        return ""

    async def attr_set_display_name(self, value: str) -> None:
        await self.set_config_property(["description"], value)

    async def handle_enable(self) -> None:
        await super().handle_enable()
        self.get_peripheral().update_ports_from_device_info_asap(self.get_device_friendly_name())

    async def handle_disable(self) -> None:
        await super().handle_enable()
        self.get_peripheral().update_ports_from_device_info_asap(self.get_device_friendly_name())
