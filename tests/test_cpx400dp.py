"""The CPX400DP driver's parsing, verification and write-checking, against a
fake instrument.

This driver has no mock backend on purpose: its risk is not wrong device paths
but wrong *response parsing*, and a backend-level mock would replace the very
code most likely to be wrong. So the fake here stands in for the transport, and
everything above it - the query tables, the parsers, the limit-status decoding,
the error checking, the ceiling - is the real implementation.

FakeInstrument's replies are byte-exact transcripts recorded from a real
CPX400DP (Thurlby Thandar, serial 599542, firmware 2.03-4.12). That matters:
if the fake replied `OVP1 66.00` where the instrument really says `VP1 66.00`,
every test here would pass and the driver would fail on the bench. The
formats, the silence on an unimplemented mnemonic, and EER 100 on an
out-of-range write are all reproduced from measurements, not from the manual.
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Dict, List, Optional

import pytest

from hardware.backend import HardwareError, MissingChannelError
from hardware.cpx400dp.cpx400dp_backend import Cpx400dpBackend
from hardware.cpx400dp.cpx400dp_channels import (
    CACHED_CHANNELS,
    COMMAND_CHANNELS,
    METER_CHANNELS,
    STATE_CHANNELS,
    TELEMETRY_CHANNELS,
)
from protocol.wire import DEVICE_CPX400DP

IDENTITY = "THURLBY THANDAR, CPX400DP, 599542, 2.03-4.12"


def sync(fn):
    """Run an async test body to completion. A local decorator rather than
    pytest-asyncio, so this file adds no dependency - the backend's async
    surface is the only thing here that needs a loop, and one per test is
    plenty."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper

# Verbatim replies from the real instrument. Every format quirk here was
# observed, including the two that would break a naive parser: OVP<n>? answers
# with VP<n> and OCP<n>? with CP<n>.
BASELINE_REPLIES: Dict[str, str] = {
    "*IDN?": IDENTITY,
    "V1O?": "-0.005V",
    "V2O?": "-0.009V",
    "I1O?": "0.019A",
    "I2O?": "0.053A",
    "OP1?": "0",
    "OP2?": "0",
    "LSR1?": "2",
    "LSR2?": "3",
    "V1?": "V1 20.19",
    "V2?": "V2 47.62",
    "I1?": "I1 0.070",
    "I2?": "I2 20.202",
    "OVP1?": "VP1 66.00",
    "OVP2?": "VP2 66.00",
    "OCP1?": "CP1 22.00",
    "OCP2?": "CP2 22.00",
    "DELTAV1?": "DELTAV1 0.01",
    "DELTAV2?": "DELTAV2 0.01",
    "DELTAI1?": "DELTAI1 0.01",
    "DELTAI2?": "DELTAI2 0.01",
    "LSE1?": "0",
    "LSE2?": "0",
    "CONFIG?": "2",
    "RATIO?": "100.00",
    "IPADDR?": "169.254.229.133",
    "NETMASK?": "255.255.0.0",
    "NETCONFIG?": "DHCP",
    "EER?": "0",
    "*ESR?": "0",
    "IFLOCK": "1",
    "IFUNLOCK": "0",
    "IFLOCK?": "0",
}


class FakeInstrument:
    """Stands in for TtiSocketTransport. Answers only what it is given; an
    unknown query raises the same HardwareError the real transport raises on a
    read timeout, which is how an unimplemented mnemonic actually presents
    itself (the instrument answers with silence, not an error)."""

    def __init__(
        self,
        replies: Optional[Dict[str, str]] = None,
        unsupported: tuple = (),
        eer_after_write: Optional[Dict[str, str]] = None,
        esr_after_write: Optional[Dict[str, str]] = None,
    ):
        self.replies = dict(BASELINE_REPLIES if replies is None else replies)
        self.unsupported = set(unsupported)
        self.eer_after_write = eer_after_write or {}
        self.esr_after_write = esr_after_write or {}
        self.sent: List[str] = []
        self.is_open = False
        self.address = "fake:9221"
        self._lock = asyncio.Lock()
        self._last_write: Optional[str] = None

    async def open(self) -> None:
        self.is_open = True

    async def close(self) -> None:
        self.is_open = False

    def transaction(self):
        return self._lock

    async def query(self, command: str) -> str:
        async with self._lock:
            return await self.query_in_transaction(command)

    async def write(self, command: str) -> None:
        async with self._lock:
            await self.write_in_transaction(command)

    async def write_in_transaction(self, command: str) -> None:
        self.sent.append(command)
        self._last_write = command

    async def query_in_transaction(self, command: str) -> str:
        self.sent.append(command)
        if command in self.unsupported or command not in self.replies:
            raise HardwareError(f"no response to {command!r} (fake instrument: unimplemented)")
        # The error registers answer per the write that preceded them, so a
        # test can make one specific command be refused.
        if command == "EER?" and self._last_write in self.eer_after_write:
            return self.eer_after_write[self._last_write]
        if command == "*ESR?" and self._last_write in self.esr_after_write:
            return self.esr_after_write[self._last_write]
        return self.replies[command]


