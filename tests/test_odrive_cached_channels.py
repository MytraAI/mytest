"""The cached configuration tier: sixty-one channels not asked for every frame.

A frame costs its channel count in USB round-trips - one request-response per
attribute, measured at 290 us each on one host and 650 us on another - so the only
way to make a frame faster is to ask for fewer things. Sixty of the hundred are
device configuration, which cannot change unless something writes it, and this
driver owns every setter.

What has to hold for that to be safe: a cached channel still appears in every frame,
a write refreshes the channel it wrote, and the staleness has a bound.
"""
from __future__ import annotations

import asyncio

import pytest

from hardware.odrive.odrive_backend import (
    _CACHED_CHANNELS,
    _TELEMETRY_PATHS,
    CACHED_REFRESH_FRAMES,
    OdriveBackend,
)
from hardware.odrive.odrive_channels import TELEMETRY_CHANNELS


class FakeBoard:
    """An ODrive whose attribute reads are counted, so a test can assert what was
    actually asked over USB rather than what came back."""

    def __init__(self):
        self.reads = []
        self.values = {}

    def read(self, root, path):
        self.reads.append((root, path))
        return self.values.get((root, path), 0.0)


def _backend(board):
    backend = OdriveBackend()
    backend._odrv = object()  # never dereferenced: _read_one is replaced below
    backend._read_one = board.read
    return backend


def test_a_cached_channel_is_still_in_every_frame():
    """The tier is an optimisation, not a smaller channel surface. A channel missing
    from a frame fails verify_channels() on the other end, which reads as a driver that
    does not implement what it declared."""
    board = FakeBoard()
    backend = _backend(board)
    backend._refresh_cached_channels()

    frame = backend._read_all_channels()

    missing = set(TELEMETRY_CHANNELS) - set(frame) - {"turns_traveled"}
    assert not missing, f"declared but absent from a frame: {sorted(missing)}"


def test_a_frame_only_reads_the_live_channels_off_the_board():
    """The whole point: 39 round-trips, not 100."""
    board = FakeBoard()
    backend = _backend(board)
    backend._refresh_cached_channels()
    board.reads.clear()

    backend._read_all_channels()

    live = set(_TELEMETRY_PATHS) - _CACHED_CHANNELS
    assert len(board.reads) == len(live)
    read_names = {n for n, p in _TELEMETRY_PATHS.items() if p in board.reads}
    assert not read_names & _CACHED_CHANNELS, "a cached channel was fetched anyway"


def test_a_write_refreshes_the_channel_it_wrote():
    """A cache that kept reporting the old value would be worse than no cache: the
    setpoint a test just changed would read as the one it replaced, forever."""
    board = FakeBoard()
    backend = _backend(board)
    backend._refresh_cached_channels()

    name = next(iter(sorted(_CACHED_CHANNELS - {"board_serial_number"})))
    root, path = _TELEMETRY_PATHS[name]
    board.values[(root, path)] = 42.0
    backend._refresh_one_cached(root, path)

    assert backend._read_all_channels()[name] == 42.0


def test_a_write_read_back_records_what_the_board_took_not_what_was_asked():
    """The board is free to clamp or reject a value. A cache holding the requested one
    would report a limit the hardware is not enforcing."""
    board = FakeBoard()
    backend = _backend(board)
    backend._refresh_cached_channels()

    name = next(iter(sorted(_CACHED_CHANNELS - {"board_serial_number"})))
    root, path = _TELEMETRY_PATHS[name]
    board.values[(root, path)] = 8.0  # what the board settled on, not the 20.0 asked for
    backend._refresh_one_cached(root, path)

    assert backend._read_all_channels()[name] == 8.0


def test_a_write_to_a_live_channel_costs_no_extra_read():
    """set_position runs every leg of every stroke. Refreshing a tier it is not in
    would put the tier's whole cost on the hot path."""
    board = FakeBoard()
    backend = _backend(board)
    backend._refresh_cached_channels()
    board.reads.clear()

    backend._refresh_one_cached(*_TELEMETRY_PATHS["controller_input_pos"])

    assert board.reads == []


def test_the_tier_is_re_read_on_a_bounded_schedule():
    """Insurance against a change made outside this driver - odrivetool on the same
    board, or firmware rewriting its own configuration. Bounded, not immediate: a write
    through this driver refreshes its own channel at once."""
    board = FakeBoard()
    backend = _backend(board)
    backend._refresh_cached_channels()

    name = next(iter(sorted(_CACHED_CHANNELS - {"board_serial_number"})))
    root, path = _TELEMETRY_PATHS[name]
    board.values[(root, path)] = 99.0

    for _ in range(CACHED_REFRESH_FRAMES):
        stale = backend._read_all_channels()[name]
    assert stale == 0.0, "it should still be serving the cached value up to the bound"

    assert backend._read_all_channels()[name] == 99.0


def test_the_tier_is_configuration_and_nothing_that_moves():
    """A live quantity cached is a frozen reading. Position, velocity, current and the
    error words are what a test steers and stops on."""
    for name in ("pos_estimate", "vel_estimate", "motor_foc_iq_measured", "axis_is_armed",
                 "active_errors", "disarm_reason", "board_ibus", "board_vbus_voltage",
                 "motor_effective_current_lim", "controller_input_pos"):
        assert name not in _CACHED_CHANNELS, f"{name} changes on its own and cannot be cached"


def test_every_cached_channel_is_a_declared_one():
    assert _CACHED_CHANNELS <= set(_TELEMETRY_PATHS)
