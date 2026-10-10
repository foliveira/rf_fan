"""Fixtures: Home Assistant's own Broadlink integration on a fake RM4 Pro."""

from unittest.mock import patch

# Bind the top-level custom_components package to this repository before the
# test plugin's config directory, which ships its own, can take the name.
import custom_components  # noqa: F401
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .common import RM4PRO_DEVTYPE, RM4PRO_MAC, FakeRM4Pro


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/rf_fan."""


@pytest.fixture
def rm4() -> FakeRM4Pro:
    """The RM4 Pro behind the Broadlink integration."""
    return FakeRM4Pro()


@pytest.fixture
async def broadlink_rf(hass: HomeAssistant, rm4: FakeRM4Pro) -> str:
    """Set up the Broadlink integration for the RM4 Pro; return the entity_id
    of its radio_frequency transmitter."""
    entry = MockConfigEntry(
        domain="broadlink",
        unique_id=RM4PRO_MAC,
        title="RM4 Pro",
        data={
            "host": "192.168.0.14",
            "mac": RM4PRO_MAC,
            "type": RM4PRO_DEVTYPE,
            "timeout": 10,
        },
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "homeassistant.components.broadlink.device.blk.gendevice",
            return_value=rm4,
        ),
        patch("homeassistant.components.broadlink.heartbeat.blk.ping"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "radio_frequency"
    )