async def _connected(**kwargs: Any):
    fake = kwargs.pop("fake", None) or FakeInstrument()
    backend = Cpx400dpBackend(transport=fake, **kwargs)
    await backend.connect()
    return backend, fake


# --- declaration ------------------------------------------------------------


def test_declared_channels_have_no_duplicates_and_tiers_partition_cleanly():
    assert len(set(TELEMETRY_CHANNELS)) == len(TELEMETRY_CHANNELS)
    assert len(set(COMMAND_CHANNELS)) == len(COMMAND_CHANNELS)
    tiers = [set(STATE_CHANNELS), set(METER_CHANNELS), set(CACHED_CHANNELS)]
    for i, a in enumerate(tiers):
        for b in tiers[i + 1:]:
            assert a.isdisjoint(b), f"a channel is declared in two tiers: {sorted(a & b)}"
    assert set(TELEMETRY_CHANNELS) == set().union(*tiers)


def test_backend_declares_its_device_and_interval():
    backend = Cpx400dpBackend(transport=FakeInstrument())
    assert backend.device == DEVICE_CPX400DP
    assert backend.sample_interval_s > 0


# --- connect ----------------------------------------------------------------


@sync
async def test_connect_reads_identity_and_clears_stale_error_registers():
    """*CLS at connect is not housekeeping. The error registers outlive the
    socket, so without it the first write's check could raise on an error left
    behind by a process that died earlier."""
    backend, fake = await _connected()
    assert backend.is_connected
    assert "*CLS" in fake.sent
    assert fake.sent.index("*IDN?") < fake.sent.index("*CLS")
    status = await backend.get_status()
    assert status["serial_number"] == "599542"
    assert status["firmware"] == "2.03-4.12"


@sync
async def test_connecting_twice_is_a_no_op_not_a_second_socket():
    """runner.run() connects at process start and a client then sends `connect`
    over the wire, as every testbed here does. This instrument permits a single
    raw socket, so a second open is refused - the redundant call has to be
    absorbed or the working link is reported as a failure."""
    backend, fake = await _connected()
    fake.sent.clear()
    await backend.connect()
    assert fake.sent == [], "a redundant connect must not touch the instrument"
    assert backend.is_connected


@sync
async def test_connect_refuses_an_instrument_that_is_not_a_cpx400dp():
    """The address is link-local and can move to another device. Reachability
    is not identity."""
    fake = FakeInstrument({**BASELINE_REPLIES, "*IDN?": "THURLBY THANDAR, PL303QMD, 1234, 1.00-1.00"})
    backend = Cpx400dpBackend(transport=fake)
    with pytest.raises(HardwareError, match="PL303QMD"):
        await backend.connect()
    assert not fake.is_open, "a failed connect must not leave the instrument's single socket slot held"


@sync
async def test_connect_names_declared_channels_the_firmware_does_not_implement():
    """An unimplemented mnemonic is answered with silence, so it costs a full
    read timeout. Undetected, that is a whole run streaming at a crawl."""
    fake = FakeInstrument(unsupported=("DELTAV2?", "LSE2?"))
    backend = Cpx400dpBackend(transport=fake)
    with pytest.raises(MissingChannelError) as excinfo:
        await backend.connect()
    assert "delta_voltage_2" in str(excinfo.value)
    assert "limit_status_enable_2" in str(excinfo.value)
    assert "setpoint_voltage_1" not in str(excinfo.value)


@sync
async def test_a_dead_link_is_reported_as_a_dead_link_not_as_missing_channels():
    fake = FakeInstrument(replies={"*IDN?": IDENTITY})
    backend = Cpx400dpBackend(transport=fake)
    with pytest.raises(HardwareError, match="unresponsive"):
        await backend.connect()


