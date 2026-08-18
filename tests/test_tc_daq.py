"""The TC DAQ driver: what it makes of the device's stream, and what it refuses.

The device streams eight CSV fields per sample and takes no commands, so the
only thing that can be wrong is the reading of it. These run against a fake
transport - no serial port, no device.
"""
from __future__ import annotations

import asyncio
import functools

import pytest

from hardware.backend import HardwareError
from hardware.tc_daq.tc_daq_backend import PROBE_LINES, TcDaqBackend
from hardware.tc_daq.tc_daq_channels import CHANNEL_COUNT, TELEMETRY_CHANNELS
from hardware.tc_daq.transport import SerialLineTransport
from protocol.wire import DEVICE_TC_DAQ, TELEMETRY_ENDPOINTS

GOOD_LINE = "FAULT,FAULT,FAULT,22.508,22.242,21.367,22.383,22.422"
"""Verbatim from the device, with three channels unconnected."""

ALL_GOOD_LINE = "20.125,20.250,20.375,20.500,20.625,20.750,20.875,21.000"


def sync(fn):
    """Run an async test body to completion, as tests/test_cpx400dp.py does -
    a local decorator rather than a pytest-asyncio dependency."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeDevice:
    """Stands in for SerialLineTransport: hands out prepared lines, then repeats
    the last one forever, the way a device that never stops streaming does."""

    def __init__(self, lines=None):
        self.lines = list(lines if lines is not None else [GOOD_LINE] * 20)
        self.address = "fake:115200"
        self.is_open = False
        self.reads = 0
        self.closed_times = 0

    async def open(self):
        self.is_open = True

    async def close(self):
        self.is_open = False
        self.closed_times += 1

    async def read_line(self):
        self.reads += 1
        if len(self.lines) > 1:
            return self.lines.pop(0)
        return self.lines[0]

    async def discard_partial_line(self):
        await self.read_line()


async def _connected(lines=None):
    fake = FakeDevice(lines)
    backend = TcDaqBackend(transport=fake)
    await backend.connect()
    return backend, fake


async def _first_frame(backend):
    async for frame in backend.stream_samples():
        return frame


# --- reading the stream -----------------------------------------------------


@sync
async def test_a_faulted_channel_reads_as_no_temperature_rather_than_zero():
    """0.0 or a retained previous value would read as a real temperature. None
    lands as an empty cell, which replay reconstructs as an absent channel, so a
    bound on it returns no result instead of passing on a fabricated number."""
    backend, _ = await _connected()
    frame = await _first_frame(backend)

    assert frame["temperature_1_c"] is None
    assert frame["fault_1"] is True
    assert frame["temperature_4_c"] == 22.508
    assert frame["fault_4"] is False


@sync
async def test_the_fault_count_is_carried_so_one_bound_covers_every_channel():
    backend, _ = await _connected()
    assert (await _first_frame(backend))["fault_count"] == 3

    backend, _ = await _connected([ALL_GOOD_LINE] * 20)
    assert (await _first_frame(backend))["fault_count"] == 0


@sync
async def test_every_declared_channel_is_in_every_frame():
    """TelemetryClient.verify_channels() checks a declared channel against one
    live frame, so a channel that only appears sometimes fails a run - which is
    why a faulted channel carries None rather than being left out."""
    backend, _ = await _connected()
    frame = await _first_frame(backend)

    assert set(frame) == set(TELEMETRY_CHANNELS)


# --- refusing what it cannot read -------------------------------------------


@sync
async def test_connect_refuses_a_stream_of_the_wrong_shape():
    """Opening the port succeeds at any baud rate, and a wrong one delivers
    plausible-looking text. Without this check the driver would publish garbage
    under real channel names for a whole run."""
    with pytest.raises(HardwareError) as excinfo:
        await _connected(["1.0,2.0,3.0"] * 20)

    message = str(excinfo.value)
    assert "got 3" in message and "baud" in message


@sync
async def test_connect_refuses_a_field_that_is_neither_a_number_nor_fault():
    with pytest.raises(HardwareError, match="neither a number"):
        await _connected(["20.0,20.0,20.0,20.0,20.0,20.0,20.0,OPEN"] * 20)


@sync
async def test_a_refused_link_does_not_stay_open():
    """The next attempt would fail to open a port this one left held."""
    fake = FakeDevice(["nonsense"] * 20)
    backend = TcDaqBackend(transport=fake)

    with pytest.raises(HardwareError):
        await backend.connect()

    assert fake.is_open is False
    assert backend.is_connected is False


@sync
async def test_connect_reads_more_than_one_line_before_trusting_the_link():
    """A single fragment that survived the partial-line discard must not pass
    the check on its own."""
    fake = FakeDevice([GOOD_LINE, GOOD_LINE, "22.4"] + [GOOD_LINE] * 10)
    backend = TcDaqBackend(transport=fake)

    with pytest.raises(HardwareError):
        await backend.connect()
    assert PROBE_LINES > 1


@sync
async def test_connecting_twice_is_a_no_op():
    """runner.run() connects when the process starts, and a client then calls
    connect over the wire - so the second call must not re-open the port."""
    backend, fake = await _connected()
    reads_after_connect = fake.reads

    await backend.connect()

    assert fake.reads == reads_after_connect


# --- a bad line mid-run is counted, not fatal -------------------------------


@sync
async def test_an_unreadable_line_is_skipped_and_counted():
    """One garbled line on a marginal cable must not end a run that may be hours
    in - but the count is published, so the stored record shows it rather than
    only whatever scrolled past in a terminal."""
    # connect() consumes the partial-line discard plus PROBE_LINES before the
    # stream this test is about begins.
    lines = [GOOD_LINE] * (PROBE_LINES + 1) + ["", ALL_GOOD_LINE] * 10
    backend, _ = await _connected(lines)

    frames = []
    async for frame in backend.stream_samples():
        frames.append(frame)
        if len(frames) == 2:
            break

    assert all(frame["temperature_1_c"] == 20.125 for frame in frames), "a bad line was published"
    assert frames[0]["malformed_lines"] == 1
    assert frames[1]["malformed_lines"] == 2, "each bad line counts"


# --- a device with nothing to command ---------------------------------------


@sync
async def test_it_accepts_no_commands():
    """list_actions() answers with nothing, so a caller expecting an action
    fails at verify_actions() instead of having a write silently go nowhere."""
    backend, _ = await _connected()

    assert await backend.list_actions() == []
    with pytest.raises(HardwareError, match="accepts no commands"):
        await backend.execute("set_units", value="C")


@sync
async def test_status_reports_the_link_and_the_bad_line_count():
    backend, _ = await _connected()
    status = await backend.get_status()

    assert status["address"] == "fake:115200"
    assert status["channel_count"] == CHANNEL_COUNT
    assert status["malformed_lines"] == 0


# --- silence is the only symptom a dead link has ----------------------------


class SilentPort:
    """A pyserial port that never answers, which is what an unplugged device
    looks like: read() returns empty at its timeout rather than raising."""

    def readline(self):
        return b""

    def close(self):
        pass


@sync
async def test_a_silent_stream_is_reported_as_a_dead_link():
    """There is no command to probe with, so nothing arriving is the only
    evidence available - and a driver that quietly published nothing would leave
    a test believing its temperatures were simply unchanging."""
    transport = SerialLineTransport(read_timeout_s=0.01, silence_timeout_s=0.05)
    transport._serial = SilentPort()

    with pytest.raises(HardwareError, match="silent"):
        await transport.read_line()


# --- how the stand sees it --------------------------------------------------


def test_the_device_has_its_own_name_and_endpoint():
    """A device name keys a directory of recorded output, so this must not share
    DEVICE_DAQ's - the simulated DAQ publishes a different channel set entirely."""
    assert DEVICE_TC_DAQ in TELEMETRY_ENDPOINTS
    assert TELEMETRY_ENDPOINTS[DEVICE_TC_DAQ] not in [
        endpoint for device, endpoint in TELEMETRY_ENDPOINTS.items() if device != DEVICE_TC_DAQ
    ]
