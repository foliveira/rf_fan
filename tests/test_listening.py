"""Following the physical remote through the Broadlink's receiver."""

import asyncio
from unittest.mock import patch

from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType

from .common import FAN_CODES, FakeRM4Pro, burst, fan_entry, hear, setup_entry


def _fan(hass: HomeAssistant) -> State:
    return hass.states.get("fan.ceiling_fan")


def _light(hass: HomeAssistant) -> str:
    return hass.states.get("light.ceiling_fan_light").state


async def test_remote_press_updates_the_fan(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    await setup_entry(hass, fan_entry(broadlink_rf))
    assert _fan(hass).state == "off"
    await hear(hass, rm4, burst(FAN_CODES["speed_3"], repeats=5, skip=7, jitter=40))
    assert _fan(hass).state == "on"
    assert _fan(hass).attributes["percentage"] == 100


async def test_off_and_unknown_codes(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    await setup_entry(hass, fan_entry(broadlink_rf))
    await hear(hass, rm4, burst(FAN_CODES["speed_1"], skip=11, jitter=50))
    assert _fan(hass).attributes["percentage"] == 33
    await hear(hass, rm4, burst(0x5A5A02, jitter=20))  # some other remote
    assert _fan(hass).attributes["percentage"] == 33
    await hear(hass, rm4, burst(FAN_CODES["off"], skip=4, jitter=50))
    assert _fan(hass).state == "off"


async def test_toggle_counts_once_per_press(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    await setup_entry(hass, fan_entry(broadlink_rf))
    await hear(hass, rm4, burst(FAN_CODES["light"], skip=3, jitter=30))
    assert _light(hass) == "on"
    # The same press captured again (button held down) is not another toggle.
    await hear(hass, rm4, burst(FAN_CODES["light"], skip=9, jitter=30, seed=2))
    assert _light(hass) == "on"
    # A separate, later press toggles it back.
    with patch("custom_components.rf_fan.listener.DEBOUNCE_SECONDS", 0):
        await hear(hass, rm4, burst(FAN_CODES["light"], jitter=30, seed=3))
    assert _light(hass) == "off"


async def test_own_send_is_not_heard_back(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    """Hearing the toggle Home Assistant just sent does not undo it."""
    await setup_entry(hass, fan_entry(broadlink_rf))
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.ceiling_fan_light"}, blocking=True
    )
    assert _light(hass) == "on"
    await hear(hass, rm4, burst(FAN_CODES["light"], skip=2))
    assert _light(hass) == "on"


async def test_learning_takes_the_receiver(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    """A learn flow gets the receiver; listening resumes once it is saved."""
    entry = fan_entry(broadlink_rf)
    await setup_entry(hass, entry)
    await asyncio.sleep(0.1)
    assert rm4.capture_active

    configure = hass.config_entries.options.async_configure
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await configure(flow["flow_id"], {"next_step_id": "relearn"})
    flow = await configure(flow["flow_id"], {"button": "speed_2"})
    assert flow["type"] is FlowResultType.SHOW_PROGRESS
    assert not rm4.capture_active
    await hass.async_block_till_done()
    flow = await configure(flow["flow_id"])
    assert flow["step_id"] == "learn_press"
    rm4.air.append(burst(FAN_CODES["speed_2"]))
    flow = await configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    flow = await configure(flow["flow_id"])
    assert flow["step_id"] == "learn_result"
    flow = await configure(flow["flow_id"], {"next_step_id": "confirm"})
    flow = await configure(flow["flow_id"], {"next_step_id": "finish"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)
    assert rm4.capture_active
    await hear(hass, rm4, burst(FAN_CODES["speed_2"], skip=5))
    assert _fan(hass).attributes["percentage"] == 66


async def test_abandoned_learn_flow_gives_the_receiver_back(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    entry = fan_entry(broadlink_rf)
    await setup_entry(hass, entry)
    configure = hass.config_entries.options.async_configure
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await configure(flow["flow_id"], {"next_step_id": "relearn"})
    flow = await configure(flow["flow_id"], {"button": "off"})
    assert not rm4.capture_active
    hass.config_entries.options.async_abort(flow["flow_id"])
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)
    assert rm4.capture_active
    await hear(hass, rm4, burst(FAN_CODES["speed_3"], skip=1))
    assert _fan(hass).attributes["percentage"] == 100


async def test_malformed_capture_does_not_stop_listening(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    await setup_entry(hass, fan_entry(broadlink_rf))
    # The declared length (2) ends inside an escaped long pulse (00 01 2c).
    await hear(hass, rm4, bytes([0xB1, 0x00, 0x02, 0x00, 0x00, 0x01, 0x2C, 0x0C]))
    await hear(hass, rm4, burst(FAN_CODES["speed_3"], skip=2))
    assert _fan(hass).attributes["percentage"] == 100
