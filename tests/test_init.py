"""Config entry migration."""

from broadlink.remote import TICK
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from .common import (
    FAN_CODES,
    FREQ_HZ,
    FakeRM4Pro,
    burst,
    fan_entry,
    legacy_stored,
    packet_ticks,
    setup_entry,
)


async def test_codes_learned_by_1x_replay_as_learned(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    """1.x stored timings at the old tick; once migrated, they are sent with
    the remote's own pulse lengths, not 7.8 % longer."""
    entry = fan_entry(broadlink_rf)
    await setup_entry(hass, entry)
    assert (entry.version, entry.minor_version) == (2, 2)
    await hass.services.async_call(
        "fan",
        "set_percentage",
        {"entity_id": "fan.ceiling_fan", "percentage": 100},
        blocking=True,
    )
    assert packet_ticks(rm4.sent[-1]) == packet_ticks(burst(FAN_CODES["speed_3"]))


async def test_v1_entry_migrates_through_to_2_2(
    hass: HomeAssistant, rm4: FakeRM4Pro, broadlink_rf: str
) -> None:
    timer = burst(0xA5A520)
    entry = MockConfigEntry(
        domain="rf_fan",
        version=1,
        title="Ceiling fan",
        data={
            "name": "Ceiling fan",
            "transmitter": broadlink_rf,
            "frequency": FREQ_HZ,
            "speed_count": 1,
            "commands": {
                "off": legacy_stored(burst(FAN_CODES["off"])),
                "speed_1": legacy_stored(burst(FAN_CODES["speed_1"])),
                "custom_timer": legacy_stored(timer),
            },
            "customs": [{"key": "custom_timer", "name": "Timer"}],
        },
    )
    await setup_entry(hass, entry)
    assert (entry.version, entry.minor_version) == (2, 2)
    assert entry.data["sub_entities"] == [
        {"entity_type": "button", "name": "Timer", "key_prefix": "custom_timer"}
    ]
    assert entry.data["commands"]["custom_timer"]["timings"][:3] == [
        round(t * TICK) * sign
        for t, sign in zip(packet_ticks(timer)[:3], (1, -1, 1), strict=True)
    ]
    assert hass.states.get("button.ceiling_fan_timer") is not None
