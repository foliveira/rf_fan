"""Background listening: keep state in step with the physical remote.

The Broadlink that transmits for an RF device also listens for it. Its receiver
is held in RF capture at the device's frequency through python-broadlink's
open-ended ``capture_rf`` window, which re-arms the RM4 Pro whenever it drops
out of learning mode and after every send. Each burst the Broadlink hears is
matched against the learned codes of the entries that use it, and a match is
handed to that entry's entities, which update their state as if Home Assistant
had sent the code.
"""

from __future__ import annotations

import asyncio
from base64 import b64encode
from collections import Counter
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from functools import lru_cache
import logging
from statistics import median
import time
from typing import Any

from broadlink.exceptions import BroadlinkException

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util.hass_dict import HassKey

from .const import (
    BROADLINK_DOMAIN,
    CONF_COMMANDS,
    CONF_FREQUENCY,
    CONF_TRANSMITTER,
    DOMAIN,
)
from .mercator import split_frames
from .rf import decode_broadlink_packet

_LOGGER = logging.getLogger(__name__)

# How long to wait before reopening the receiver after it failed: the
# Broadlink is offline or reloading, or another capture holds its receiver.
_RETRY_SECONDS = 30

# A held button keeps repeating and is heard more than once. A key heard again
# within this window, or heard just after we sent it ourselves, is the same
# press and is ignored.
DEBOUNCE_SECONDS = 2.0

# A capture holds one frame repeated. A space no longer than the fixed 1.3 ms
# split mercator.py uses never ends a frame.
_MIN_FRAME_GAP_US = 1300

# Two pulses are the same if they differ by at most 150 us or 30 % of the
# longer one: room for receiver jitter, while a one-unit pulse still never
# passes for a two-unit one.
_TOLERANCE_US = 150
_TOLERANCE_RATIO = 0.3

# A frame of fewer pulses is too generic to tell one button from another.
_MIN_PULSES = 16


def _repeats(timings: list[int], gap: float) -> list[list[int]]:
    """Cut a capture at the spaces longer than ``gap``; return every copy of
    its most common frame, or nothing if no frame is long enough."""
    frames = [f for f in split_frames(timings, gap) if len(f) >= _MIN_PULSES]
    if not frames:
        return []
    size = Counter(len(f) for f in frames).most_common(1)[0][0]
    return [f for f in frames if len(f) == size]


@lru_cache(maxsize=256)
def _reference(timings: tuple[int, ...]) -> tuple[list[int], float] | None:
    """Return a learned code's frame and the gap that separates its repeats.

    Besides the code, a capture can hold a stray pulse and a silence longer
    than the gap between repeats, or a pause between two bursts of the code.
    So half of each long space is tried as the gap, and the longest one that
    cuts at least half as many copies of a frame as the best one does is
    kept. Cutting at a silence leaves one or two pieces, while cutting inside
    the frames finds more copies only when part of a frame repeats within it.

    The frame is the per-pulse median of the copies, which votes out a pulse
    the receiver garbled in one of them. The gap returned for heard captures
    lies halfway between the longest space inside the frame and the shortest
    space the learned capture was cut at, leaving room for jitter both ways.
    The idle that ends a capture is not a gap between repeats.
    """
    spaces = [-t for t in timings[1:-1] if t < 0]
    cuts = {
        gap: _repeats(list(timings), gap)
        for gap in {
            _MIN_FRAME_GAP_US,
            *(space / 2 for space in spaces if space > 2 * _MIN_FRAME_GAP_US),
        }
    }
    most = max(map(len, cuts.values()))
    if not most:
        return None
    gap = max(gap for gap, repeats in cuts.items() if 2 * len(repeats) >= most)
    frame = [round(median(pulse)) for pulse in zip(*cuts[gap], strict=True)]
    inside = max((-t for t in frame if t < 0), default=0)
    if (between := min((s for s in spaces if s > gap), default=None)) is not None:
        gap = max(_MIN_FRAME_GAP_US, (inside + between) / 2)
    return frame, gap


def _close(a: int, b: int) -> bool:
    """Whether two signed pulse lengths agree within receiver jitter."""
    return (a > 0) == (b > 0) and abs(a - b) <= max(
        _TOLERANCE_US, _TOLERANCE_RATIO * max(abs(a), abs(b))
    )


def heard_matches(learned: list[int], heard: list[int]) -> bool:
    """Whether a heard capture holds a whole frame of a learned code."""
    if (reference := _reference(tuple(learned))) is None:
        return False
    frame, gap = reference
    return any(
        len(f) == len(frame) and all(map(_close, frame, f, strict=True))
        for f in split_frames(heard, gap)
    )


class PressTracker:
    """Tells a new press on the remote from a repeat of one, or from our send."""

    def __init__(self) -> None:
        """Start with nothing pressed."""
        self._last: dict[str, float] = {}

    def sent(self, key: str) -> None:
        """Note that we just transmitted ``key`` ourselves."""
        self._last[key] = time.monotonic()

    def heard(self, key: str) -> bool:
        """Note that ``key`` was heard; return True if it is a new press."""
        now = time.monotonic()
        last = self._last.get(key)
        self._last[key] = now
        return last is None or now - last > DEBOUNCE_SECONDS


def signal_heard(entry_id: str) -> str:
    """Dispatcher signal that carries each command key heard for an entry."""
    return f"{DOMAIN}_heard_{entry_id}"