@sync
async def test_connect_does_not_change_any_output_or_setting():
    """Passive connect: it may clear its own error registers and nothing else."""
    backend, fake = await _connected()
    writes = [c for c in fake.sent if not c.endswith("?") and c != "*CLS"]
    assert writes == [], f"connect wrote to the instrument: {writes}"


@sync
async def test_a_frame_read_after_disconnect_yields_nothing_rather_than_raising():
    """A `disconnect` arriving over the command wire runs concurrently with the
    streaming loop. If the socket can close between the loop's connected check
    and its read, the read raises - and runner.run() treats a telemetry task
    dying as a real device failure, so an orderly teardown would exit the
    process non-zero. It must be indistinguishable from idle instead."""
    backend, _ = await _connected()
    await backend.disconnect()
    assert await backend._read_frame() is None


@sync
async def test_disconnect_leaves_outputs_alone():
    backend, fake = await _connected()
    fake.sent.clear()
    await backend.disconnect()
    assert not any(c.startswith("OP") for c in fake.sent), "disconnect must not switch outputs"
    assert not backend.is_connected


@sync
async def test_interface_lock_is_taken_only_when_asked_and_released_on_disconnect():
    _, passive = await _connected()
    assert "IFLOCK" not in passive.sent

    backend, fake = await _connected(fake=FakeInstrument(), take_interface_lock=True)
    assert "IFLOCK" in fake.sent
    await backend.disconnect()
    assert "IFUNLOCK" in fake.sent


@sync
async def test_a_failed_unlock_does_not_make_the_driver_forget_it_holds_the_lock():
    """IFUNLOCK answers -1 when this interface has no authority to release the
    lock. Treating that as success would make disconnect() skip the release of
    a lock that is still held, shutting other interfaces out of writes."""
    fake = FakeInstrument({**BASELINE_REPLIES, "IFUNLOCK": "-1"})
    backend = Cpx400dpBackend(transport=fake, take_interface_lock=True)
    await backend.connect()
    assert backend._holds_interface_lock
    await backend.execute("interface_unlock")
    assert backend._holds_interface_lock, "a refused unlock must not clear the flag"


@sync
async def test_reset_drops_a_lock_the_instrument_has_cancelled():
    """*RST's documented defaults include 'Lock cancelled', so the flag must
    follow - otherwise teardown tries to release a lock that no longer exists."""
    backend, _ = await _connected(fake=FakeInstrument(), take_interface_lock=True)
    assert backend._holds_interface_lock
    await backend.execute("reset")
    assert not backend._holds_interface_lock


@sync
async def test_reading_the_limit_register_by_hand_still_feeds_the_latch():
    """LSR<n>? is read-and-clear, so an explicit read consumes bits the next
    frame would have latched. Without folding them in, calling this action
    quietly punches a hole in the record the latch exists to keep."""
    fake = FakeInstrument()
    backend = Cpx400dpBackend(transport=fake)
    await backend.connect()
    await backend.execute("clear_limit_status_latch_1")
    fake.replies["LSR1?"] = "8"  # over-current trip
    assert await backend.execute("read_limit_status_1") == 8
    fake.replies["LSR1?"] = "0"
    frame = await backend._read_frame()
    assert frame["limit_status_latched_1"] & (1 << 3), "the hand-read trip must survive in the latch"


# --- telemetry --------------------------------------------------------------


@sync
async def test_a_frame_carries_every_declared_channel():
    """What TelemetryClient.verify_channels() checks against a live process: a
    real frame's keys, not a static list."""
    backend, _ = await _connected()
    frame = await backend._read_frame()
    assert set(frame) == set(TELEMETRY_CHANNELS)


@sync
async def test_measured_values_parse_including_the_formats_that_break_naive_parsers():
    backend, _ = await _connected()
    frame = await backend._read_frame()
    assert frame["voltage_1"] == pytest.approx(-0.005)  # '-0.005V', unit suffix
    assert frame["current_2"] == pytest.approx(0.053)  # '0.053A'
    assert frame["setpoint_voltage_2"] == pytest.approx(47.62)  # 'V2 47.62', echoed mnemonic
    assert frame["ovp_1"] == pytest.approx(66.00)  # 'VP1 66.00' - NOT 'OVP1 66.00'
    assert frame["ocp_2"] == pytest.approx(22.00)  # 'CP2 22.00' - NOT 'OCP2 22.00'
    assert frame["output_enabled_1"] is False
    assert frame["ip_address"] == "169.254.229.133"


