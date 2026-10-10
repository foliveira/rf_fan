"""The setup wizard and the Configure flow."""

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .common import FAN_CODES, FREQ_HZ, FakeRM4Pro, burst

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
    flow, _ = await _learn(
        hass,
        rm4,
        flow,
        [FAN_CODES["off"], FAN_CODES["speed_1"]],
        hass.config_entries.flow.async_configure,
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY, flow
    assert set(flow["data"]["commands"]) == {"off", "speed_1"}
    assert rm4.arms[0] == 433.92  # direct capture listens at the set frequency
    await hass.async_block_till_done()
    assert hass.states.get("fan.ceiling_fan") is not None
