"""Physical testbed for ydrive: the ODrive driver process and a CPX400DP bench
supply, with connected command/telemetry clients for both. There is no separate
DUT layer - the ODrive is the entire actuator and sensor interface.

The supply feeds two rails: a 48 V motor bus on output 2 and a 24 V brake on
output 1 (see MOTOR_BUS/BRAKE_BUS below).

THE BRAKE IS MAGNET-APPLIED, AND SO FAIL-SAFE. A permanent magnet supplies the
holding force, so the brake is engaged whenever its rail is unpowered; powering
output 1 cancels that field and releases it. Losing the rail therefore holds the
load rather than dropping it. power_brake_bus() is the only place that polarity
is
asserted - and it moves the rail alone. Pairing the rail with the axis state, so
the motor never drives against an engaged brake and the brake never lets go of a
load the controller has not taken, is engage_brake()/release_brake() in
testcases/ydrive/teststeps/teststeps.py.

What this does NOT do: energize anything in start(). Rail setpoints are
configured with both outputs off, and powering the stand is a test's decision,
taken in PreTestSetup. It also does not configure the ODrive, including
`board_config_dc_bus_overvoltage_trip_level`, which is persistent device state.

use_mock substitutes the ODrive only. The supply's driver has no mock backend, so
a reachable CPX400DP is always needed.

Test steps use testbed.command/testbed.telemetry directly, and the named
per-channel methods (get_pos_estimate(), ...) for a synchronous point-read -
those use a separate sync_telemetry client so they do not contend with whatever
else is consuming .telemetry, such as LiveRulebookRunner. Every client accessor
raises RuntimeError before start().
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from hardware.clients.telemetry_client import TelemetryClient
from hardware.cpx400dp.cpx400dp_channels import (
    COMMAND_CHANNELS as CPX400DP_COMMAND_CHANNELS,
    TELEMETRY_CHANNELS as CPX400DP_TELEMETRY_CHANNELS,
)
from hardware.cpx400dp.cpx400dp_command_client import Cpx400dpCommandClient
from hardware.cpx400dp.rails import Rail, deliverable_current_a
from hardware.driver_process import start_driver
from hardware.odrive import odrive_errors
from hardware.odrive.odrive_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS
from hardware.odrive.odrive_command_client import OdriveCommandClient
from hardware.tc_daq.tc_daq_channels import TELEMETRY_CHANNELS as TC_DAQ_TELEMETRY_CHANNELS
from hardware.vision_home.vision_home_channels import (
    TELEMETRY_CHANNELS as VISION_HOME_TELEMETRY_CHANNELS,
)
from hardware.vision_home.vision_home_command_client import VisionHomeCommandClient
from hardware.tc_daq.transport import SILENCE_TIMEOUT_S as TC_DAQ_SILENCE_TIMEOUT_S
from protocol.paths import driver_console_path, driver_log_path
from testcases.utils import Stopwatch
from protocol.wire import (
    DEFAULT_CPX400DP_COMMAND_ENDPOINT,
    DEFAULT_CPX400DP_TELEMETRY_ENDPOINT,
    DEFAULT_ODRIVE_COMMAND_ENDPOINT,
    DEFAULT_ODRIVE_TELEMETRY_ENDPOINT,
    DEFAULT_TC_DAQ_TELEMETRY_ENDPOINT,
    DEFAULT_VISION_HOME_COMMAND_ENDPOINT,
    DEFAULT_VISION_HOME_TELEMETRY_ENDPOINT,
    DEVICE_CPX400DP,
    DEVICE_ODRIVE,
    DEVICE_TC_DAQ,
    DEVICE_VISION_HOME,
)

logger = logging.getLogger(__name__)


class MarkerAlignment(NamedTuple):
    """Whether the camera sees the taught view of the fixture, and what it is
    measured against - from one telemetry frame."""

    aligned: bool
    score: float
    taught: bool


class Motion(NamedTuple):
    """Position, velocity and whether the axis is still driving, from one telemetry frame.
    One read, because every question about a moving axis is about all three at once."""

    position: float
    velocity: float
    armed: bool


CPX400DP_HOST = "169.254.166.16"
"""This stand's supply, serial 595016.

