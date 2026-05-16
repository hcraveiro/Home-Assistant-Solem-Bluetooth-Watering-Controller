"""Number setup for our Integration.

Here we use a different method to define some of our entity classes.
As, in our example, so much is common, we use our base entity class to define
many properties, then our base sensor class to define the property to get the
value of the sensor.

As such, for all our other sensor types, we can just set the _attr_ value to
keep our code small and easily readable.  You can do this for all entity properties(attributes)
if you so wish, or mix and match to suit.
"""

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from . import MyConfigEntry
from .base import SolemBaseEntity
from .coordinator import SolemCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class NumberTypeClass:
    """Class for holding sensor type to sensor class."""

    device_type: str
    state_field: str
    number_class: object


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MyConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up the Sensors."""
    coordinator: SolemCoordinator = config_entry.runtime_data.coordinator

    number_types = [
        NumberTypeClass("IRRIGATION_DURATION_NUMBER", "value", IrrigationManualDuration),
        NumberTypeClass("IRRIGATION_DURATION_STATION_NUMBER", "value", IrrigationManualDurationPerStation),  # MODIFICA
        NumberTypeClass("WATER_FLOW_NUMBER", "value", IrrigationFlowRate),
    ]

    numbers = []

    for number_type in number_types:
        _LOGGER.debug(number_type)
        numbers.extend(
            [
                number_type.number_class(coordinator, device, number_type.state_field)
                for device in coordinator.data
                if device.get("device_type") == number_type.device_type
            ]
        )

    async_add_entities(numbers)


class SolemNumberEntity(SolemBaseEntity, NumberEntity):
    def __init__(
        self, coordinator: SolemCoordinator, device: dict[str, Any], parameter: str
    ) -> None:
        """Initialise entity."""
        super().__init__(coordinator, device, parameter)

    @property
    def entity_category(self):
        return EntityCategory.CONFIG

class IrrigationManualDuration(SolemNumberEntity):
    def __init__(self, coordinator: SolemCoordinator, device: dict[str, Any], parameter: str):
        super().__init__(coordinator, device, parameter)
        self._attr_min_value = 1
        self._attr_max_value = 60
        self._attr_native_min_value = 1
        self._attr_native_max_value = 60
        self._attr_step = 1
        self._attr_native_value = self.coordinator.irrigation_manual_duration

    @property
    def native_value(self) -> float | None:
        return self.coordinator.irrigation_manual_duration
        
    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.coordinator.irrigation_manual_duration = value
        self.async_write_ha_state()


# MODIFICA: nuova classe per la durata per singola stazione
class IrrigationManualDurationPerStation(SolemNumberEntity):
    def __init__(self, coordinator: SolemCoordinator, device: dict[str, Any], parameter: str):
        super().__init__(coordinator, device, parameter)
        self._attr_min_value = 1
        self._attr_max_value = 60
        self._attr_native_min_value = 1
        self._attr_native_max_value = 60
        self._attr_step = 1
        station_id = int(self.device_id.rsplit('_', 1)[-1])
        self._attr_native_value = self.coordinator.irrigation_manual_duration_per_station[station_id - 1]

    @property
    def native_value(self) -> float | None:
        station_id = int(self.device_id.rsplit('_', 1)[-1])
        return self.coordinator.irrigation_manual_duration_per_station[station_id - 1]

    async def async_set_native_value(self, value: float) -> None:
        station_id = int(self.device_id.rsplit('_', 1)[-1])
        self._attr_native_value = value
        self.coordinator.irrigation_manual_duration_per_station[station_id - 1] = value
        await self.coordinator.save_persistent_data()
        self.async_write_ha_state()


class IrrigationFlowRate(SolemNumberEntity):
    def __init__(self, coordinator: SolemCoordinator, device: dict[str, Any], parameter: str):
        super().__init__(coordinator, device, parameter)
        self._attr_min_value = 1
        self._attr_max_value = 30
        self._attr_native_min_value = 1
        self._attr_native_max_value = 30
        self._attr_step = 1

    @property
    def native_value(self) -> float | None:
        station_id = int(self.device_id.rsplit('_', 1)[-1])
        return self.coordinator.water_flow_rate[station_id - 1]
        
    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        station_id = int(self.device_id.rsplit('_', 1)[-1])
        self.coordinator.water_flow_rate[station_id - 1] = value
        self.async_write_ha_state()
