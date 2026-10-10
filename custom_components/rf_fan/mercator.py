"""Mercator FRM87/FRM97 protocol helpers: read captured frames, then (later)
generate clean ones.

Per rtl_433 issue #2200 the remote is OOK at 433.92 MHz with a base cell of
~333us; each data bit spans 3 cells (symbol one = 110, symbol zero = 010). The
button is a one-hot field, so light / off / each speed are the *same* frame
differing in a single bit. Rather than reconstruct the exact bit map from the
(ambiguous) published notation, we work empirically from the user's own captured
frames - which is robust to the DIP-switch address and to Broadlink's timing.

A capture contains the frame repeated several times, separated by long gaps. We
split on those gaps, render each frame as a cell string (``H`` = high cell,
``l`` = low cell; a long pulse/gap = two cells), and take the most common frame
as the consensus - noise then shows up as low agreement.
"""

from __future__ import annotations

from collections import Counter

_IDLE_US = 20000  # drop leading/trailing idle longer than this
_FRAME_GAP_US = 1300  # a gap longer than this separates repeated frames
_LONG_US = 600  # a pulse/gap at least this long counts as two cells


def split_frames(timings: list[int], gap: float = _FRAME_GAP_US) -> list[list[int]]:
    """Trim idle, then split a capture into its repeated frames.

    Any space longer than ``gap`` microseconds ends a frame.
    """
    ts = [int(t) for t in timings]
    while ts and abs(ts[0]) > _IDLE_US:
        ts.pop(0)
    while ts and abs(ts[-1]) > _IDLE_US:
        ts.pop()

    frames: list[list[int]] = []
    current: list[int] = []
    for t in ts:
        if t < 0 and abs(t) > gap:
            if current:
                frames.append(current)
                current = []
            continue
        current.append(t)
    if current:
        frames.append(current)
    return frames


def frame_cells(frame: list[int]) -> str:
    """Render one frame as a cell string (H = high cell, l = low cell)."""
    cells: list[str] = []
    for t in frame:
        count = 2 if abs(t) >= _LONG_US else 1
        cells.append(("H" if t > 0 else "l") * count)
    return "".join(cells)


def consensus(timings: list[int]) -> tuple[str, int, int]:
    """Return (most-common frame cell string, frames-agreeing, total-frames)."""
    signatures = [frame_cells(f) for f in split_frames(timings) if f]
    if not signatures:
        return "", 0, 0
    signature, count = Counter(signatures).most_common(1)[0]
    return signature, count, len(signatures)


def clean_frame(timings: list[int], repeat_gap: int = -1800) -> list[int]:
    """Return one clean (consensus) frame's actual timings, with a trailing gap.

    A capture holds the frame repeated several times, some corrupted by the
    Broadlink receiver. We pick the most common (consensus) frame and return its
    real microsecond timings plus a trailing gap, so it can be retransmitted -
    repeated cleanly - without the noisy frames from the raw capture.
    """
    framed = [f for f in split_frames(timings) if f]
    if not framed:
        return []
    signatures = [frame_cells(f) for f in framed]
    target = Counter(signatures).most_common(1)[0][0]
    frame = framed[signatures.index(target)]
    return [*frame, repeat_gap]
