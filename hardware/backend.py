"""Abstract interface between the hardware driver's servers and whatever
actually talks to a piece of test-stand hardware - a DAQ, a power supply,
or any other device that needs a command/telemetry pair.

The command and telemetry servers depend only on this interface, never on
a concrete implementation. They only require the universal core below:
connect/disconnect/get_status/stream_samples.

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


class HardwareError(Exception):
    """Raised for any backend failure. The command server catches this
    and turns it into a CommandReply(ok=False, error=str(exc)) instead
    of letting it crash the process."""


class MissingChannelError(Exception):
    """Raised when an expected telemetry or command channel isn't
    actually present - either a live telemetry frame is missing an
    expected key (TelemetryClient.verify_channels()), or a backend's
    real list_actions() doesn't include an expected action
    (CommandClient.verify_actions()). Both are "a channel we expected
    to exist wasn't there," just on the read side vs. the write side."""


class HardwareBackend(ABC):
    """Interface the command/telemetry servers depend on instead of a concrete device implementation."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish the connection to the device."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear down the connection."""

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
