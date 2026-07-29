"""Abstract interface between the hardware driver's servers and whatever
actually talks to a piece of test-stand hardware - a DAQ, a power supply,
or any other device that needs a command/telemetry pair.

The command and telemetry servers depend only on this interface, never on
a concrete implementation. They only require the universal core below:
connect/disconnect/get_status/stream_samples.

Alongside the interface, this module carries what every backend needs rather
than re-derives: the connected-state guard, the `device`/`sample_interval_s`
declarations that runner.py reads off the backend, and `to_jsonable()` for
putting device values on the JSON wire.

Everything device-specific - loading a DAQ setup, starting acquisition,
setting a power supply's output, enabling/disabling an output - goes
through `execute()`, which each concrete backend implements however fits
that device. This is what lets the same command/telemetry server code run
unchanged whether it's fronting `MockDaqBackend`, `MockPowerSupplyBackend`,
or a real device adapter: the server never needs to know what commands a
given device supports.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, List

from protocol.wire import UNKNOWN_DEVICE


def to_jsonable(value: Any) -> Any:
    """Coerce a raw device value into something json.dumps can serialize.

    Primitives pass through untouched; anything else is cast to int, then to
    str as a last resort, so one uncoercible channel can't fail the whole
    frame. Needed by real adapters, whose SDKs return enum-like and wrapper
    types; mock backends produce plain Python values and never hit it."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


class HardwareError(Exception):
    """Raised for any backend failure. The command server catches this
    and turns it into a CommandReply(ok=False, error=str(exc)) instead
    of letting it crash the process."""


class MissingChannelError(Exception):
    """Raised when an expected telemetry or command channel isn't actually
    present. Three places raise it: a backend confirming its declared
    channels against the real device at connect(), a live telemetry frame
    missing an expected key (TelemetryClient.verify_channels()), and a
    backend's real list_actions() not including an expected action
    (CommandClient.verify_actions()). All three are "a channel we expected to
    exist wasn't there" - device-side, read-side and write-side."""


class HardwareBackend(ABC):
    """Interface the command/telemetry servers depend on instead of a concrete device implementation."""

    device: str = UNKNOWN_DEVICE
    """What this backend is driving. Stamped onto every telemetry frame it
    publishes, and used as the per-device directory name in stored output
    (see protocol/paths.py). Override with one of protocol/wire.py's
    DEVICE_* constants. Declared on the backend rather than passed in by an
    entry point, because it's a property of the device being driven, not of
    whoever started the process."""

    sample_interval_s: float = 0.02
    """How long stream_samples() sleeps between frames. Also what sizes the
    telemetry publisher's high-water mark, in seconds of buffer (see
    protocol/wire.py's hwm_for_interval). Note this is the *sleep*, not the
    achieved period: a real backend whose reads cost round-trips will run
    slower than 1/sample_interval_s."""

    _connected: bool = False
    """Whether connect() has succeeded. Backends whose connection state is a
    handle rather than a flag override is_connected instead of setting this."""

    @property
    def is_connected(self) -> bool:
        """Whether the device is currently connected. Override when
        connection state is something richer than the _connected flag."""
        return self._connected

    def _require_connected(self) -> None:
        """Raise unless connected - guards execute()/get_status() against
        being called before connect(). On the base class because every
        backend needs the same guard and the same error."""
        if not self.is_connected:
            raise HardwareError("backend not connected")

    @abstractmethod
    async def connect(self) -> None:
        """Establish the connection to the device.

        A real backend must also confirm here that every channel it declares
        exists on the device, raising MissingChannelError naming the ones that
        don't - see OdriveBackend._verify_declared_channels_exist. It belongs
        in the backend because only the process holding the device can tell a
        structurally absent channel from one that merely has no value yet.
        Mock backends generate frames from the same declaration they publish,
        so they cannot diverge and need no check."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear down the connection, leaving the device in a safe state.

        Must tolerate an already-unreachable or faulted device: this runs on
        the teardown path, where raising would mask the failure already
        propagating."""

    @abstractmethod
    async def get_status(self) -> dict:
        """Return current backend status - fields are device-specific."""

    @abstractmethod
    def stream_samples(self) -> AsyncIterator[dict]:
        """Yield one dict of {channel: value} per tick.

        Implementations should idle (not raise) rather than yield
        anything when there's nothing meaningful to report yet (e.g.
        acquisition not started, output disabled) - the telemetry
        server just forwards whatever this produces.
        """

    @abstractmethod
    async def execute(self, action: str, **params: Any) -> Any:
        """Perform a device-specific action (everything outside the
        universal core here). Raise HardwareError for an unknown
        action or invalid params."""

    @abstractmethod
    async def list_actions(self) -> List[str]:
        """Return every action name this backend accepts via execute()
        - a real, live answer from the running backend, not a
        hand-maintained list elsewhere. Lets a caller (e.g.
        CommandClient.verify_actions()) positively confirm the actions
        it depends on actually exist, the same way
        TelemetryClient.verify_channels() confirms a telemetry channel
        by reading a live frame rather than trusting a static list."""
