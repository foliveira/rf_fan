"""Matching what the receiver hears against learned codes."""

from itertools import groupby, product

from custom_components.rf_fan.listener import heard_matches
from custom_components.rf_fan.rf import decode_broadlink_packet

from .common import burst, capture

EV1527 = [0xA5A501, 0xA5A502, 0xA5A504, 0xA5A508, 0xA5A510, 0xA5A503]


def _t(packet: bytes) -> list[int]:
    return decode_broadlink_packet(packet)[0]


def test_ev1527_buttons_are_told_apart() -> None:
    for learned, heard in product(EV1527, EV1527):
        hit = heard_matches(
            _t(burst(learned)),
            _t(burst(heard, repeats=4, skip=13, jitter=60, seed=heard)),
        )
        assert hit == (learned == heard), (hex(learned), hex(heard))


def test_slow_remote_whose_bit_gaps_exceed_the_fixed_split() -> None:
    """Base 470 us: a zero bit's 1.4 ms low is longer than mercator.py's 1.3 ms
    frame split, so frames must be cut at the sync gaps instead."""
    for learned, heard in product(EV1527, EV1527):
        hit = heard_matches(
            _t(burst(learned, base=470)),
            _t(burst(heard, base=470, repeats=3, skip=7, jitter=60, seed=heard)),
        )
        assert hit == (learned == heard), (hex(learned), hex(heard))


def _mercator(bits: str, repeats: int = 6, gap_cells: int = 6) -> list[int]:
    """Mercator FRM97-style train: 333 us cells, one = 110, zero = 010."""
    frame = "".join("110" if b == "1" else "010" for b in bits)
    cells = ("0" * gap_cells).join([frame] * repeats)
    timings = [
        len(list(run)) * 333 * (1 if level == "1" else -1)
        for level, run in groupby(cells)
    ]
    return timings[1:] if timings[0] < 0 else timings


# One-hot button field: the buttons differ in which bit is cleared.
MERCATOR = ["1011001110" + "1" * i + "0" + "1" * (5 - i) for i in range(6)]


def test_mercator_one_hot_buttons_are_told_apart() -> None:
    for learned, heard in product(MERCATOR, MERCATOR):
        hit = heard_matches(
            _t(capture(_mercator(learned))),
            _t(capture(_mercator(heard, repeats=4), skip=9, jitter=50, seed=3)),
        )
        assert hit == (learned == heard), (learned, heard)


def test_a_garbled_frame_does_not_hide_the_others() -> None:
    learned = _t(burst(EV1527[1]))
    heard = _t(burst(EV1527[1], repeats=4, skip=6))  # frames at 44, 94, 144
    heard[61] = -4000  # receiver dropout inside the first whole frame
    heard[110] = 30  # a glitch inside the second
    assert heard_matches(learned, heard)
    del heard[144:]
    assert not heard_matches(learned, heard)
    garbled = list(learned)
    garbled[55] = -3000  # one garbled repeat in the learned capture
    assert heard_matches(garbled, _t(burst(EV1527[1], repeats=3, skip=6)))


def test_noise_matches_nothing() -> None:
    learned = _t(burst(EV1527[0]))
    assert not heard_matches(learned, [])
    assert not heard_matches(learned, [300, -300, 300])
    assert not heard_matches([], _t(burst(EV1527[0])))