@sync
async def test_a_reply_in_the_wrong_shape_is_an_error_not_a_silently_wrong_number():
    fake = FakeInstrument({**BASELINE_REPLIES, "V1?": "OVP1 20.19"})
    backend = Cpx400dpBackend(transport=fake)
    with pytest.raises(HardwareError, match="expected a 'V1' reply"):
        await backend.connect()


@sync
async def test_limit_status_decodes_to_per_bit_channels():
    """LSR1? = 2 is bit 1: regulating in constant current."""
    backend, _ = await _connected()
    frame = await backend._read_frame()
    assert frame["limit_status_1"] == 2
    assert frame["in_cc_1"] is True
    assert frame["in_cv_1"] is False
    # LSR2? = 3 is bits 0 and 1 together.
    assert frame["in_cv_2"] is True and frame["in_cc_2"] is True
    assert frame["tripped_oc_2"] is False


@sync
async def test_the_latch_keeps_a_trip_that_lasted_less_than_one_frame():
    """The register clears on read, so a trip seen by one poll would otherwise
    be gone from the next - and from the record."""
    fake = FakeInstrument()
    backend = Cpx400dpBackend(transport=fake)
    await backend.connect()

    fake.replies["LSR1?"] = "8"  # bit 3: over-current trip, for exactly one frame
    frame = await backend._read_frame()
    assert frame["tripped_oc_1"] is True
    assert frame["limit_status_latched_1"] & (1 << 3)

    fake.replies["LSR1?"] = "1"  # trip gone, back to constant voltage
    frame = await backend._read_frame()
    assert frame["tripped_oc_1"] is False, "the instantaneous channel must follow the register"
    assert frame["limit_status_latched_1"] & (1 << 3), "the latch must remember it"

    previous = await backend.execute("clear_limit_status_latch_1")
    assert previous & (1 << 3)
    frame = await backend._read_frame()
    assert not (frame["limit_status_latched_1"] & (1 << 3))


@sync
async def test_only_the_state_tier_is_read_every_frame():
    """The meter tier is capped at the instrument's 4 Hz reading rate and the
    cached tier only changes when we write, so an ordinary frame costs four
    round-trips rather than eight."""
    backend, fake = await _connected()
    await backend._read_frame()
    fake.sent.clear()
    await backend._read_frame()
    assert sorted(fake.sent) == sorted(["OP1?", "OP2?", "LSR1?", "LSR2?"])


@sync
async def test_meter_values_are_held_between_reads():
    """Polling the meters per frame re-reads a register the instrument only
    refreshes four times a second."""
    from hardware.cpx400dp import cpx400dp_backend as backend_module

    backend, fake = await _connected()
    first = await backend._read_frame()
    assert first["voltage_1"] == pytest.approx(-0.005)

    # The instrument's reading changes, but the driver must not pick it up
    # until the meter interval has elapsed.
    fake.replies["V1O?"] = "4.995V"
    fake.sent.clear()
    held = await backend._read_frame()
    assert "V1O?" not in fake.sent, "the meter tier was re-read too soon"
    assert held["voltage_1"] == pytest.approx(-0.005), "the held value must be carried, not re-read"

    backend._meters_read_at -= backend_module.METER_INTERVAL_S
    fake.sent.clear()
    refreshed = await backend._read_frame()
    assert "V1O?" in fake.sent, "the meter tier was not re-read once due"
    assert refreshed["voltage_1"] == pytest.approx(4.995)


@sync
async def test_connect_primes_the_meters_so_the_first_frame_is_complete():
    """Without priming, every channel in the meter tier would be missing from
    frames for the first meter interval - and verify_channels() reads exactly
    one frame."""
    backend, _ = await _connected()
    frame = await backend._read_frame()
    assert set(frame) == set(TELEMETRY_CHANNELS)
    assert frame["current_2"] == pytest.approx(0.053)


# --- writes -----------------------------------------------------------------


@sync
async def test_a_write_is_checked_and_refreshes_what_it_changed():
    backend, fake = await _connected()
    fake.replies["V1?"] = "V1 12.00"
    fake.sent.clear()
    await backend.execute("set_voltage_1", value=12.0)
    assert fake.sent[0] == "V1 12.0"
    assert "EER?" in fake.sent and "*ESR?" in fake.sent
    frame = await backend._read_frame()
    assert frame["setpoint_voltage_1"] == pytest.approx(12.0)


