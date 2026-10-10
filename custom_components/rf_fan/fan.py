"""Fan platform for rf_fan - an optimistic, discrete-speed RF fan."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .command import async_send_stored
from .const import (
    CLEAN_REPEAT,
    CMD_OFF,
    CMD_ON,
    CONF_COMMANDS,
    CONF_DIRECT,
    CONF_FREQUENCY,
    CONF_HAS_ON,
    CONF_NAME,
    CONF_SPEED_COUNT,
    CONF_SUB_ENTITIES,
    CONF_TRANSMITTER,
    DOMAIN,
    ENTITY_TYPE_FAN,
)
from .listener import PressTracker, signal_heard


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the fan entity from a config entry."""
    data = entry.data
    entities: list[FanEntity] = []

    primary_type = data.get("entity_type", ENTITY_TYPE_FAN)
    if primary_type == ENTITY_TYPE_FAN:
        entities.append(RfFan(
            entry,
            name=data[CONF_NAME],
            key_prefix="",
            speed_count=data.get(CONF_SPEED_COUNT, 3),
            has_on=data.get(CONF_HAS_ON, False),
        ))

    for sub in data.get(CONF_SUB_ENTITIES, []):
        if sub.get("entity_type") == ENTITY_TYPE_FAN:
            entities.append(RfFan(
                entry,
                name=sub["name"],
                key_prefix=sub["key_prefix"],
                speed_count=sub.get("speed_count", 3),
                has_on=sub.get("has_on", False),
            ))

    if entities:
        async_add_entities(entities)


class RfFan(FanEntity, RestoreEntity):
    """A learned RF fan with discrete speeds and optimistic state.

    RF is one-way, so we cannot read the fan's real state - we assume it matches
    the last command we sent. Because every speed maps to its own discrete code
    (and there's a dedicated Off), that assumption stays accurate without drift.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_assumed_state = True

    def __init__(
        self,
        entry: ConfigEntry,
        name: str,
        key_prefix: str,
        speed_count: int,
        has_on: bool,
    ) -> None:
        """Initialise from the config entry's learned commands."""
        data = entry.data
        self._transmitter: str = data[CONF_TRANSMITTER]
        self._frequency: int = data[CONF_FREQUENCY]
        self._commands: dict[str, Any] = data[CONF_COMMANDS]
        self._speed_count = speed_count
        self._has_on = has_on
        self._direct: bool = data.get(CONF_DIRECT, False)
        self._key_prefix = key_prefix
        self._entry_id = entry.entry_id
        self._presses = PressTracker()
        self._attr_name = name if key_prefix else None
        uid_suffix = f"fan_{key_prefix}" if key_prefix else "fan"

        self._attr_unique_id = f"{entry.entry_id}_{uid_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=data[CONF_NAME],
            manufacturer="RF Device (Broadlink Learning)",
        )
        self._attr_supported_features = (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
        )
        self._attr_speed_count = speed_count
        self._attr_is_on = False
        self._attr_percentage = 0

    def _pk(self, cmd: str) -> str:
        return f"{self._key_prefix}_{cmd}" if self._key_prefix else cmd

    async def async_added_to_hass(self) -> None:
        """Restore the last assumed state, then follow the physical remote."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
            if (pct := last.attributes.get("percentage")) is not None:
                self._attr_percentage = pct
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_heard(self._entry_id), self._async_heard
            )
        )

    @callback
    def _async_heard(self, key: str) -> None:
        """Take on the state a press on the physical remote leaves the fan in."""
        speeds = {
            self._pk(f"speed_{i}"): i for i in range(1, self._speed_count + 1)
        }
        if key not in (self._pk(CMD_OFF), self._pk(CMD_ON), *speeds):
            return
        if not self._presses.heard(key):
            return
        if key == self._pk(CMD_OFF):
            self._attr_is_on = False
            self._attr_percentage = 0
        elif key in speeds:
            self._attr_is_on = True
            self._attr_percentage = ranged_value_to_percentage(
                (1, self._speed_count), speeds[key]
            )
        else:  # On resumes the last speed, or speed 1, as async_turn_on assumes
            self._attr_is_on = True
            if not self._attr_percentage:
                self._attr_percentage = ranged_value_to_percentage(
                    (1, self._speed_count), 1
                )
        self.async_write_ha_state()

    async def _send(self, key: str) -> None:
        """Transmit a learned command by key."""
        command = self._commands.get(key)
        if not command:
            raise HomeAssistantError(
                f"Command '{key}' not learned."
            )
        self._presses.sent(key)
        await async_send_stored(
            self.hass,
            self._transmitter,
            command,
            self._frequency,
            clean=self._direct,
            repeat=CLEAN_REPEAT if self._direct else 0,
        )

    def _speed_from_percentage(self, percentage: int) -> int:
        """Map a percentage to a discrete speed 1..speed_count."""
        speed = math.ceil(percentage_to_ranged_value((1, self._speed_count), percentage))
        return max(1, min(self._speed_count, speed))

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the fan speed by percentage."""
        if percentage == 0:
            await self.async_turn_off()
            return
        speed = self._speed_from_percentage(percentage)
        await self._send(self._pk(f"speed_{speed}"))
        self._attr_is_on = True
        self._attr_percentage = ranged_value_to_percentage((1, self._speed_count), speed)
        self.async_write_ha_state()

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn the fan on, optionally at a given speed."""
        if percentage is not None:
            await self.async_set_percentage(percentage)
            return
        on_key = self._pk(CMD_ON)
        if self._has_on and on_key in self._commands:
            await self._send(on_key)
            self._attr_is_on = True
            if not self._attr_percentage:
                self._attr_percentage = ranged_value_to_percentage(
                    (1, self._speed_count), 1
                )
        else:
            # No dedicated On button: resume the last speed, or speed 1.
            speed = self._speed_from_percentage(self._attr_percentage or 1)
            await self._send(self._pk(f"speed_{speed}"))
            self._attr_is_on = True
            self._attr_percentage = ranged_value_to_percentage(
                (1, self._speed_count), speed
            )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the fan off."""
        await self._send(self._pk(CMD_OFF))
        self._attr_is_on = False
        self._attr_percentage = 0
        self.async_write_ha_state()