Not a stable address: the instrument reports DHCP, but its segment has no DHCP
server, so it self-assigns a link-local address that changes if a DHCP server
appears or on an address collision. Nothing announces the move, and `connect()`
checks the model rather than the serial, so a moved address is found with
`python -m tools.find_cpx400dp`, which reports the identity of whatever answers.
Pass a new one as YdriveTestbed(cpx400dp_host=...)."""

CPX400DP_MDNS_HOST = "t595016.local"
"""The same supply by mDNS name: it advertises itself as `t<serial>.local`, which
follows it when its address changes. Needs an mDNS responder on the host - macOS
has one built in, a Windows or CentOS stand may not. Pass either this or
CPX400DP_HOST as YdriveTestbed(cpx400dp_host=...)."""

TC_DAQ_STALENESS_S = TC_DAQ_SILENCE_TIMEOUT_S + 2.0
"""How long this stand's consumers wait for a thermocouple frame before treating
the stream as dead.

Derived from the driver's own tolerance rather than set alongside it, and
deliberately longer. Both ends are watching the same silence: at the same value
they race, and the client wins by reporting only that no frame arrived - while
the driver reports which port went quiet and for how long. Two seconds of margin
means the useful diagnosis is the one that lands. TelemetryClient's 5 s default
would fire first and make the driver's extra patience unreachable."""

STARTUP_DELAY_S = 1.0
"""Seconds allowed for both drivers to bind their sockets and connect. The
supply's connect() checks identity, clears its error registers, probes all 27
declared queries and reads its cached tier before it serves."""

MOTOR_BUS = Rail(name="ydrive motor bus", output=2, voltage_v=48.0, current_limit_a=16.0)
"""The ODrive's DC bus, on output 2.

The 16 A limit is above what the supply can deliver at 48 V - its 420 W envelope
caps this output at 8.75 A - so it does not act as a current limit. An overdraw
makes the output go unregulated and the bus voltage sag; `in_power_limit_2` is
the channel that reports it, not `current_2`. For the same reason
a sustained draw cannot reach `ydrive_rulebook`'s fatal `board_ibus` bound on
this rail - see MAX_BUS_CURRENT_A, which says so itself."""

BRAKE_BUS = Rail(name="ydrive brake", output=1, voltage_v=24.0, current_limit_a=5.0)
"""The ydrive brake, on output 1.

Magnet-applied and fail-safe: the brake is engaged with this rail unpowered, and
powering it RELEASES the brake. 120 W is inside the envelope, so this rail does
get real current
limiting."""

METERS_PER_TURN = 0.084
"""How far the load travels per motor turn.

Stand geometry, so it lives with the stand rather than in whichever test needed
it first: every test that reports a distance or a speed in the units an operator
thinks in converts through this. 2 m of travel is 23.8 turns; 1.8 m/s is 21.43
turns/s."""

RAILS = (BRAKE_BUS, MOTOR_BUS)
"""Both rails, ordered by output number. start() iterates this to configure
setpoints. It is not the teardown order - see stop()."""

TEARDOWN_SETTLE_S = 3.0
"""How long stop() waits for a moving load to come to rest under the controller
before dropping the brake rail on it.

Short on purpose. This is time spent after a run has already ended, and the
fallback - the brake - is where the load ends up anyway. Three seconds covers a
stop from this stand's full speed with room over."""

TEARDOWN_VELOCITY_TOLERANCE = 0.05  # turns/s
"""What counts as at rest for the teardown settle. Small enough to mean stopped
rather than slowed."""

VISION_COMMAND_TIMEOUT_MS = 120_000
"""How long a vision-home command may take, against CommandClient's 5 s default.

select_best_camera opens every camera on the machine and reads frames from each,
and none of that is fast: an absent index on Windows costs an MSMF attempt, a
settle, a DSHOW attempt and a last-resort probe, and a present one costs a release
settle on top. Six indices runs to tens of seconds.

Generous rather than tuned, because the cost of overrunning is not a slow report -
a timed-out REQ socket is left permanently broken (see CommandClient), so the
testbed's client has to be rebuilt and the run is over."""

CAMERA_SOURCE = "0"
"""Which camera the vision-home driver opens on this bench.

A device index, or an address. NOT A STABLE NUMBER: index numbering is
per-machine and per-OS, and a built-in camera or a docking station can move a
USB webcam to an unpredictable one - `python -m hardware.vision_home.main --scan`
prints what this machine answers on. Pass a new one as
YdriveTestbed(camera_source=...)."""