@sync
async def test_a_silently_refused_write_raises():
    """Measured on hardware: `V2 999` leaves the setpoint untouched, answers
    nothing, and reports itself only as EER 100. Without this check the caller
    would be told the write succeeded."""
    fake = FakeInstrument(eer_after_write={"V2 999.0": "100"})
    backend = Cpx400dpBackend(transport=fake)
    await backend.connect()
    with pytest.raises(HardwareError, match="EER 100"):
        await backend.execute("set_voltage_2", value=999.0)


@sync
async def test_a_syntax_error_raises_even_though_eer_stays_zero():
    """The two registers catch different failures: an unrecognised mnemonic
    sets only *ESR? bit 5 and leaves EER? at 0."""
    fake = FakeInstrument(esr_after_write={"V1 5.0": "32"})
    backend = Cpx400dpBackend(transport=fake)
    await backend.connect()
    with pytest.raises(HardwareError, match="not understood"):
        await backend.execute("set_voltage_1", value=5.0)


@sync
async def test_a_verify_timeout_warns_rather_than_raising(caplog):
    """Bit 3 means the command completed but the output did not settle in 5 s -
    a large output capacitor does this. The setpoint was still applied."""
    fake = FakeInstrument(esr_after_write={"V1V 5.0": "8"})
    backend = Cpx400dpBackend(transport=fake)
    await backend.connect()
    await backend.execute("set_voltage_verify_1", value=5.0)
    assert "did not reach the commanded value" in caplog.text


@sync
async def test_enable_output_sends_an_integer_not_a_boolean():
    backend, fake = await _connected()
    fake.sent.clear()
    await backend.execute("enable_output_2", value=1)
    assert fake.sent[0] == "OP2 1"


# --- the driver-side ceiling ------------------------------------------------


@sync
async def test_the_ceiling_refuses_a_setpoint_before_it_reaches_the_wire():
    """The failure the instrument cannot catch: a value well inside its own
    60 V range and fatal to a 12 V load."""
    backend, fake = await _connected(max_voltage=15.0, max_current=2.0)
    fake.sent.clear()
    with pytest.raises(HardwareError, match="exceeds this backend's max_voltage"):
        await backend.execute("set_voltage_1", value=48.0)
    assert fake.sent == [], "nothing may reach the instrument once the ceiling refuses it"

    with pytest.raises(HardwareError, match="max_current"):
        await backend.execute("set_current_1", value=5.0)


@sync
async def test_the_ceiling_applies_to_the_verify_variant_too():
    backend, _ = await _connected(max_voltage=15.0)
    with pytest.raises(HardwareError, match="max_voltage"):
        await backend.execute("set_voltage_verify_2", value=48.0)


@sync
async def test_the_ceiling_catches_a_step_that_would_cross_it():
    """A step carries no value, so its result is predicted from the cached
    setpoint and step size."""
    fake = FakeInstrument({**BASELINE_REPLIES, "V1?": "V1 11.995", "DELTAV1?": "DELTAV1 0.01"})
    backend = Cpx400dpBackend(transport=fake, max_voltage=12.0)
    await backend.connect()
    fake.sent.clear()
    # 11.995 + 0.01 = 12.005, over the 12.0 ceiling.
    with pytest.raises(HardwareError, match="would set"):
        await backend.execute("increment_voltage_1")
    assert fake.sent == [], "nothing may reach the instrument once the ceiling refuses it"


@sync
async def test_a_step_that_stays_under_the_ceiling_is_allowed():
    fake = FakeInstrument({**BASELINE_REPLIES, "V1?": "V1 5.00", "DELTAV1?": "DELTAV1 0.01"})
    backend = Cpx400dpBackend(transport=fake, max_voltage=12.0)
    await backend.connect()
    fake.sent.clear()
    await backend.execute("increment_voltage_1")
    assert fake.sent[0] == "INCV1"


@sync
async def test_no_ceiling_by_default():
    backend, fake = await _connected()
    await backend.execute("set_voltage_1", value=48.0)
    assert "V1 48.0" in fake.sent


@sync
async def test_a_recalled_setup_above_the_ceiling_raises_and_says_it_is_applied():
    fake = FakeInstrument()
    backend = Cpx400dpBackend(transport=fake, max_voltage=15.0)
    await backend.connect()
    fake.replies["V1?"] = "V1 48.00"  # the store held something far above the ceiling
    with pytest.raises(HardwareError, match="IS NOW APPLIED"):
        await backend.execute("recall_setup_1", value=3)