def _broadlink_devices(hass: HomeAssistant) -> dict[str, Any]:
    """Return the Broadlink integration's devices, by config entry id."""
    return getattr(hass.data.get(BROADLINK_DOMAIN), "devices", None) or {}


class RfListener:
    """Holds one Broadlink's RF receiver open and reports what it hears."""

    def __init__(self, hass: HomeAssistant, device_id: str, frequency: int) -> None:
        """Listen through the Broadlink of config entry ``device_id``."""
        self.hass = hass
        self.device_id = device_id
        self.frequency = frequency  # Hz
        self.entry_ids: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    @callback
    def start(self) -> None:
        """Open the receiver in the background, unless it is already open."""
        if self._task is None:
            self._task = self.hass.async_create_background_task(
                self._run(), f"{DOMAIN} listener {self.device_id}"
            )

    async def async_stop(self) -> None:
        """Close the receiver and wait until the Broadlink is released."""
        if (task := self._task) is None:
            return
        self._task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        """Listen, reopening the receiver after failures."""
        while True:
            device = _broadlink_devices(self.hass).get(self.device_id)
            if device is not None:
                try:
                    await self._listen(device)
                except (BroadlinkException, OSError) as err:
                    _LOGGER.debug(
                        "rf_fan: stopped listening on %s (%s), retrying in %ss",
                        device.name,
                        err,
                        _RETRY_SECONDS,
                    )
            await asyncio.sleep(_RETRY_SECONDS)

    async def _listen(self, device: Any) -> None:
        """Hold the receiver open, dispatching every code it hears."""
        signals = device.api.capture_rf(
            0, frequency=self.frequency / 1_000_000, stop_after_first=False
        )
        async with aclosing(signals):
            _LOGGER.debug(
                "rf_fan: listening on %s at %.2f MHz",
                device.name,
                self.frequency / 1_000_000,
            )
            async for signal in signals:
                self._dispatch(signal.packet)

    @callback
    def _dispatch(self, packet: bytes) -> None:
        """Hand a heard code to the entities of the entry that learned it."""
        try:
            heard, _ = decode_broadlink_packet(packet)
        except IndexError:  # the declared length cuts a long pulse short
            _LOGGER.debug("rf_fan: ignoring a malformed capture %s", packet.hex())
            return
        # The same encoding as a learned code's "b64", to compare the two.
        _LOGGER.debug("rf_fan: heard %s", b64encode(packet).decode())
        for entry_id in self.entry_ids:
            if (entry := self.hass.config_entries.async_get_entry(entry_id)) is None:
                continue
            keys = [
                key
                for key, command in entry.data[CONF_COMMANDS].items()
                if heard_matches(command["timings"], heard)
            ]
            _LOGGER.debug(
                "rf_fan: heard %d pulses; %s matches %s",
                len(heard),
                entry.title,
                keys or "nothing",
            )
            # Two of an entry's buttons matching the same code is ambiguous.
            if len(keys) == 1:
                async_dispatcher_send(self.hass, signal_heard(entry_id), keys[0])


@dataclass
class _Listeners:
    """Every listener, by the Broadlink config entry whose receiver it holds."""

    by_device: dict[str, RfListener] = field(default_factory=dict)
    # Learn flows using a Broadlink's receiver; it is not listened on meanwhile.
    paused: Counter[str] = field(default_factory=Counter)


DATA_LISTENERS: HassKey[_Listeners] = HassKey(DOMAIN)


@callback
def async_setup_listening(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Follow the entry's remote through the Broadlink that transmits for it."""
    transmitter = er.async_get(hass).async_get(entry.data[CONF_TRANSMITTER])
    if (
        transmitter is None
        or transmitter.platform != BROADLINK_DOMAIN
        or transmitter.config_entry_id is None
    ):
        _LOGGER.debug(
            "rf_fan: %s does not transmit through a Broadlink, not listening",
            entry.title,
        )
        return
    device_id = transmitter.config_entry_id
    frequency = entry.data[CONF_FREQUENCY]
    data = hass.data.setdefault(DATA_LISTENERS, _Listeners())
    if (listener := data.by_device.get(device_id)) is None:
        listener = data.by_device[device_id] = RfListener(hass, device_id, frequency)
    elif listener.frequency != frequency:
        _LOGGER.warning(
            "rf_fan: %s uses %.2f MHz but its Broadlink already listens at "
            "%.2f MHz for another device, so its remote will not be heard",
            entry.title,
            frequency / 1_000_000,
            listener.frequency / 1_000_000,
        )
        return
    listener.entry_ids.add(entry.entry_id)
    if not data.paused[device_id]:
        listener.start()

    async def _async_stop_listening() -> None:
        listener.entry_ids.discard(entry.entry_id)
        if not listener.entry_ids:
            if data.by_device.get(device_id) is listener:
                del data.by_device[device_id]
            await listener.async_stop()

    entry.async_on_unload(_async_stop_listening)


async def async_pause_listening(hass: HomeAssistant, device_id: str) -> None:
    """Release a Broadlink's receiver so a learn flow can use it."""
    data = hass.data.setdefault(DATA_LISTENERS, _Listeners())
    data.paused[device_id] += 1
    if (listener := data.by_device.get(device_id)) is not None:
        await listener.async_stop()


@callback
def async_resume_listening(hass: HomeAssistant, device_id: str) -> None:
    """Take the receiver back once the learn flow is over."""
    data = hass.data[DATA_LISTENERS]
    data.paused[device_id] -= 1
    listener = data.by_device.get(device_id)
    if not data.paused[device_id] and listener is not None:
        listener.start()