BRAKE_SETTLE_S = 0.1
"""Seconds to wait after switching the brake rail, before moving or dwelling.

Chosen for this stand rather than taken from the brake's datasheet, which is
still the number that should replace it. A brake is not instantaneous: the coil
field has to collapse before a magnet-applied brake grabs, and build before it
lets go, and an output's terminal voltage decays through its capacitance rather
than dropping.

The risk is asymmetric. Too short before a dwell means dwelling briefly
unbraked. Too short before a move means driving the axis into a brake that has
not let go.

It also sets how far a moving load coasts before the brake bites, which is part
of any stopping distance measured from the brake command: at 1.8 m/s this wait
alone is up to 0.18 m."""


def settle_load_under_controller(testbed, settle_s: float = TEARDOWN_SETTLE_S) -> None:
    """Bring a still-moving load to rest under the controller, so stop() is not asked to stop
    it with the brake. An attempt only: bounded by `settle_s`, then stop() carries on."""
    motion = testbed.get_motion()
    if abs(motion.velocity) <= TEARDOWN_VELOCITY_TOLERANCE:
        logger.info("the load is already at rest at %.2f turns - nothing to stop", motion.position)
        return

    logger.warning(
        "the stand is shutting down with the load moving %.2f turns/s at %.2f turns - stopping "
        "it under the controller so the brake is not asked to",
        motion.velocity, motion.position,
    )
    # Park the setpoint where the axis is rather than where it was going, so the
    # controller decelerates in place instead of finishing the stroke.
    testbed.command.set_position(motion.position)

    deadline = Stopwatch(duration_s=settle_s)
    while not deadline.expired:
        motion = testbed.get_motion()
        if abs(motion.velocity) <= TEARDOWN_VELOCITY_TOLERANCE:
            logger.info("the load is at rest at %.2f turns; the stand can be shut down", motion.position)
            return
        if not motion.armed:
            # Nothing is driving it, so there is nothing to wait for: the axis
            # disarmed, or was never armed, and the load is coasting. The brake is
            # the only thing left that can stop it, and stop() engages it next.
            logger.error(
                "the axis is not driving, so the load is coasting at %.2f turns/s - handing it "
                "to the brake now rather than waiting",
                motion.velocity,
            )
            return
    logger.error(
        "the load was still moving %.2f turns/s after %.0fs - handing it to the brake at %.2f turns",
        motion.velocity, settle_s, motion.position,
    )


