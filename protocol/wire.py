"""Wire schemas and endpoint constants for the command server (REQ/REP),
the per-device telemetry servers (PUB/SUB), and the testcase process's
run-state stream (PUB/SUB).

Both speak JSON payloads over ZeroMQ. Keeping the schemas in one module
means every process on the wire - hardware drivers, the testcase
execution process, the telemetry engine - imports the same contract
instead of hand-rolling its own message shapes.

Lives under protocol/ rather than hardware/ because most of what's here
isn't hardware-specific: RunStateFrame and DEFAULT_RUN_STATE_ENDPOINT
are a contract between the testcase execution process (which publishes)
and the telemetry engine and operator tooling (which consume), none of
which is a hardware driver. protocol/ is the one home for anything two
processes have to agree on - see the package's sibling verdict.py, and
AI/Mytest.md's process architecture, whose rule is that processes
communicate only through defined interfaces, never by importing each
other.

**Telemetry is relayed by nobody.** Each driver publishes its own frames
once; the telemetry engine subscribes to every device directly and
attributes frames to a run itself, using the run-state stream to learn
which run is open and which devices belong to it. The testcase process
therefore never republishes telemetry - it only publishes its own small
state stream. That is why there is no "tagged" frame type here: the
engine merges state into the rows it writes, so the relay hop, and the
double-write it implied, don't exist.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# Default ZeroMQ endpoints. Override via CLI args / config for
# multi-stand deployments or when client and server run on different
# hosts.
DEFAULT_COMMAND_ENDPOINT = "tcp://127.0.0.1:5555"
DEFAULT_TELEMETRY_ENDPOINT = "tcp://127.0.0.1:5556"

# The testcase execution process's own state stream - one small message at
# STATE_PUBLISH_INTERVAL_S carrying the open run's identity, the devices it
# claims, and whatever the test has published via set_state(). Consumed by the
# telemetry engine (to attribute frames to a run) and by operator tooling (to
# show live status and to discover the running test's id).
DEFAULT_RUN_STATE_ENDPOINT = "tcp://127.0.0.1:5557"

# A power supply is a separate device/process from the DAQ above, so it
# gets its own port pair - both can run simultaneously.
DEFAULT_POWER_SUPPLY_COMMAND_ENDPOINT = "tcp://127.0.0.1:5560"
DEFAULT_POWER_SUPPLY_TELEMETRY_ENDPOINT = "tcp://127.0.0.1:5561"

# The DUT itself is a separate device/process too - its own command and
# control channel, distinct from the instruments (DAQ, power supply)
# that surround it.
DEFAULT_DUT_COMMAND_ENDPOINT = "tcp://127.0.0.1:5570"
DEFAULT_DUT_TELEMETRY_ENDPOINT = "tcp://127.0.0.1:5571"

# The ODrive motor controller is its own device/process too - the first
# real (non-simulated) hardware this framework talks to, over USB via
# the official odrive package (see hardware/odrive/).
DEFAULT_ODRIVE_COMMAND_ENDPOINT = "tcp://127.0.0.1:5580"
DEFAULT_ODRIVE_TELEMETRY_ENDPOINT = "tcp://127.0.0.1:5581"

# Single topic used on the telemetry PUB socket, kept as one constant
# rather than per-channel topics because subscribers currently want
# the full frame. Splitting by channel is an easy future change if a
# subscriber ever needs to filter server-side.
TELEMETRY_TOPIC = b"telem"

# Topic used on the testcase process's test-state PUB socket.
RUN_STATE_TOPIC = b"run_state"

STATE_PUBLISH_INTERVAL_S = 0.05
"""How often the testcase process republishes its state, unconditionally.

Deliberately not change-triggered. Publishing on a fixed tick makes one
mechanism serve three purposes at once: it propagates a change (within one
tick), it is the keepalive that tells the engine the run is still open, and it
heals ZeroMQ's slow-joiner drop - a subscriber that missed the first message
gets the next one 50 ms later rather than waiting for the test to change
something.

