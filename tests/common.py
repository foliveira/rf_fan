"""Test helpers: an in-memory RM4 Pro and builders for remote presses."""

import asyncio
from base64 import b64encode
import random
import struct

import broadlink as blk
from broadlink import exceptions as blk_exc
from broadlink.remote import SignalKind, pulses_to_data
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

RM4PRO_DEVTYPE = 0x6026
RM4PRO_MAC = "34ea34c43f31"
FREQ_HZ = 433_920_000

# The timing unit rf_fan 1.x decoded captures with (python-broadlink < 1.0).
LEGACY_TICK = 32.84

# 24-bit codes of a fan remote's buttons.
FAN_CODES = {
    "off": 0xA5A501,
    "speed_1": 0xA5A502,
    "speed_2": 0xA5A504,
    "speed_3": 0xA5A508,
    "light": 0xA5A510,
}


class FakeRM4Pro(blk.rm4pro):
    """python-broadlink's RM4 Pro with an in-memory radio instead of UDP.

    Only the transport (``_send``) and the network handshakes are replaced, so
    the library's own learning, sending and ``capture_rf`` code runs.
    """

    def __init__(self) -> None:
        """Start idle, with nothing on the air."""
        super().__init__(
            ("192.168.0.14", 80),
            RM4PRO_MAC,
            RM4PRO_DEVTYPE,
            name="RM4 Pro",
            model="RM4 pro",
            manufacturer="Broadlink",
        )
        self.sent: list[bytes] = []
        self.arms: list[float | None] = []
        self.armed = False
        self.air: list[bytes] = []  # packets the receiver hears once armed

    async def auth(self) -> bool:
        return True

    async def get_fwversion(self) -> int:
        return 52

    async def aclose(self) -> None:
        return None

    async def check_sensors(self) -> dict:
        return {"temperature": 21.0, "humidity": 40.0}

    async def update(self) -> None:
        return None

    async def _send(self, command: int, data: bytes = b"") -> bytes:
        if command == 0x02:  # send_data; transmitting ends a learn session
            self.sent.append(bytes(data))
            self.armed = False
            return b""
        if command == 0x1B:  # find_rf_packet
            self.arms.append(struct.unpack("<I", data)[0] / 1000 if data else None)
            self.armed = True
            return b""
        if command in (0x19, 0x1E):  # sweep_frequency, cancel_sweep_frequency
            return b""
        if command == 0x1A:  # check_frequency: found 433.92 MHz
            self.armed = False
            return bytes([1]) + struct.pack("<I", 433920)
        if command == 0x04:  # check_data
            if self.armed and self.air:
                self.armed = False
                return self.air.pop(0)
            raise blk_exc.exception(-5)  # StorageError: nothing captured yet
        raise blk_exc.exception(-4)  # CommandNotSupportedError


def ev1527_frame(code: int, base: int = 350) -> list[int]:
    """One 24-bit EV1527-style frame and its sync gap, signed microseconds."""
    frame: list[int] = []
    for bit in range(23, -1, -1):
        if code >> bit & 1:
            frame += [3 * base, -base]
        else:
            frame += [base, -3 * base]
    return [*frame, base, -31 * base]


def device_packet(timings: list[int]) -> bytes:
    """The bytes an RM4 Pro returns from check_data for these pulses."""
    packet = bytearray(
        pulses_to_data([abs(t) for t in timings], kind=SignalKind.RF_433)
    )
    packet[0] = 0xB1  # what an RM4 Pro reports for a 433 MHz capture
    return bytes(packet) + bytes(16)  # zero padding past the declared length


def capture(
    timings: list[int], skip: int = 0, jitter: int = 0, seed: int = 1
) -> bytes:
    """A capture of a pulse train that lost its first ``skip`` pulses and has
    up to ``jitter`` us of timing noise."""
    rng = random.Random(seed)
    timings = timings[skip + skip % 2 :]  # a capture starts on a mark
    if jitter:
        timings = [
            (abs(t) + rng.randint(-jitter, jitter)) * (1 if t > 0 else -1)
            for t in timings
        ]
    return device_packet(timings)


def burst(
    code: int,
    repeats: int = 8,
    skip: int = 0,
    jitter: int = 0,
    seed: int = 1,
    base: int = 350,
) -> bytes:
    """A button press as the RM4 Pro captures it: the frame repeated."""
    return capture(ev1527_frame(code, base) * repeats, skip, jitter, seed)


def packet_ticks(packet: bytes) -> list[int]:
    """Pulse lengths of a Broadlink packet, in device ticks."""
    length = packet[2] | packet[3] << 8
    body = packet[4 : 4 + length]
    ticks: list[int] = []
    i = 0
    while i < len(body):
        if body[i] == 0:
            ticks.append(body[i + 1] << 8 | body[i + 2])
            i += 3
        else:
            ticks.append(body[i])
            i += 1
    return ticks


def legacy_stored(packet: bytes) -> dict:
    """A learned command as rf_fan 1.x stored it."""
    timings = [
        round(t * LEGACY_TICK) * (1 if i % 2 == 0 else -1)
        for i, t in enumerate(packet_ticks(packet))
    ]
    return {"timings": timings, "repeat": packet[1], "b64": b64encode(packet).decode()}


def fan_entry(transmitter: str) -> MockConfigEntry:
    """A three-speed fan with a light, as rf_fan 1.1.0 stored it."""
    return MockConfigEntry(
        domain="rf_fan",
        version=2,
        title="Ceiling fan",
        data={
            "name": "Ceiling fan",
            "entity_type": "fan",
            "transmitter": transmitter,
            "frequency": FREQ_HZ,
            "speed_count": 3,
            "has_on_button": False,
            "has_light": True,
            "direct_capture": False,
            "commands": {k: legacy_stored(burst(c)) for k, c in FAN_CODES.items()},
            "customs": [],
            "sub_entities": [],
        },
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up a config entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def hear(hass: HomeAssistant, rm4: FakeRM4Pro, packet: bytes) -> None:
    """The RM4 Pro hears a press; wait (up to 4 s) until it is taken."""
    rm4.air.append(packet)
    for _ in range(40):
        await asyncio.sleep(0.1)
        if packet not in rm4.air:
            break
    await hass.async_block_till_done()
