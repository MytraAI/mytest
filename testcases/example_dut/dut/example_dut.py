"""ExampleDut: abstraction over the example_dut DUT's own hardware
command and control channel.

This is separate from the instruments (DAQ, power supply) that
surround the DUT and that the testbed manages instead (see
testbeds/example_testbed/example_testbed.py). The testbed deliberately
doesn't touch the DUT's driver process or issue it any commands - this
class owns that, since talking to the DUT is a distinct concern from
setting up the instruments around it.

This is the one façade test authors call through - every command/
telemetry channel gets its own named get_<channel>()/set_<channel>()
method here (e.g. set_position(), get_position(), get_velocity()),
even where that's a thin forward to self.command/self._sync_telemetry.
Test-authored code should never need to reach into .command or
.telemetry directly to read or write one of these channels.

Command channels: position (via set_position() - the wire-level action
is "set_position_input", renamed here for symmetry with get_position()),
position_gain, velocity_gain, velocity_integrator.
Read channels: position, velocity, current.

get_channels() reads a single, consistent snapshot of every read
channel at once, via its own dedicated telemetry subscription - kept
separate from .telemetry so it never contends with whatever else is
already consuming that one (e.g. LiveRulebookRunner's background
thread), since a ZeroMQ socket isn't safe to read from two threads at
once. get_position()/get_velocity()/get_current() are thin per-channel
wrappers over it - use get_channels() directly instead when you need
more than one channel from the same instant.

start() positively confirms MockDutBackend's own declared
TELEMETRY_CHANNELS/COMMAND_CHANNELS (see
hardware/mock_dut/mock_channels.py - the source of truth; ../channels.py
just re-exports them for test-author reference) - the former against a
live telemetry frame, the latter against the backend's own live
list_actions() answer - raising MissingChannelError immediately if
either is missing, rather than a test discovering the gap later when
some step happens to use the missing channel.
"""
from __future__ import annotations

import subprocess
import sys
import time
from typing import Dict, Optional, Tuple

from hardware.mock_dut.mock_dut_command_client import DutCommandClient
from hardware.clients.telemetry_client import TelemetryClient
from hardware.mock_dut.mock_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS
from protocol.wire import DEFAULT_DUT_TELEMETRY_ENDPOINT, DEVICE_DUT

STARTUP_DELAY_S = 0.5


class ExampleDut:
    """Starts the DUT's own driver process and exposes its command/telemetry surface.

    Use as a context manager:

        with ExampleDut() as dut:
            dut.set_position(10.0)
            dut.set_gains(position_gain=2.0, velocity_gain=5.0, velocity_integrator=0.1)
            print(dut.get_position(), dut.get_velocity(), dut.get_current())
    """

    DEVICES: Tuple[str, ...] = (DEVICE_DUT,)
    """The device whose driver process this façade owns - the product under
    test itself. Declared here because this is what starts it; the test case
    unions this with its testbed's declaration. See testcases/base.py's
    DEVICES."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self.command: Optional[DutCommandClient] = None
        self.telemetry: Optional[TelemetryClient] = None
        # Separate socket for get_channels() telemetry
        # socket is owned by whatever's continuously reading it in another
        # thread (e.g. LiveRulebookRunner), and ZMQ sockets aren't
        # thread-safe to share.
        self._sync_telemetry: Optional[TelemetryClient] = None

    def start(self) -> None:
        self._process = subprocess.Popen([sys.executable, "-m", "hardware.mock_dut.main"])
        time.sleep(STARTUP_DELAY_S)  # let the driver bind its sockets

        self.command = DutCommandClient()
        self.telemetry = TelemetryClient(endpoint=DEFAULT_DUT_TELEMETRY_ENDPOINT)
        self._sync_telemetry = TelemetryClient(endpoint=DEFAULT_DUT_TELEMETRY_ENDPOINT)
        self.command.connect_backend()

        self.command.verify_actions(COMMAND_CHANNELS)
        self.telemetry.verify_channels(TELEMETRY_CHANNELS)

    def stop(self) -> None:
        if self.command is not None:
            self.command.disconnect_backend()
            self.command.close()
        if self.telemetry is not None:
            self.telemetry.close()
        if self._sync_telemetry is not None:
            self._sync_telemetry.close()
        if self._process is not None:
            self._process.terminate()
            self._process.wait(timeout=5)

    def __enter__(self) -> "ExampleDut":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def set_position(self, value: float) -> None:
        self.command.set_position_input(value)

    def set_position_gain(self, value: float) -> None:
        self.command.set_position_gain(value)

    def set_velocity_gain(self, value: float) -> None:
        self.command.set_velocity_gain(value)

    def set_velocity_integrator(self, value: float) -> None:
        self.command.set_velocity_integrator(value)

    def set_gains(self, position_gain: float, velocity_gain: float, velocity_integrator: float) -> None:
        self.set_position_gain(position_gain)
        self.set_velocity_gain(velocity_gain)
        self.set_velocity_integrator(velocity_integrator)

    def get_channels(self) -> Dict[str, float]:
        """Block until the next telemetry frame arrives and return its
        full channels dict (position, velocity, current) - one
        consistent snapshot from a single instant, unlike calling
        multiple per-channel getters separately (each would read
        whatever frame happens to arrive next, moments apart)."""
        frame = next(self._sync_telemetry.frames())
        return frame.channels

    def get_position(self) -> float:
        return self.get_channels()["position"]

    def get_velocity(self) -> float:
        return self.get_channels()["velocity"]

    def get_current(self) -> float:
        return self.get_channels()["current"]