class YdriveTestbed:
    """Starts/stops the ODrive, CPX400DP and thermocouple DAQ drivers for ydrive, and owns
    connected clients for them. Use as a context manager."""

    DEVICES: Tuple[str, ...] = (DEVICE_ODRIVE, DEVICE_CPX400DP, DEVICE_TC_DAQ, DEVICE_VISION_HOME)
    """The devices whose driver processes this testbed owns. Declared here
    because this is what starts them; the test case unions this with its DUT
    façade's declaration (there is none for ydrive) and publishes the result, so
    the telemetry engine knows whose frames belong to the run. See
    testcases/base.py's DEVICES."""

    def __init__(
        self,
        use_mock: bool = False,
        serial_number: Optional[str] = None,
        cpx400dp_host: str = CPX400DP_HOST,
        tc_daq_port: Optional[str] = None,
        camera_source: str = CAMERA_SOURCE,
        output_dir: Optional[Path] = None,
        test_id: Optional[str] = None,
    ) -> None:
        """cpx400dp_host, tc_daq_port and camera_source are bench configuration, found by address,
        USB vendor and index; output_dir/test_id put each driver's log beside its telemetry."""
        self._use_mock = use_mock
        self._camera_source = camera_source
        self._serial_number = serial_number
        self._cpx400dp_host = cpx400dp_host
        self._tc_daq_port = tc_daq_port
        self._output_dir = output_dir
        self._test_id = test_id
        self._processes: List[subprocess.Popen] = []
        self._device_for_process: List[str] = []
        """Which device each process in _processes drives, so a driver that exits
        can be named rather than counted."""
        self._command: Optional[OdriveCommandClient] = None
        self._telemetry: Optional[TelemetryClient] = None
        self._sync_telemetry: Optional[TelemetryClient] = None
        self._supply: Optional[Cpx400dpCommandClient] = None
        self._supply_telemetry: Optional[TelemetryClient] = None
        self._tc_daq_telemetry: Optional[TelemetryClient] = None
        self._vision: Optional[VisionHomeCommandClient] = None
        self._vision_telemetry: Optional[TelemetryClient] = None

    def _log_args(self, device: str) -> List[str]:
        """`--log-file` for one device's driver, or nothing if this testbed
        wasn't told which run it belongs to."""
        if self._output_dir is None or self._test_id is None:
            return []
        return ["--log-file", str(driver_log_path(self._output_dir, self._test_id, device))]

    def _console_path(self, device: str):
        """Where to capture this device's raw stdout/stderr, or None if this testbed
        wasn't told which run it belongs to - see start_driver()."""
        if self._output_dir is None or self._test_id is None:
            return None
        return driver_console_path(self._output_dir, self._test_id, device)

    def start(self) -> None:
        """Bring all three drivers up, verify their channel surfaces, and configure both rails'
        setpoints - with the outputs left OFF. Energizing is a test's decision, not a testbed's."""
        odrive_args = [sys.executable, "-m", "hardware.odrive.main", *self._log_args(DEVICE_ODRIVE)]
        if self._use_mock:
            odrive_args.append("--mock")
        elif self._serial_number is not None:
            odrive_args += ["--serial-number", self._serial_number]

        # The driver's ceiling is per-backend rather than per-output, so the only
        # values it can carry are the maxima across both rails. It is a coarse
        # backstop against a gross typo, not per-rail protection: to the driver,
        # 48 V on the 24 V brake rail is a legal command. check_rails() detects
        # that after the fact.
        supply_args = [
            sys.executable, "-m", "hardware.cpx400dp.main",
            "--host", self._cpx400dp_host,
            "--max-voltage", str(max(rail.voltage_v for rail in RAILS)),
            "--max-current", str(max(rail.current_limit_a for rail in RAILS)),
            *self._log_args(DEVICE_CPX400DP),
        ]

        # The thermocouple DAQ takes no commands at all, so it gets no command
        # client below - nothing would be sendable through one. Its driver is
        # started, its stream is verified, and that is the whole interface.
        tc_daq_args = [
            sys.executable, "-m", "hardware.tc_daq.main",
            *(["--port", self._tc_daq_port] if self._tc_daq_port else []),
            *self._log_args(DEVICE_TC_DAQ),
        ]

        # The camera that re-references the axis against the world. Pointed at a
        # device index or an address, because neither is predictable across
        # benches or operating systems - see hardware/vision_home/camera.py.
        vision_args = [
            sys.executable, "-m", "hardware.vision_home.main",
            "--camera-source", self._camera_source,
            *self._log_args(DEVICE_VISION_HOME),
        ]

        self._processes = [
            start_driver(odrive_args, self._console_path(DEVICE_ODRIVE)),
            start_driver(supply_args, self._console_path(DEVICE_CPX400DP)),
            start_driver(tc_daq_args, self._console_path(DEVICE_TC_DAQ)),
            start_driver(vision_args, self._console_path(DEVICE_VISION_HOME)),
        ]
        self._device_for_process = [
            DEVICE_ODRIVE, DEVICE_CPX400DP, DEVICE_TC_DAQ, DEVICE_VISION_HOME
        ]
        time.sleep(STARTUP_DELAY_S)  # let the drivers bind their sockets

        self._command = OdriveCommandClient(endpoint=DEFAULT_ODRIVE_COMMAND_ENDPOINT)
        self._telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._sync_telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._supply = Cpx400dpCommandClient(endpoint=DEFAULT_CPX400DP_COMMAND_ENDPOINT)
        self._supply_telemetry = TelemetryClient(endpoint=DEFAULT_CPX400DP_TELEMETRY_ENDPOINT)
        self._tc_daq_telemetry = TelemetryClient(
            endpoint=DEFAULT_TC_DAQ_TELEMETRY_ENDPOINT, timeout_s=TC_DAQ_STALENESS_S
        )
        self._vision = VisionHomeCommandClient(
            endpoint=DEFAULT_VISION_HOME_COMMAND_ENDPOINT,
            timeout_ms=VISION_COMMAND_TIMEOUT_MS,
        )
        self._vision_telemetry = TelemetryClient(endpoint=DEFAULT_VISION_HOME_TELEMETRY_ENDPOINT)

        # Before waiting ten seconds on a command server: a driver that has
        # already exited will never answer, and its own log says why.
        self._require_drivers_alive()
        self._command.connect_backend()
        self._supply.connect_backend()

        self._command.verify_actions(COMMAND_CHANNELS)
        self._telemetry.verify_channels(TELEMETRY_CHANNELS)
        self._supply.verify_actions(CPX400DP_COMMAND_CHANNELS)
        self._supply_telemetry.verify_channels(CPX400DP_TELEMETRY_CHANNELS)
        # No verify_actions for the DAQ: it declares no commands, so there is
        # nothing to confirm. Its stream is the only thing to check, and a
        # faulted thermocouple still publishes its channel (as None), so this
        # passes with sensors unplugged - what it catches is a driver that
        # started against the wrong port and is streaming something else.
        self._tc_daq_telemetry.verify_channels(TC_DAQ_TELEMETRY_CHANNELS)
        self._vision_telemetry.verify_channels(VISION_HOME_TELEMETRY_CHANNELS)

        self._configure_rails()

    def _require_drivers_alive(self) -> None:
        """Raise if any driver process has already exited, naming the device, its exit code and its
        log path - otherwise it surfaces as a bare CommandTimeout ten seconds later."""
        dead = [
            (args, process)
            for args, process in zip(self._device_for_process, self._processes)
            if process.poll() is not None
        ]
        if not dead:
            return
        detail = "; ".join(
            f"{device} driver exited with code {process.returncode}"
            + (f" - see {driver_log_path(self._output_dir, self._test_id, device)}"
               if self._output_dir is not None and self._test_id is not None else "")
            for device, process in dead
        )
        raise RuntimeError(f"a hardware driver did not stay up: {detail}")

    def _configure_rails(self) -> None:
        """Switch both outputs off, then set every rail's voltage and current setpoints. Off first:
        the driver adopts the state it starts into, and a setpoint would step a live rail."""
        for rail in RAILS:
            self.supply.enable_output(rail.output, False)
        logger.info("both outputs off - configuring setpoints on de-energized rails")

        for rail in RAILS:
            if not rail.is_within_envelope:
                logger.warning(
                    "%s: the configured %.1f A limit is above what this supply can deliver at "
                    "%.1f V (%.2f A, from the 420 W envelope), so it will NOT act as a current "
                    "limit. If the load draws more, the output goes unregulated and the rail "
                    "voltage sags instead - watch in_power_limit_%d, not current_%d.",
                    rail.name, rail.current_limit_a, rail.voltage_v,
                    deliverable_current_a(rail.voltage_v), rail.output, rail.output,
                )
            self.supply.set_voltage(rail.output, rail.voltage_v)
            self.supply.set_current(rail.output, rail.current_limit_a)
            logger.info(
                "%s configured on output %d: %.1f V, %.1f A limit (%.0f W)",
                rail.name, rail.output, rail.voltage_v, rail.current_limit_a, rail.power_w,
            )
        self.check_rails()

    SETPOINT_TOLERANCE = 0.02
    """How far a read-back setpoint may sit from its configured value before
    check_rails() calls it wrong. The instrument reports voltage setpoints to
    10 mV and current to 1 mA, so this is a rounding allowance, not a band."""

    SETPOINT_SETTLE_ATTEMPTS = 6
    SETPOINT_SETTLE_DELAY_S = 0.2
    """How many times, and how far apart, check_rails() re-reads before calling a
    setpoint wrong.

    A telemetry frame can be older than the write it is being asked about. This
    driver holds setpoints in a cached tier, re-read at connect and after a write
    rather than every frame, and latest_frame() answers with the newest frame
    already queued rather than waiting for one published after the write. So the
    first read can legitimately still carry the previous run's values - which is
    invisible whenever those happen to match, and a spurious failure whenever
    they do not."""

    def check_rails(self) -> None:
        """Confirm both rails still hold their configured setpoints, raising if not - the supply
        accepts and then silently discards a value it dislikes. See SETPOINT_SETTLE_ATTEMPTS."""
        for attempt in range(self.SETPOINT_SETTLE_ATTEMPTS):
            wrong = self._setpoint_disagreements()
            if not wrong:
                return
            if attempt + 1 < self.SETPOINT_SETTLE_ATTEMPTS:
                time.sleep(self.SETPOINT_SETTLE_DELAY_S)
        raise RuntimeError(
            "the supply's rail setpoints do not match this stand's configuration:\n  "
            + "\n  ".join(wrong)
            + "\nSomething commanded a setpoint outside this testbed, or a write was refused. "
            "See MOTOR_BUS/BRAKE_BUS in this module for what the rails should be."
        )

    def _setpoint_disagreements(self) -> List[str]:
        """Every configured rail setpoint the supply is not currently holding,
        from one telemetry frame."""
        channels = self._supply_channels()
        wrong = []
        for rail in RAILS:
            for quantity, expected, channel in (
                ("voltage", rail.voltage_v, f"setpoint_voltage_{rail.output}"),
                ("current limit", rail.current_limit_a, f"setpoint_current_{rail.output}"),
            ):
                actual = channels[channel]
                if abs(float(actual) - expected) > self.SETPOINT_TOLERANCE:
                    wrong.append(f"{rail.name} {quantity}: expected {expected}, instrument holds {actual}")
        return wrong

    def stop(self) -> None:
        """Settle a moving load, drop the brake rail so the brake grabs, disarm, then drop the bus.
        Each step logs rather than raises, so one wedged client cannot leave 48 V energized."""
        self._safe("bring a moving load to rest", lambda: settle_load_under_controller(self))
        # The brake settle below is a plain sleep rather than TestCase.wait_for():
        # teardown has no test case to poll, and nothing it could usefully abort for.
        self._safe("engage the brake (drop the 24 V rail)", self._engage_brake_for_teardown)
        self._safe("disarm the ODrive axis", lambda: self.command.set_axis_state("IDLE"))
        self._safe("drop the 48 V motor bus", lambda: self.power_motor_bus(False))
        self._safe("confirm both rails are off", self._confirm_rails_off)

        self._safe("disconnect the ODrive backend", lambda: self.command.disconnect_backend())
        self._safe("disconnect the supply backend", lambda: self.supply.disconnect_backend())

        for client in (self._command, self._telemetry, self._sync_telemetry,
                       self._supply, self._supply_telemetry, self._tc_daq_telemetry,
                       self._vision, self._vision_telemetry):
            if client is not None:
                self._safe(f"close {type(client).__name__}", client.close)
        self._command = self._telemetry = self._sync_telemetry = None
        self._supply = self._supply_telemetry = self._tc_daq_telemetry = None
        self._vision = self._vision_telemetry = None

        for process in self._processes:
            self._safe(f"terminate pid {process.pid}", process.terminate)
        for process in self._processes:
            self._safe(f"reap pid {process.pid}", lambda p=process: p.wait(timeout=5))
        self._processes = []

    def _confirm_rails_off(self) -> None:
        """Read both outputs back after switching them off, and say so at ERROR if either is still
        on - the one step of stop() that reads the outcome rather than the command."""
        self.supply_telemetry.discard_backlog()
        channels = self._supply_channels()
        still_on = [rail.name for rail in RAILS if channels.get(f"output_enabled_{rail.output}")]
        if still_on:
            logger.error(
                "TEARDOWN LEFT THE STAND ENERGIZED: %s still on - switch the supply off by hand",
                ", ".join(still_on),
            )

    def _engage_brake_for_teardown(self) -> None:
        """Drop the brake rail and give the brake time to grab, before the axis is
        disarmed and the bus removed."""
        self.power_brake_bus(False)
        time.sleep(BRAKE_SETTLE_S)

    @staticmethod
    def _safe(what: str, action: Callable[[], object]) -> None:
        """Run a teardown step, logging rather than raising on failure."""
        try:
            action()
        except Exception as exc:  # teardown must continue regardless
            logger.error("teardown step failed, continuing: %s: %r", what, exc)

    # --- power ------------------------------------------------------------

    def power_motor_bus(self, enabled: bool) -> None:
        """Switch the 48 V motor bus (output 2) on or off. The output ramps rather than stepping,
        so a check immediately after enabling reads low."""
        self.supply.enable_output(MOTOR_BUS.output, enabled)
        logger.info("%s %s", MOTOR_BUS.name, "energized" if enabled else "de-energized")

    def power_brake_bus(self, enabled: bool) -> None:
        """Switch the 24 V brake rail (output 1) on or off - powering RELEASES the brake. Moves the
        rail alone; engage_brake()/release_brake() are the sequenced versions a test should use."""
        self.supply.enable_output(BRAKE_BUS.output, enabled)
        logger.info("%s %s", BRAKE_BUS.name, "released (rail energized)" if enabled else "engaged (rail de-energized)")

    def get_marker_alignment(self) -> "MarkerAlignment":
        """Whether the camera sees the taught view, from one frame - one read, so the alignment
        and what it is measured against describe the same instant."""
        channels = self.vision_telemetry.latest_frame().channels
        return MarkerAlignment(
            aligned=bool(channels["aligned"]),
            score=float(channels["match_score"]),
            taught=bool(channels["taught"]),
        )

    def _supply_channels(self) -> Dict[str, object]:
        """Block for the next supply telemetry frame and return its channels. Private: callers ask a
        named question instead, so this stand's channel names live in one place."""
        return self.supply_telemetry.latest_frame().channels


    @property
    def supply(self) -> Cpx400dpCommandClient:
        if self._supply is None:
            raise RuntimeError("YdriveTestbed.supply accessed before start()")
        return self._supply

    @property
    def supply_telemetry(self) -> TelemetryClient:
        if self._supply_telemetry is None:
            raise RuntimeError("YdriveTestbed.supply_telemetry accessed before start()")
        return self._supply_telemetry

    @property
    def vision(self) -> VisionHomeCommandClient:
        if self._vision is None:
            raise RuntimeError("testbed not started")
        return self._vision

    @property
    def vision_telemetry(self) -> TelemetryClient:
        if self._vision_telemetry is None:
            raise RuntimeError("testbed not started")
        return self._vision_telemetry

    @property
    def tc_daq_telemetry(self) -> TelemetryClient:
        """The thermocouple DAQ's stream, and the only interface it has - it accepts no commands, so
        there is no command client to pair with it."""
        if self._tc_daq_telemetry is None:
            raise RuntimeError("YdriveTestbed.tc_daq_telemetry accessed before start()")
        return self._tc_daq_telemetry

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

    def _odrive_channels(self) -> Dict[str, object]:
        """The newest ODrive frame's channels, blocking if none has arrived. Private, and the only
        place a raw frame is handled - see Motion for a read that needs one instant."""
        return self.sync_telemetry.latest_frame().channels

    def get_motion(self) -> Motion:
        """Position and velocity, from one frame."""
        channels = self._odrive_channels()
        return Motion(
            position=channels["pos_estimate"],
            velocity=channels["vel_estimate"],
            armed=bool(channels["axis_is_armed"]),
        )

    def get_faults(self) -> Dict[str, str]:
        """Every watched ODrive channel currently reading as a fault, decoded -
        empty when the board is clean. One frame, so it describes one instant."""
        return odrive_errors.faults_in_frame(self._odrive_channels())

    def describe_errors(self) -> Dict[str, str]:
        """Every watched channel decoded, faulted or not, from one frame - the diagnostic for why an
        axis refused, where NOMINAL is as much of the answer as a fault."""
        channels = self._odrive_channels()
        return {
            name: odrive_errors.describe(name, channels[name])
            for name in odrive_errors.WATCHED_CHANNELS
            if name in channels
        }

    @staticmethod
    def turns_to_metres(turns: float) -> float:
        """Turns of the motor as metres of track. Reading only - see METERS_PER_TURN.

        Static and side-effect-free because the run's derived channels call it from the
        state publisher's thread, where touching a telemetry socket would be a race."""
        return float(turns) * METERS_PER_TURN

    def get_distance_travelled_m(self) -> float:
        """Metres of track the axis has covered since its driver connected.

        The driver counts the path frame by frame, so every overshoot and reversal is in
        it - see the odrive's turns_traveled. Cumulative from connect, not from a run, so
        a caller wanting one run's distance takes the difference."""
        return self.turns_to_metres(self._odrive_channels()["turns_traveled"])

    def get_pos_estimate(self) -> float:
        return self._odrive_channels()["pos_estimate"]

    def get_axis_armed_status(self) -> bool:
        """Whether the axis is actively controlling the motor. Requesting a state only writes
        requested_state and the ODrive can decline it; this is the reading that says it took."""
        return bool(self._odrive_channels()["axis_is_armed"])

    def get_vel_estimate(self) -> float:
        return self._odrive_channels()["vel_estimate"]

    def __enter__(self) -> "YdriveTestbed":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
