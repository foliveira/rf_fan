"""The setup wizard and the Configure flow."""

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .common import FAN_CODES, FREQ_HZ, FakeRM4Pro, burst, fan_entry, hear, setup_entry

Configure = Callable[..., Awaitable[dict[str, Any]]]


async def _learn(
    hass: HomeAssistant,
    rm4: FakeRM4Pro,
    flow: dict[str, Any],
    codes: list[int],
    configure: Configure,
) -> tuple[dict[str, Any], list[str]]:
    """Press each button when the wizard asks for it and keep the code.

    Returns the flow after the last button and the button labels shown.
    """
    labels = []
    for code in codes:
        assert flow["type"] is FlowResultType.SHOW_PROGRESS, flow
        labels.append(flow["description_placeholders"]["button"])
        rm4.air.append(burst(code))
        await hass.async_block_till_done()
        flow = await configure(flow["flow_id"])
        if flow["step_id"] == "learn_press":  # swept: release, then press again
            flow = await configure(flow["flow_id"], {})
            await hass.async_block_till_done()
            flow = await configure(flow["flow_id"])
        assert flow["step_id"] == "learn_result", flow
        flow = await configure(flow["flow_id"], {"next_step_id": "confirm"})
    return flow, labels


async def _start_fan_setup(
    hass: HomeAssistant, transmitter: str, **options: Any
) -> dict[str, Any]:
    """Fill in the wizard's two screens for a one-speed fan."""
    flow = await hass.config_entries.flow.async_init(
        "rf_fan", context={"source": "user"}
    )
    assert flow["type"] is FlowResultType.FORM
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {
            "name": "Ceiling fan",
            "entity_type": "fan",
            "transmitter": transmitter,
            "frequency": str(FREQ_HZ),
        },
    )
    assert flow["step_id"] == "entity_options"
    return await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"speed_count": 1, "has_on_button": False, "direct_capture": True, **options},
    )


async def test_wizard_learns_the_fan(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    flow = await _start_fan_setup(hass, broadlink_rf)
    flow, labels = await _learn(
        hass,
        rm4,
        flow,
        [FAN_CODES["off"], FAN_CODES["speed_1"]],
        hass.config_entries.flow.async_configure,
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY, flow
    assert labels == ["Off", "Speed 1 of 1 (lowest)"]
    assert set(flow["data"]["commands"]) == {"off", "speed_1"}
    assert rm4.arms[0] == 433.92  # direct capture listens at the set frequency
    await hass.async_block_till_done()
    assert hass.states.get("fan.ceiling_fan") is not None


async def test_wizard_learns_the_fan_light_when_ticked(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    flow = await _start_fan_setup(hass, broadlink_rf, has_light=True)
    flow, labels = await _learn(
        hass,
        rm4,
        flow,
        [FAN_CODES["off"], FAN_CODES["speed_1"], FAN_CODES["light"]],
        hass.config_entries.flow.async_configure,
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY, flow
    assert labels[-1] == "Light"
    assert flow["data"]["has_light"] is True
    assert set(flow["data"]["commands"]) == {"off", "speed_1", "light"}
    await hass.async_block_till_done()

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.ceiling_fan_light"}, blocking=True
    )
    assert hass.states.get("light.ceiling_fan_light").state == "on"
    assert len(rm4.sent) == 1


async def test_wizard_skips_the_fan_light_when_unticked(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    flow = await _start_fan_setup(hass, broadlink_rf, has_light=False)
    flow, _ = await _learn(
        hass,
        rm4,
        flow,
        [FAN_CODES["off"], FAN_CODES["speed_1"]],
        hass.config_entries.flow.async_configure,
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY, flow
    assert flow["data"]["has_light"] is False
    assert "light" not in flow["data"]["commands"]
    await hass.async_block_till_done()
    assert hass.states.async_entity_ids("light") == []


async def test_added_fan_can_learn_its_light(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    """A fan added under Configure gets the same light option."""
    entry = fan_entry(broadlink_rf)
    await setup_entry(hass, entry)
    configure = hass.config_entries.options.async_configure
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await configure(flow["flow_id"], {"next_step_id": "add_entity"})
    flow = await configure(flow["flow_id"], {"entity_type": "fan", "name": "Patio"})
    assert flow["step_id"] == "add_entity_options"
    flow = await configure(
        flow["flow_id"], {"speed_count": 1, "has_on_button": False, "has_light": True}
    )
    patio_off, patio_speed, patio_light = 0x3C3C01, 0x3C3C02, 0x3C3C10
    flow, labels = await _learn(
        hass, rm4, flow, [patio_off, patio_speed, patio_light], configure
    )
    assert labels[-1] == "Light"
    assert flow["step_id"] == "init"
    flow = await configure(flow["flow_id"], {"next_step_id": "finish"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert "patio_light" in entry.data["commands"]
    assert hass.states.get("fan.ceiling_fan_patio") is not None
    assert hass.states.get("light.ceiling_fan_patio_light").state == "off"
    await hear(hass, rm4, burst(patio_light, skip=3))
    assert hass.states.get("light.ceiling_fan_patio_light").state == "on"