# --- dispatch ---------------------------------------------------------------


@sync
async def test_every_declared_action_is_dispatchable():
    """The static import-time check proves the tables match the declaration;
    this proves execute() actually routes each one rather than falling through
    to 'unknown action'."""
    backend, fake = await _connected()
    for action in COMMAND_CHANNELS:
        params = {"value": 1} if _needs_value(action) else {}
        try:
            await backend.execute(action, **params)
        except HardwareError as exc:
            assert "unknown action" not in str(exc), f"{action} is declared but not dispatched"


def _needs_value(action: str) -> bool:
    from hardware.cpx400dp.cpx400dp_backend import _VALUE_COMMANDS

    return action in _VALUE_COMMANDS


@sync
async def test_an_undeclared_action_is_rejected():
    backend, _ = await _connected()
    with pytest.raises(HardwareError, match="unknown action"):
        await backend.execute("set_range_1", value=1)


@sync
async def test_a_value_command_without_a_value_is_rejected():
    backend, _ = await _connected()
    with pytest.raises(HardwareError, match="requires a 'value'"):
        await backend.execute("set_voltage_1")


@sync
async def test_commands_are_refused_before_connect():
    backend = Cpx400dpBackend(transport=FakeInstrument())
    with pytest.raises(HardwareError, match="not connected"):
        await backend.execute("set_voltage_1", value=1.0)


# --- the transport itself ---------------------------------------------------
#
# Everything above substitutes the transport. These exercise the real one
# against a scripted TCP server, since the line framing and the timeout
# behaviour are its own logic and nothing else covers them.


async def _serve(handler):
    """Run a one-connection TCP server on an ephemeral port; yield its port."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@sync
async def test_transport_frames_a_reply_and_strips_the_terminator():
    from hardware.cpx400dp.transport import TtiSocketTransport

    async def handler(reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                return
            writer.write(b"V1 12.34\r\n")
            await writer.drain()

    server, port = await _serve(handler)
    transport = TtiSocketTransport("127.0.0.1", port)
    await transport.open()
    assert await transport.query("V1?") == "V1 12.34"
    await transport.close()
    server.close()


@sync
async def test_transport_reports_silence_as_an_error_rather_than_hanging():
    """An unimplemented mnemonic is answered with silence, not an error."""
    from hardware.cpx400dp.transport import TtiSocketTransport

    async def handler(reader, writer):
        await reader.readline()
        await asyncio.sleep(10)

    server, port = await _serve(handler)
    transport = TtiSocketTransport("127.0.0.1", port, timeout_s=0.2, drain_timeout_s=0.05)
    await transport.open()
    with pytest.raises(HardwareError, match="does not implement"):
        await transport.query("RANGE1?")
    await transport.close()
    server.close()


@sync
async def test_a_late_reply_is_discarded_instead_of_answering_the_next_query():
    """The failure this prevents is silent: a reply that lands after its read
    gave up would be handed to the following query, and every read after that
    would report its neighbour's value. `OP<n>?` and `LSR<n>?` both answer bare
    integers, so that swap would pass every parser."""
    from hardware.cpx400dp.transport import TtiSocketTransport

    async def handler(reader, writer):
        await reader.readline()  # the query that will time out
        await asyncio.sleep(0.3)
        writer.write(b"LATE\r\n")  # arrives after the caller gave up
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                return
            writer.write(b"0.019A\r\n")
            await writer.drain()

    server, port = await _serve(handler)
    transport = TtiSocketTransport("127.0.0.1", port, timeout_s=0.1, drain_timeout_s=0.5)
    await transport.open()

    with pytest.raises(HardwareError):
        await transport.query("SLOW?")
    # Without the drain, this would return 'LATE' and every later read would be
    # one behind for the rest of the run.
    assert await transport.query("I1O?") == "0.019A"
    await transport.close()
    server.close()


@sync
async def test_transport_explains_a_refused_connection_as_the_single_socket_limit():
    from hardware.cpx400dp.transport import TtiSocketTransport

    server, port = await _serve(lambda r, w: None)
    server.close()
    await server.wait_closed()
    transport = TtiSocketTransport("127.0.0.1", port)
    with pytest.raises(HardwareError, match="only one raw-socket connection"):
        await transport.open()
