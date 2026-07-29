"""Wire schemas and endpoint constants for the command server (REQ/REP)
and telemetry server/publisher (PUB/SUB).

Both speak JSON payloads over ZeroMQ. Keeping the schemas in one module
means every process on the wire - hardware drivers, the testcase
execution process, the telemetry engine - imports the same contract
instead of hand-rolling its own message shapes.

Lives under protocol/ rather than hardware/ because most of what's here
isn't hardware-specific: TaggedTelemetryFrame and
DEFAULT_TAGGED_TELEMETRY_ENDPOINT are a contract between the testcase
execution process (which publishes) and the telemetry engine (which
consumes), neither of which is a hardware driver. protocol/ is the one
home for anything two processes have to agree on - see the package's
sibling verdict.py, and AI/Mytest.md's process architecture, whose rule
is that processes communicate only through defined interfaces, never by
importing each other.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

# Default ZeroMQ endpoints. Override via CLI args / config for
# multi-stand deployments or when client and server run on different
# hosts.
DEFAULT_COMMAND_ENDPOINT = "tcp://127.0.0.1:5555"
DEFAULT_TELEMETRY_ENDPOINT = "tcp://127.0.0.1:5556"
DEFAULT_TAGGED_TELEMETRY_ENDPOINT = "tcp://127.0.0.1:5557"

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

# Topic used on the Telemetry Publisher's tagged PUB socket - the testcase
# execution process's outbound feed to the Telemetry Aggregator.
TAGGED_TELEMETRY_TOPIC = b"telem_tagged"

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
class TaggedTelemetryFrame:
    """A TelemetryFrame republished by the Telemetry Publisher with
    test-case context attached.

    `seq` is carried over unchanged from the originating TelemetryFrame
    (not reassigned), so a subscriber can correlate a tagged frame with
    its raw counterpart directly.

    `test_id` is a random identifier unique to one test run. `test_name`
    is the stable identifier of the test *type* (e.g. a TestCase
    subclass's TEST_NAME) - evaluation looks up which Rulebooks apply to
    a frame by `test_name`, since `test_id` differs on every run.

    `channels` may include test-case-published state values (e.g. a
    gating flag) merged in alongside real hardware channels - see
    TelemetryPublisher.set_state()."""

    test_id: str
    test_name: str
    seq: int
    t: float
    channels: Dict[str, Any]
    device: str = UNKNOWN_DEVICE

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "TaggedTelemetryFrame":
        data = json.loads(raw.decode("utf-8"))
        return cls(
            test_id=data["test_id"],
            test_name=data["test_name"],
            seq=data["seq"],
            t=data["t"],
            channels=data["channels"],
            device=data.get("device", UNKNOWN_DEVICE),
        )

    @classmethod
    def from_telemetry_frame(
        cls, frame: TelemetryFrame, test_id: str, test_name: str, extra_channels: Optional[Dict[str, Any]] = None
    ) -> "TaggedTelemetryFrame":
        channels = {**frame.channels, **(extra_channels or {})}
        return cls(
            test_id=test_id,
            test_name=test_name,
            seq=frame.seq,
            t=frame.t,
            channels=channels,
            device=frame.device,
        )
