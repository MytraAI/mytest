"""Disconnecting the ODrive while a frame is being read.

A frame is read attribute by attribute on a worker thread; disconnect() runs on the
event loop. Dropping the handle underneath a read raised `AttributeError: 'NoneType'
object has no attribute 'axis0'` out of _read_one - which runner.py treats as fatal
and _read_one's own comment reads as the device's attribute graph having changed
mid-run. It had not: the driver was shutting down, and reported it as a device fault,
logging CRITICAL on a teardown that was going correctly. That is the signal a real
driver death has to be told apart from.
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from hardware.odrive.odrive_backend import OdriveBackend


class FakeBoard:
    """A handle whose reads can be held open, so a test can disconnect mid-frame."""

    def __init__(self, block_after: int = 0):
        self.axis0 = self
        self.serial_number = "FAKE"
        self.reads = 0
        self._reached = threading.Event()
        self._release = threading.Event()
        self._block_after = block_after

    def read(self, root, path):
        self.reads += 1
        if self.reads == self._block_after:
            self._reached.set()
            self._release.wait(5.0)
        return 0.0

    def wait_until_reading(self, timeout=5.0):
        assert self._reached.wait(timeout), "the read never started"

    def let_the_read_finish(self):
        self._release.set()


def _backend(board):
    backend = OdriveBackend()
    backend._odrv = board
    backend._read_one = board.read
    # Set by connect() on a real board; disconnect() idles the axis through it.
    backend._AxisState = SimpleNamespace(IDLE=1)
    return backend


def test_disconnect_waits_for_the_frame_already_being_read():
    """The wait is the fix: teardown is tens of milliseconds behind one frame, and the
    read completes against a handle that is still there."""
    board = FakeBoard(block_after=3)
    backend = _backend(board)
    result = {}

    def read_a_frame():
        result["frame"] = backend._read_all_channels()

    reader = threading.Thread(target=read_a_frame, daemon=True)
    reader.start()
    board.wait_until_reading()

    disconnected = threading.Event()

    def disconnect():
        asyncio.run(backend.disconnect())
        disconnected.set()

    closer = threading.Thread(target=disconnect, daemon=True)
    closer.start()

    assert not disconnected.wait(0.2), "disconnect must not drop the handle mid-frame"
    assert backend._odrv is board, "the handle is still the reader's"

    board.let_the_read_finish()
    reader.join(5.0)
    closer.join(5.0)

    assert disconnected.is_set(), "disconnect must complete once the frame is done"
    assert backend._odrv is None
    assert result["frame"] is not None, "the in-flight frame still came back whole"


def test_a_read_that_starts_after_the_handle_is_gone_is_not_a_fault():
    """stream_samples() tests the handle on the event loop and the read starts later on
    a worker thread, so disconnect can land between the two. That is a shutdown, and
    has to report as no frame rather than as an AttributeError from _read_one."""
    backend = _backend(FakeBoard())
    asyncio.run(backend.disconnect())

    assert backend._read_all_channels() is None


def test_a_frame_is_still_read_normally_when_nothing_disconnects():
    board = FakeBoard()
    backend = _backend(board)

    frame = backend._read_all_channels()

    assert frame is not None and board.reads > 0


def test_the_stream_idles_rather_than_raising_on_a_frame_that_was_not_read():
    """The whole point: a driver shutting down must not look like a driver dying.

    Poses the race directly rather than trying to win it - the handle is still set, so
    stream_samples() gets past its own test, and the read reports None the way it does
    when disconnect() lands in between. Without the guard the None reaches
    _accumulate_turns_traveled and raises, which is the fatal path all over again."""
    backend = _backend(FakeBoard())
    backend._read_all_channels = lambda: None

    async def drive():
        stream = backend.stream_samples()
        try:
            await asyncio.wait_for(anext(stream), timeout=0.3)
        except asyncio.TimeoutError:
            return "idled"
        finally:
            await stream.aclose()
        return "yielded"

    assert asyncio.run(drive()) == "idled", "a frame that was not read must not be yielded"


def test_the_stream_still_yields_a_frame_that_was_read():
    """The guard must not swallow the healthy case."""
    backend = _backend(FakeBoard())

    async def first():
        stream = backend.stream_samples()
        try:
            return await asyncio.wait_for(anext(stream), timeout=1.0)
        finally:
            await stream.aclose()

    assert asyncio.run(first()) is not None
