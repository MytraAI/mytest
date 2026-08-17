"""Physical testbed for ydrive: starts/stops the ODrive driver process
(hardware/odrive/) and owns ready-to-use command/telemetry clients -
there's no separate DUT layer here, since the ODrive motor controller
IS the entire hardware interface, both actuator and sensor.

Test steps use testbed.command/testbed.telemetry directly, and the
named per-channel methods below (get_pos_estimate(), ...) for a
synchronous point-read - these use a separate sync_telemetry client so
they don't contend with whatever else is consuming .telemetry (e.g.
LiveRulebookRunner, once started). command/telemetry/sync_telemetry
raise RuntimeError if accessed before start(). Pass use_mock=True to
run against MockOdriveBackend instead of real hardware.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hardware.clients.telemetry_client import TelemetryClient
from hardware.odrive.odrive_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS
from hardware.odrive.odrive_command_client import OdriveCommandClient
from protocol.paths import driver_log_path
from protocol.wire import (
    DEFAULT_ODRIVE_COMMAND_ENDPOINT,
    DEFAULT_ODRIVE_TELEMETRY_ENDPOINT,
    DEVICE_ODRIVE,
)

STARTUP_DELAY_S = 0.5


class YdriveTestbed:
    """Starts/stops the ODrive driver process for ydrive, and owns connected command/telemetry clients.

    Use as a context manager:

        with YdriveTestbed() as testbed:
            testbed.command.set_control_mode("VELOCITY_CONTROL")
            testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
            ...  # the driver is up, and the ODrive is controllable, for the duration of this block
    """

    DEVICES: Tuple[str, ...] = (DEVICE_ODRIVE,)
    """The devices whose driver processes this testbed owns. Declared here
    because this is what starts them; the test case unions this with its DUT
    façade's declaration (there is none for ydrive) and publishes the result, so
    the telemetry engine knows whose frames belong to the run. See
    testcases/base.py's DEVICES."""

    def __init__(
        self,
        use_mock: bool = False,
        serial_number: Optional[str] = None,
        output_dir: Optional[Path] = None,
        test_id: Optional[str] = None,
    ) -> None:
        """output_dir/test_id: given both, the driver writes its detailed log to
        `<output_dir>/runs/<test_id>/odrive/logs.txt`, beside the telemetry it
        produced - which is where a decoded ODrive fault goes (see
        hardware/odrive/odrive_errors.py). A test case passes `self._output_dir`
        and `self.test_id` from PreTestSetup. Omitted, the driver logs to its
        console, which for a subprocess is nowhere anybody reads."""
        self._use_mock = use_mock
        self._serial_number = serial_number
        self._output_dir = output_dir
        self._test_id = test_id
        self._process: Optional[subprocess.Popen] = None
        self._command: Optional[OdriveCommandClient] = None
        self._telemetry: Optional[TelemetryClient] = None
        self._sync_telemetry: Optional[TelemetryClient] = None

    def _log_args(self) -> List[str]:
        """`--log-file` for the ODrive driver, or nothing if this testbed wasn't
        told which run it belongs to."""
        if self._output_dir is None or self._test_id is None:
            return []
        return ["--log-file", str(driver_log_path(self._output_dir, self._test_id, DEVICE_ODRIVE))]

    def start(self) -> None:
        args = [sys.executable, "-m", "hardware.odrive.main", *self._log_args()]
        if self._use_mock:
            args.append("--mock")
        elif self._serial_number is not None:
            args += ["--serial-number", self._serial_number]
        self._process = subprocess.Popen(args)
        time.sleep(STARTUP_DELAY_S)  # let the driver bind its sockets

        self._command = OdriveCommandClient(endpoint=DEFAULT_ODRIVE_COMMAND_ENDPOINT)
        self._telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._sync_telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._command.connect_backend()

        self._command.verify_actions(COMMAND_CHANNELS)
        self._telemetry.verify_channels(TELEMETRY_CHANNELS)

    def stop(self) -> None:
        if self._command is not None:
            self._command.set_axis_state("IDLE")  # safe even if it was never armed
            self._command.disconnect_backend()
            self._command.close()
            self._command = None
        if self._telemetry is not None:
            self._telemetry.close()
            self._telemetry = None
        if self._sync_telemetry is not None:
            self._sync_telemetry.close()
            self._sync_telemetry = None
        if self._process is not None:
            self._process.terminate()
            self._process.wait(timeout=5)
            self._process = None

    @property
    def command(self) -> OdriveCommandClient:
        if self._command is None:
            raise RuntimeError("YdriveTestbed.command accessed before start()")
        return self._command

    @property
    def telemetry(self) -> TelemetryClient:
        if self._telemetry is None:
            raise RuntimeError("YdriveTestbed.telemetry accessed before start()")
        return self._telemetry

    @property
    def sync_telemetry(self) -> TelemetryClient:
        if self._sync_telemetry is None:
            raise RuntimeError("YdriveTestbed.sync_telemetry accessed before start()")
        return self._sync_telemetry

    def get_channels(self) -> Dict[str, object]:
        """Block until the next telemetry frame arrives and return its
        full channels dict. For test step code, prefer the named
        per-channel methods below - this is for callers that need more
        than one channel from the same instant."""
        frame = next(self.sync_telemetry.frames())
        return frame.channels

    def get_pos_estimate(self) -> float:
        return self.get_channels()["pos_estimate"]

    def get_vel_estimate(self) -> float:
        return self.get_channels()["vel_estimate"]

    def __enter__(self) -> "YdriveTestbed":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
