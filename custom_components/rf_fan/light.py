"""Light platform for rf_fan - toggle or on/off RF lights with optimistic state."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .command import async_send_stored
from .const import (
    CLEAN_REPEAT,
    CMD_LIGHT,
    CMD_OFF,
    CMD_ON,
    CONF_COMMANDS,
    CONF_DIRECT,
    CONF_FREQUENCY,
    CONF_HAS_LIGHT,
    CONF_LIGHT_MODE,
    CONF_NAME,
    CONF_SUB_ENTITIES,
    CONF_TRANSMITTER,
    DOMAIN,
    ENTITY_TYPE_FAN,
    ENTITY_TYPE_LIGHT,
    LIGHT_MODE_ON_OFF,
    LIGHT_MODE_TOGGLE,
)
from .listener import PressTracker, signal_heard


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the light entity."""
    data = entry.data
    entities: list[LightEntity] = []
    commands = data[CONF_COMMANDS]

    primary_type = data.get("entity_type", ENTITY_TYPE_FAN)

    # Fan with an integrated light (one toggle button)
    if primary_type == ENTITY_TYPE_FAN and data.get(CONF_HAS_LIGHT) and CMD_LIGHT in commands:
        entities.append(RfLight(entry, name="Light", key_prefix="", light_mode=LIGHT_MODE_TOGGLE))

    # Primary entity is a light
    if primary_type == ENTITY_TYPE_LIGHT:
        light_mode = data.get(CONF_LIGHT_MODE, LIGHT_MODE_TOGGLE)
        entities.append(RfLight(entry, name=data[CONF_NAME], key_prefix="", light_mode=light_mode))

    # Sub-entities
    for sub in data.get(CONF_SUB_ENTITIES, []):
        if sub.get("entity_type") == ENTITY_TYPE_LIGHT:
            entities.append(RfLight(
                entry,
                name=sub["name"],
                key_prefix=sub["key_prefix"],
                light_mode=sub.get("light_mode", LIGHT_MODE_TOGGLE),
            ))
        elif (
            sub.get("entity_type") == ENTITY_TYPE_FAN
            and sub.get("has_light")
            and f"{sub['key_prefix']}_{CMD_LIGHT}" in commands
        ):
            entities.append(RfLight(
                entry,
                name=f"{sub['name']} light",
                key_prefix=sub["key_prefix"],
                light_mode=LIGHT_MODE_TOGGLE,
            ))

    if entities:
        async_add_entities(entities)


class RfLight(LightEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_assumed_state = True
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, entry: ConfigEntry, name: str, key_prefix: str, light_mode: str) -> None:
        """Initialise from the learned light command."""
        self._commands = entry.data[CONF_COMMANDS]
        self._transmitter = entry.data[CONF_TRANSMITTER]
        self._frequency = entry.data[CONF_FREQUENCY]
        self._direct = entry.data.get(CONF_DIRECT, False)
        self._light_mode = light_mode
        self._key_prefix = key_prefix
        self._entry_id = entry.entry_id
        self._presses = PressTracker()
        self._attr_name = name
        uid_suffix = f"light_{key_prefix}" if key_prefix else "light"
        self._attr_unique_id = f"{entry.entry_id}_{uid_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data[CONF_NAME],
            manufacturer="RF Device (Broadlink Learning)",
        )
        self._attr_is_on = False

    def _pk(self, cmd: str) -> str:
        return f"{self._key_prefix}_{cmd}" if self._key_prefix else cmd

    async def async_added_to_hass(self) -> None:
        """Restore the last assumed state, then follow the physical remote."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_heard(self._entry_id), self._async_heard
            )
        )

    @callback
    def _async_heard(self, key: str) -> None:
        """Take on the state a press on the physical remote leaves the light in."""
        if self._light_mode == LIGHT_MODE_TOGGLE:
            keys = {self._pk(CMD_LIGHT)}
        else:
            keys = {self._pk(CMD_ON), self._pk(CMD_OFF)}
        if key not in keys or not self._presses.heard(key):
            return
        if self._light_mode == LIGHT_MODE_TOGGLE:
            self._attr_is_on = not self._attr_is_on
        else:
            self._attr_is_on = key == self._pk(CMD_ON)
        self.async_write_ha_state()

    async def _send(self, cmd: str) -> None:
        key = self._pk(cmd)
        command = self._commands.get(key)
        if not command:
            return
        self._presses.sent(key)
        await async_send_stored(
            self.hass,
            self._transmitter,
            command,
            self._frequency,
            clean=self._direct,
            repeat=CLEAN_REPEAT if self._direct else 0,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on (only sends the toggle if currently off)."""
        if self._light_mode == LIGHT_MODE_TOGGLE:
            if not self._attr_is_on:
                await self._send(CMD_LIGHT)
        else:
            await self._send(CMD_ON)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off (only sends the toggle if currently on)."""
        if self._light_mode == LIGHT_MODE_TOGGLE:
            if self._attr_is_on:
                await self._send(CMD_LIGHT)
        else:
            await self._send(CMD_OFF)
        self._attr_is_on = False
        self.async_write_ha_state()