20 Hz is well above any device's frame rate here, so the state the engine
merges into a row is never more than one tick stale, and the traffic is a few
hundred bytes per message against telemetry measured in kilobytes per frame."""

# Which device a telemetry frame came from, carried on the frame itself
# rather than inferred from which endpoint a subscriber happened to
# connect to. Needed once more than one device streams into one test:
# storage keys per-device files by it, and two devices could otherwise
# declare the same channel name (a DAQ and an ODrive can both plausibly
# publish "temperature") with no way to tell the values apart. Set by the
# driver process that publishes the frame - the only participant that
# knows what it's driving.
UNKNOWN_DEVICE = "unknown"

# The device names themselves, kept here next to the endpoint constants so
# a driver, the engine's storage layout, and any analysis of the stored
# files all spell them identically. These become directory names under a
# run (see protocol/paths.py), so they stay lowercase and path-safe.
DEVICE_DAQ = "daq"
DEVICE_POWER_SUPPLY = "power_supply"
DEVICE_DUT = "dut"
DEVICE_ODRIVE = "odrive"

# Every device this stand knows about, and where it publishes telemetry.
#
# One mapping rather than a constant per device, because two callers need to
# enumerate rather than name: the telemetry engine subscribes to *all* of them
# (its record's job is breadth - it records whatever is streaming, test or no
# test), and a test's declared device set is validated against these keys
# before it starts, so a test can't declare a device nothing is recording.
#
# A testbed or DUT façade declares which of these devices it owns (see their
# DEVICES tuples); it never names an endpoint, because a port is transport
# detail and the device name is what already travels on every frame and names
# the device's output directory.
TELEMETRY_ENDPOINTS: Dict[str, str] = {
    DEVICE_DAQ: DEFAULT_TELEMETRY_ENDPOINT,
    DEVICE_POWER_SUPPLY: DEFAULT_POWER_SUPPLY_TELEMETRY_ENDPOINT,
    DEVICE_DUT: DEFAULT_DUT_TELEMETRY_ENDPOINT,
    DEVICE_ODRIVE: DEFAULT_ODRIVE_TELEMETRY_ENDPOINT,
}

# High-water marks. ZeroMQ's default is 1000 *messages* on both ends, and
# PUB silently drops once its queue is full. Expressed here in seconds of
# buffer instead of a raw count, because the useful question is "how long
# may a consumer stall before we lose data", and one count means very
# different things at each device's sample rate.
#
# Deliberately modest rather than huge. Storage writes are drained by a
# separate task (see telemetry_engine/main.py), so a socket queue should
# never grow for long; a very deep queue would only convert a slow writer
# into tens of seconds of invisible latency, where a shallow one surfaces
# it promptly as a counted drop. It also has to stay small enough that
# many devices can each hold one without adding up: a frame of ~100
# channels is roughly 5 KB, so 500 frames is about 2.5 MB per socket.
TELEMETRY_BUFFER_S = 10.0
DEFAULT_TELEMETRY_HWM = 500  # fallback for subscribers, which don't know the publisher's rate


def hwm_for_interval(sample_interval_s: float, buffer_s: float = TELEMETRY_BUFFER_S) -> int:
    """Frames-worth of high-water mark for a device sampling every
    `sample_interval_s`. Each backend already declares its own interval,
    so a publisher sizes its own buffer with no new number to maintain."""
    if sample_interval_s <= 0:
        return DEFAULT_TELEMETRY_HWM
    return max(1, int(buffer_s / sample_interval_s))



@dataclass
class CommandRequest:
    """A command sent from a CommandClient to the CommandServer."""

    cmd: str
    args: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "CommandRequest":
        data = json.loads(raw.decode("utf-8"))
        return cls(cmd=data["cmd"], args=data.get("args", {}), id=data["id"])


@dataclass
class CommandReply:
    """The CommandServer's reply to a single CommandRequest."""

    id: str
    ok: bool
    result: Any = None
    error: Optional[str] = None

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "CommandReply":
        data = json.loads(raw.decode("utf-8"))
        return cls(**data)


@dataclass
class TelemetryFrame:
    """One tick of decoded, timestamp-aligned channel data."""

    seq: int
    t: float
    channels: Dict[str, float]
    device: str = UNKNOWN_DEVICE

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "TelemetryFrame":
        data = json.loads(raw.decode("utf-8"))
        return cls(
            seq=data["seq"],
            t=data["t"],
            channels=data["channels"],
            device=data.get("device", UNKNOWN_DEVICE),
        )

    @classmethod
    def now(cls, seq: int, channels: Dict[str, float], device: str = UNKNOWN_DEVICE) -> "TelemetryFrame":
        return cls(seq=seq, t=time.time(), channels=channels, device=device)


@dataclass
class RunStateFrame:
    """The testcase execution process's periodic announcement of itself.

    Published at STATE_PUBLISH_INTERVAL_S for the whole life of a run, from
    before PreTestSetup until after PostTestTeardown, and consumed by two
    kinds of reader:

    - The telemetry engine, which uses it to decide where a device's frames
      belong. `test_id` names the open run's directory, `devices` is the set of
      devices that run claims, and `state` is merged into the rows the engine
      writes - so the recorded file still shows what the test was doing on
      every frame without any telemetry passing through the test process.
    - Operator tooling, which wants only this: the dashboard reads
      `state["test_status"]`, the stop tool reads `test_id`, the manual GUI
      reads test identity and per-bound status.

    The stream's *existence* is what says a run is open; its absence is what
    says the run is over. There is deliberately no start or end marker: the
    verdict file already distinguishes a clean end from a crash, more reliably
    than a message could, and finalizing on a marker would truncate frames
    still in the engine's write queue. See run_recorder.py.

    `test_name` is the stable identifier of the test *type* (a TestCase
    subclass's TEST_NAME); `test_id` is random per run. Both travel because
    offline replay looks up rulebooks by name while storage keys by id."""

    test_id: str
    test_name: str
    devices: List[str]
    state: Dict[str, Any]
    t: float

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "RunStateFrame":
        data = json.loads(raw.decode("utf-8"))
        return cls(
            test_id=data["test_id"],
            test_name=data["test_name"],
            devices=list(data.get("devices", ())),
            state=data.get("state", {}),
            t=data["t"],
        )

    @classmethod
    def now(cls, test_id: str, test_name: str, devices: Sequence[str], state: Dict[str, Any]) -> "RunStateFrame":
        return cls(test_id=test_id, test_name=test_name, devices=list(devices), state=dict(state), t=time.time())
