"""Shared message schemas and constants for the hardware driver's
command server (REQ/REP) and telemetry server (PUB/SUB).

Both servers speak JSON payloads over ZeroMQ. Keeping the schemas in
one module means the driver and any client (testcase execution
process, telemetry aggregator) import the same contract instead of
hand-rolling their own message shapes.
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

# Single topic used on the telemetry PUB socket, kept as one constant
# rather than per-channel topics because subscribers currently want
# the full frame. Splitting by channel is an easy future change if a
# subscriber ever needs to filter server-side.
TELEMETRY_TOPIC = b"telem"

# Topic used on the Telemetry Publisher's tagged PUB socket - the
# testcase execution process's outbound feed towards the (not yet
# built) Telemetry Aggregator.
TAGGED_TELEMETRY_TOPIC = b"telem_tagged"


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

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "TelemetryFrame":
        data = json.loads(raw.decode("utf-8"))
        return cls(seq=data["seq"], t=data["t"], channels=data["channels"])

    @classmethod
    def now(cls, seq: int, channels: Dict[str, float]) -> "TelemetryFrame":
        return cls(seq=seq, t=time.time(), channels=channels)


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
        )

    @classmethod
    def from_telemetry_frame(
        cls, frame: TelemetryFrame, test_id: str, test_name: str, extra_channels: Optional[Dict[str, Any]] = None
    ) -> "TaggedTelemetryFrame":
        channels = {**frame.channels, **(extra_channels or {})}
        return cls(test_id=test_id, test_name=test_name, seq=frame.seq, t=frame.t, channels=channels)
