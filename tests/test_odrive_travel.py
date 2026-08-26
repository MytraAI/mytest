"""turns_traveled: the odrive driver's own count of the path the axis took.

Computed rather than read off the board, which makes it the one telemetry channel
whose correctness lives in this repository instead of in the firmware. Two things
are worth holding down: that it counts the PATH and not the displacement, and that
writing pos_estimate - which renames where the axis is without moving it - books no
travel.
"""
from __future__ import annotations

import asyncio

import pytest

from hardware.odrive.mock_backend import MockOdriveBackend
from hardware.odrive.odrive_backend import OdriveBackend


def _frames(backend, count, before_each=None):
    """`count` frames off a mock backend's stream, optionally acting between them."""
    async def run():
        await backend.connect()
        stream = backend.stream_samples().__aiter__()
        seen = []
        for index in range(count):
            if before_each is not None:
                await before_each(backend, index)
            seen.append(await stream.__anext__())
        return seen
    return asyncio.run(run())


def _accumulate(positions, writes_before=()):
    """Drive the real driver's accumulator over a series of pos_estimate readings, bumping
    the write counter before the frames named in `writes_before`."""
    backend = OdriveBackend()
    for index, position in enumerate(positions):
        if index in writes_before:
            backend._pos_estimate_writes += 1
        frame = {"pos_estimate": position}
        backend._accumulate_turns_traveled(frame)
    return frame["turns_traveled"]


def test_it_counts_the_path_and_not_the_distance_between_the_ends():
    """The whole reason it exists. A leg that runs 17 turns past its target and is pulled
    back covers that ground twice, and no arithmetic on where the move started and stopped
    can see it: 110 -> -17 -> +7 is 144 turns of track, not the 103 between the ends."""
    assert _accumulate([110.0, 40.0, -17.0, 0.0, 7.0]) == pytest.approx(151.0)


def test_the_first_frame_is_a_starting_point_and_not_travel():
    """Whatever the axis happens to read when the driver connects is not ground covered."""
    assert _accumulate([500.0]) == 0.0
    assert _accumulate([500.0, 501.5]) == pytest.approx(1.5)


def test_writing_pos_estimate_books_no_travel():
    """set_pos_estimate renames where the axis IS - the firmware shifts input_pos and
    pos_setpoint with it and moves nothing. Unguarded, setup's own write books whatever the
    board read at power-up: 125 turns, 10.5 m, in the 2026-08-25 14:23 run."""
    # ... 0 -> 2 turns of real travel, then renamed to 125, then 1 more turn
    assert _accumulate([0.0, 2.0, 125.0, 126.0], writes_before={2}) == pytest.approx(3.0)


def test_a_write_only_skips_the_one_frame_that_crosses_it():
    """At most one frame of real travel is given up, and the axis is idle behind a brake
    when setup writes."""
    assert _accumulate([0.0, 10.0, 40.0, 50.0], writes_before={2}) == pytest.approx(20.0)


def test_a_frame_with_no_position_is_skipped_rather_than_counted_as_zero():
    """A None would otherwise read as the axis having jumped to the origin and back."""
    backend = OdriveBackend()
    for position in (10.0, None, 12.0):
        frame = {"pos_estimate": position}
        backend._accumulate_turns_traveled(frame)
    assert frame["turns_traveled"] == pytest.approx(2.0)


def test_the_mock_accounts_for_travel_the_same_way_as_the_real_driver():
    """A test cannot tell which backend it is running against from the channel list, so it
    must not be able to tell from the numbers either."""
    async def command(backend, index):
        if index == 0:
            await backend.execute("set_axis_state", state="CLOSED_LOOP_CONTROL")
            await backend.execute("set_position", value=20.0)

    frames = _frames(MockOdriveBackend(), 40, command)
    travelled = [f["turns_traveled"] for f in frames]

    # From the first OBSERVED position, not from zero: the driver cannot know where the
    # axis was before it connected, which is the same reason travelled[0] is 0.
    displacement = abs(frames[-1]["pos_estimate"] - frames[0]["pos_estimate"])

    assert travelled[0] == 0.0, "the first frame is a starting point, not travel"
    assert travelled == sorted(travelled), "a magnitude, so it never goes backwards"
    assert travelled[-1] >= displacement - 1e-9, "the path is never shorter than the displacement"


def test_the_mock_does_not_book_a_pos_estimate_write_as_travel():
    """The same guard, on the path taken when no board is attached."""
    async def command(backend, index):
        if index == 5:
            await backend.execute("set_pos_estimate", value=500.0)

    frames = _frames(MockOdriveBackend(), 8, command)

    assert frames[-1]["pos_estimate"] == pytest.approx(500.0, abs=1.0)
    assert frames[-1]["turns_traveled"] < 1.0, "a 500-turn rename is not 500 turns of track"
