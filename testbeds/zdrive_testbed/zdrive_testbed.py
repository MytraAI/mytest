"""Physical testbed for zdrive: the ODrive driver process, a Keysight N6974A
feeding the motor bus, and a CPX400DP feeding the brake - with connected
command/telemetry clients for all three.

    with ZdriveTestbed() as testbed:
        testbed.power_motor_bus(True)
        testbed.command.set_axis_state("CLOSED_LOOP_CONTROL")
        testbed.power_brake_bus(True)   # only after the controller is holding

THE MOTOR BUS IS TWO-QUADRANT. The N6974A both sources and sinks, so
regenerated energy from decelerating the load flows back into the supply instead
of pushing the bus up. That is what MOTOR_BUS and ODRIVE_MAX_REGEN_CURRENT_A are
for, and they are a matched pair: the supply is programmed to absorb more than
the ODrive is permitted to return, so the ODrive's cap is the number that decides
regen and the bus stays at its setpoint. If regen ever exceeds what the supply
takes, the bus rises and the external clamp on this stand dissipates the
overflow - autonomously, on its own threshold, reporting nothing to any channel
here.

THE BRAKE IS MAGNET-APPLIED, AND SO FAIL-SAFE. A permanent magnet supplies the
holding force, so the brake is engaged whenever its rail is unpowered; powering
CPX400DP output 1 cancels that field and releases it. Losing the rail therefore
holds the load rather than dropping it. power_brake_bus() is the only place that
polarity is asserted, and it moves the rail alone - pairing it with the axis
state belongs in a test step.

CPX400DP OUTPUT 2 IS UNUSED AND UNCONNECTED, and start() switches it off
anyway: it is wired to nothing, and an output a previous run left live is still
worth forcing down.

What this does NOT do: energize anything in start(). The bus and rail setpoints
are configured with every output off, and powering the stand is a test's
decision, taken in PreTestSetup.
"""
from __future__ import annotations

import logging
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from hardware.clients.telemetry_client import TelemetryClient
from hardware.cpx400dp.cpx400dp_channels import (
    COMMAND_CHANNELS as CPX400DP_COMMAND_CHANNELS,
    OUTPUTS as CPX400DP_OUTPUTS,
    TELEMETRY_CHANNELS as CPX400DP_TELEMETRY_CHANNELS,
)
from hardware.cpx400dp.cpx400dp_command_client import Cpx400dpCommandClient
from hardware.cpx400dp.rails import Rail, deliverable_current_a
from hardware.driver_process import start_driver
from hardware.n6974a.n6974a_channels import (
    COMMAND_CHANNELS as N6974A_COMMAND_CHANNELS,
    TELEMETRY_CHANNELS as N6974A_TELEMETRY_CHANNELS,
)
from hardware.n6974a.n6974a_command_client import N6974aCommandClient
from hardware.odrive import odrive_errors
from hardware.odrive.odrive_channels import (
    COMMAND_CHANNELS as ODRIVE_COMMAND_CHANNELS,
    TELEMETRY_CHANNELS as ODRIVE_TELEMETRY_CHANNELS,
)
from hardware.odrive.odrive_command_client import OdriveCommandClient
from hardware.tc_daq.tc_daq_channels import TELEMETRY_CHANNELS as TC_DAQ_TELEMETRY_CHANNELS
from hardware.tc_daq.transport import SILENCE_TIMEOUT_S as TC_DAQ_SILENCE_TIMEOUT_S
from protocol.paths import driver_console_path, driver_log_path
from protocol.wire import (
    DEFAULT_CPX400DP_COMMAND_ENDPOINT,
    DEFAULT_CPX400DP_TELEMETRY_ENDPOINT,
    DEFAULT_N6974A_COMMAND_ENDPOINT,
    DEFAULT_N6974A_TELEMETRY_ENDPOINT,
    DEFAULT_ODRIVE_COMMAND_ENDPOINT,
    DEFAULT_ODRIVE_TELEMETRY_ENDPOINT,
    DEFAULT_TC_DAQ_TELEMETRY_ENDPOINT,
    DEVICE_CPX400DP,
    DEVICE_N6974A,
    DEVICE_ODRIVE,
    DEVICE_TC_DAQ,
)

logger = logging.getLogger(__name__)


class Motion(NamedTuple):
    """Where the axis is, how fast it is going, and whether it is still driving -
    from one telemetry frame.

    One read rather than three because every question worth asking about a moving
    axis is about all of them at once, and separate reads would answer from
    different frames a sample period apart.

    `armed` is in here because a loop watching a move has to notice the axis
    stopping driving. The ODrive disarms itself on a fault, so a move can end with
    the load coasting and no exception anywhere - which on a gravity-loaded axis
    means the load descending."""

    position: float
    velocity: float
    armed: bool


CPX400DP_HOST = "169.254.101.202"
"""This stand's brake supply, serial 603720.

Not a stable address: the instrument reports DHCP, but its segment has no DHCP
server, so it self-assigns a link-local address that changes if a DHCP server
appears or on an address collision. `t603720.local` is the same instrument by
mDNS name and follows it when the address moves, at the cost of needing an mDNS
responder on the host. Pass either as ZdriveTestbed(cpx400dp_host=...)."""

CPX400DP_MDNS_HOST = "t603720.local"
"""The same supply by mDNS name: it advertises itself as `t<serial>.local`, which
follows it when its address changes. Needs an mDNS responder on the host - macOS
has one built in, a Windows or CentOS stand may not. Pass either this or
CPX400DP_HOST as ZdriveTestbed(cpx400dp_host=...)."""

TC_DAQ_STALENESS_S = TC_DAQ_SILENCE_TIMEOUT_S + 2.0
"""How long a TC DAQ frame may be old before its client calls the stream dead.

Above the transport's own silence timeout, so the driver gets to report a dead
serial link itself rather than this client timing out first on the same
condition."""

STARTUP_DELAY_S = 2.0
"""Seconds allowed for all three drivers to bind their sockets before this
testbed connects their backends. Only socket binding happens here - each
backend's own connect() runs when connect_backend() is called below, and carries
its own timeout."""

N6974A_HOST = "169.254.160.111"
"""This stand's motor-bus supply, serial MY63000121.

Not a stable address, and observed to move within a single session: the
instrument self-assigns a link-local address, so it takes a new one whenever it
is power-cycled - which on this stand is routine, because the N7909A dissipator
is only discovered at power-on. `A-N6974A-00121.local` is the same instrument by
mDNS name and follows it. Pass either as ZdriveTestbed(n6974a_host=...)."""

N6974A_MDNS_HOST = "A-N6974A-00121.local"
"""The same supply by mDNS name: it advertises itself as `A-<model>-<serial>
.local`, which follows it when its address changes. Needs an mDNS responder on
the host - macOS has one built in, a Windows or CentOS stand may not."""

N6974A_DISSIPATORS = 1
"""How many Keysight N7909A power dissipator units are cabled to the supply.

One on a 2 kW model buys 50% of its rated current in the sinking direction:
-12.75 A. The driver verifies this against the instrument at connect and refuses
to start on a mismatch, because a dissipator that was cabled to an already-running
supply reads as absent and does nothing."""


@dataclass(frozen=True)
class MotorBus:
    """The N6974A's single output, and what this stand asks of it.

    Deliberately not a cpx400dp Rail: that type carries the CPX's 420 W envelope
    arithmetic, which says nothing true about this instrument, and it has an
    output number where the N6974A has one output."""

    name: str
    voltage_v: float
    current_limit_a: float
    sink_current_limit_a: float
    protection_mode: str
    priority_mode: str

    @property
    def sink_power_w(self) -> float:
        return abs(self.sink_current_limit_a) * self.voltage_v

    @property
    def source_power_w(self) -> float:
        return self.current_limit_a * self.voltage_v


MOTOR_BUS = MotorBus(
    name="zdrive motor bus",
    voltage_v=48.0,
    current_limit_a=25.0,
    sink_current_limit_a=-12.75,
    protection_mode="LOWZ",
    priority_mode="VOLT",
)
"""The ODrive's DC bus, on the N6974A's output.

WHICH OF THESE BINDS DEPENDS ON priority_mode, which is why it is declared here
rather than assumed. In voltage priority - what a bus supply runs in -
`voltage_v` is the regulated value, `current_limit_a` is its positive ceiling and
`sink_current_limit_a` its negative one.

THERE IS NO VOLTAGE CEILING HERE, because the instrument will not accept one in
this mode: `VOLTage:LIMit` is the ceiling for CURRENT priority, and setting it in
voltage priority is refused with `+315,"Settings conflict error; chan 1 must be
in current priority mode"`. What holds the bus to 48 V is the setpoint itself,
check_rails() confirming it, and zdrive_rulebook's bus_overvoltage_bound at
52 V.

25 A is this model's rated output, so the positive limit is set wide on purpose:
current limiting for this stand is the ODrive's soft/hard phase limits, and a
supply limit below the rating would make zdrive_rulebook's bus_current_bound
unfireable - the same trap ydrive's overcurrent_bound fell into.

-12.75 A is the whole sinking capability one N7909A gives a 2 kW model, and it is
programmed rather than inherited: recognising a dissipator raises what the
instrument will *allow* in the negative direction without moving the active
setpoint, so a supply left alone sinks 10% of its rating while a stand believes
it has 50%. 612 W of sink is inside the single dissipator's 1 kW.

LOWZ makes a protection shutdown actively pull the bus down instead of leaving
the ODrive's bus capacitance charged. It is the instrument's reset default, but
the driver adopts rather than resets state it is started into, so the stand
writes it."""

ODRIVE_MAX_REGEN_CURRENT_A = 10.0
"""How much current the ODrive is permitted to return to the supply.

The binding half of the regen pair: it sits below the supply's -12.75 A so the
supply's willingness to absorb is never the constraint, because when it is, the
bus rises and the external clamp fires on a threshold nothing here can see. This
is persistent ODrive state and the board ships it at 0.0, which returns no regen
at all and leaves a two-quadrant supply with nothing to do."""

ODRIVE_MOTOR_SOFT_MAX_A = 55.0
"""The motor phase current the controller is allowed to command.

Sized for a 1000 lb load. Measured on this stand, phase current runs
`0.0536 * lb - 1.6` amps, which puts 1000 lb at about 52 A. A demand above this
limit clamps rather than being delivered, so a load heavier than the stand is
sized for stalls instead of drawing whatever it takes."""

ODRIVE_MOTOR_HARD_MAX_A = 60.0
"""The measured motor phase current that trips CURRENT_LIMIT_VIOLATION in
firmware. zdrive_rulebook bounds `motor_foc_iq_measured` at this value, in both
directions.

15% above the 52 A a 1000 lb load is expected to draw, so the gap above the soft
limit is transient headroom: measured current may overshoot what the controller
commands without the firmware tripping, but not by more than that.

Both sit well inside the board's own inverter ceiling, which this hardware
reports as 100 A soft / 150 A hard - so what these express is what the stand asks
of the motor, not what the ODrive can deliver."""

BRAKE_BUS = Rail(name="zdrive brake", output=1, voltage_v=24.0, current_limit_a=5.0)
"""The zdrive brake, on CPX400DP output 1.

Magnet-applied and fail-safe: the brake is engaged with this rail unpowered, and
powering it RELEASES the brake. 120 W is inside the envelope, so this rail does
get real current
limiting."""

METERS_PER_TURN = 0.0096
"""How far the load travels per motor turn.

Stand geometry, so it lives with the stand rather than in whichever test needed
it first: every test that reports a distance or a speed in the units an operator
thinks in converts through this. The 0.528 m of stroke is 55 turns; 0.48 m is the
50 turns a cycling hold lifts to.

Metres, matching ydrive. This was millimetres, and the argument for that was real:
this axis is short and a brake slip lands around a micron, which in metres is a
column of leading zeros. What outweighed it is that one stand had ended up with
both a distance in mm and an accumulating total that only reads sensibly in m -
a cycling run covers kilometres - and a stand carrying two length units is a
stand where somebody eventually reads the wrong column, in a FATAL bound.

THE CONTROL PATH STAYS IN TURNS: this converts for reporting, and nothing
commands a position through it. TRIGGER_SPEED_TURNS_S is a control input and
deliberately stays in turns/s, which is the one place this stand still differs
from ydrive's TRIGGER_SPEED_M_S.

A converted figure is not automatically a measured one: the load encoder's
resolution sets the floor, and a slip of a few counts is the smallest travel this
stand can distinguish from zero."""

def turns_to_metres(turns: float) -> float:
    """Turns of the motor as metres of travel. Reporting only - see METERS_PER_TURN.

    A function because a run's derived channels evaluate on the state publisher's
    thread, where reaching through a testbed to a telemetry socket would be a race."""
    return float(turns) * METERS_PER_TURN

RAILS = (BRAKE_BUS,)
"""Every CPX400DP output this stand uses. start() iterates this to configure
setpoints, and derives the driver's ceiling from it - which with the brake alone
is 24 V and 5 A, tight enough to be real protection on the one rail left.

Output 2 is absent because the N6974A took the motor bus, and it is wired to
nothing. start() still switches it off: a supply adopts the output state it is
started into, and an energized output with open terminals is worth dropping."""

BRAKE_SETTLE_S = 0.25
"""Seconds to wait after switching the brake rail, before moving or dwelling.

A placeholder to be replaced with the brake's datasheet figure. A brake is not
instantaneous: the coil field has to collapse before a magnet-applied brake
grabs, and build before it lets go."""


class ZdriveTestbed:
    """Starts/stops the ODrive and CPX400DP driver processes for zdrive, and owns connected clients for both."""

    DEVICES: Tuple[str, ...] = (DEVICE_ODRIVE, DEVICE_CPX400DP, DEVICE_N6974A, DEVICE_TC_DAQ)
    """The devices whose driver processes this testbed owns. Declared here
    because this is what starts them; the test case unions this with its DUT
    façade's declaration (zdrive has none, as with ydrive) and publishes the
    result, so the telemetry engine records both devices into the run's
    directory. See testcases/base.py's DEVICES."""

    def __init__(
        self,
        use_mock_odrive: bool = False,
        odrive_serial_number: Optional[str] = None,
        cpx400dp_host: str = CPX400DP_HOST,
        n6974a_host: str = N6974A_HOST,
        tc_daq_port: Optional[str] = None,
        output_dir: Optional[Path] = None,
        test_id: Optional[str] = None,
    ) -> None:
        """
        use_mock_odrive: run MockOdriveBackend instead of real USB hardware.
            There is no equivalent for either supply - neither driver has a mock
            backend - so this testbed always needs a reachable CPX400DP and
            N6974A.

        output_dir/test_id: where each driver writes its detailed log. Given
            both, every driver gets `--log-file <output_dir>/runs/<test_id>/
            <device>/logs.txt`, so a decoded ODrive fault or a refused setpoint
            lands beside the telemetry it happened during and the stored run
            explains itself. Omit them and the drivers log to their consoles
            only, which for a subprocess means nowhere anybody reads.

            A test case passes `self._output_dir` and `self.test_id` from
            PreTestSetup; both are resolved by then, since run() resolves the
            engine's real output dir from the heartbeat before that phase. The
            testbed is what starts the drivers, so it is the only participant
            that can hand them the path - the drivers themselves know nothing
            about runs.
        """
        self._use_mock_odrive = use_mock_odrive
        self._odrive_serial_number = odrive_serial_number
        self._cpx400dp_host = cpx400dp_host
        self._n6974a_host = n6974a_host
        self._tc_daq_port = tc_daq_port
        self._output_dir = output_dir
        self._test_id = test_id
        self._processes: List[subprocess.Popen] = []
        self._device_for_process: List[str] = []
        self._command: Optional[OdriveCommandClient] = None
        self._telemetry: Optional[TelemetryClient] = None
        self._sync_telemetry: Optional[TelemetryClient] = None
        self._supply: Optional[Cpx400dpCommandClient] = None
        self._supply_telemetry: Optional[TelemetryClient] = None
        self._bus: Optional[N6974aCommandClient] = None
        self._bus_telemetry: Optional[TelemetryClient] = None
        self._tc_daq_telemetry: Optional[TelemetryClient] = None

    # --- lifecycle ---------------------------------------------------------

    def _log_args(self, device: str) -> List[str]:
        """`--log-file` for this device's driver, or nothing if this testbed
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
        """Bring all four drivers up, verify their channel surfaces, and
        configure the motor bus, the brake rail and the ODrive's current limits -
        with every output left OFF."""
        odrive_args = [sys.executable, "-m", "hardware.odrive.main", *self._log_args(DEVICE_ODRIVE)]
        if self._use_mock_odrive:
            odrive_args.append("--mock")
        elif self._odrive_serial_number is not None:
            odrive_args += ["--serial-number", self._odrive_serial_number]

        # The driver-side ceiling is per-backend, not per-output, so the only
        # values it can carry are the maxima across both rails. That makes it a
        # coarse backstop against a gross typo (60 V, 20 A) rather than per-rail
        # protection: as far as the driver is concerned, 48 V on the 24 V brake
        # rail is a legal command. Nothing here can prevent that - a test holds
        # the same supply client - so check_rails() detects it instead, and is
        # the thing to call if a rail's integrity matters mid-run.
        supply_args = [
            sys.executable, "-m", "hardware.cpx400dp.main",
            "--host", self._cpx400dp_host,
            "--max-voltage", str(max(rail.voltage_v for rail in RAILS)),
            "--max-current", str(max(rail.current_limit_a for rail in RAILS)),
            *self._log_args(DEVICE_CPX400DP),
        ]

        # --dissipators is required and verified against the instrument at
        # connect, so a dissipator that was cabled to an already-running supply
        # fails the run here rather than silently halving the stand's sinking.
        bus_args = [
            sys.executable, "-m", "hardware.n6974a.main",
            "--host", self._n6974a_host,
            "--dissipators", str(N6974A_DISSIPATORS),
            *self._log_args(DEVICE_N6974A),
        ]

        # The thermocouple DAQ takes no commands at all, so it gets no command
        # client below - nothing would be sendable through one. Its driver is
        # started, its stream is verified, and that is the whole interface. With
        # no --port it finds itself by its USB bridge's vendor id.
        tc_daq_args = [
            sys.executable, "-m", "hardware.tc_daq.main",
            *(["--port", self._tc_daq_port] if self._tc_daq_port else []),
            *self._log_args(DEVICE_TC_DAQ),
        ]

        self._processes = [
            start_driver(odrive_args, self._console_path(DEVICE_ODRIVE)),
            start_driver(supply_args, self._console_path(DEVICE_CPX400DP)),
            start_driver(bus_args, self._console_path(DEVICE_N6974A)),
            start_driver(tc_daq_args, self._console_path(DEVICE_TC_DAQ)),
        ]
        self._device_for_process = [
            DEVICE_ODRIVE, DEVICE_CPX400DP, DEVICE_N6974A, DEVICE_TC_DAQ,
        ]
        time.sleep(STARTUP_DELAY_S)

        self._command = OdriveCommandClient(endpoint=DEFAULT_ODRIVE_COMMAND_ENDPOINT)
        self._telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._sync_telemetry = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        self._supply = Cpx400dpCommandClient(endpoint=DEFAULT_CPX400DP_COMMAND_ENDPOINT)
        self._supply_telemetry = TelemetryClient(endpoint=DEFAULT_CPX400DP_TELEMETRY_ENDPOINT)
        self._bus = N6974aCommandClient(endpoint=DEFAULT_N6974A_COMMAND_ENDPOINT)
        self._bus_telemetry = TelemetryClient(endpoint=DEFAULT_N6974A_TELEMETRY_ENDPOINT)
        self._tc_daq_telemetry = TelemetryClient(
            endpoint=DEFAULT_TC_DAQ_TELEMETRY_ENDPOINT, timeout_s=TC_DAQ_STALENESS_S
        )

        # Before waiting on a command server or a telemetry deadline: a driver
        # that has already exited will never answer, and its own log says why.
        self._require_drivers_alive()
        self._command.connect_backend()
        self._supply.connect_backend()
        self._bus.connect_backend()

        self._command.verify_actions(ODRIVE_COMMAND_CHANNELS)
        self._telemetry.verify_channels(ODRIVE_TELEMETRY_CHANNELS)
        self._supply.verify_actions(CPX400DP_COMMAND_CHANNELS)
        self._supply_telemetry.verify_channels(CPX400DP_TELEMETRY_CHANNELS)
        self._bus.verify_actions(N6974A_COMMAND_CHANNELS)
        self._bus_telemetry.verify_channels(N6974A_TELEMETRY_CHANNELS)
        # No verify_actions for the DAQ: it declares no commands, so there is
        # nothing to confirm. Its stream is the only thing to check, and a
        # faulted thermocouple still publishes its channel (as None), so this
        # passes with sensors unplugged - what it catches is a driver that
        # started against the wrong port and is streaming something else.
        self._tc_daq_telemetry.verify_channels(TC_DAQ_TELEMETRY_CHANNELS)

        self._configure_bus()
        self._configure_rails()
        self._configure_odrive_limits()
        # After all three, not inside any one of them: a supply accepts and then
        # silently discards a value it dislikes, so this is the only evidence
        # that the stand holds what it was just told.
        self.check_rails()

    def _require_drivers_alive(self) -> None:
        """Raise if any driver process has already exited.

        Without this, a driver that died during startup - a supply at an address
        nothing answers, an ODrive that is not attached, a thermocouple DAQ that
        is unplugged - surfaces as a timeout naming neither the device nor the
        reason, and the DAQ's is the worst of them: it has no command client, so
        it surfaces as a telemetry staleness deadline rather than a refused
        connect. The exit code and the log path are what a person actually needs,
        and both are known here."""
        dead = [
            (device, process)
            for device, process in zip(self._device_for_process, self._processes)
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

    def _configure_bus(self) -> None:
        """Switch the motor bus off, put it in voltage priority, then program its
        voltage, its limits and its shutdown behaviour.

        Off first for the same reason the rails are: the driver adopts the output
        state it is started into, so the bus can still be live from a previous
        run, and writing 48 V to a live output would step the ODrive's bus. It is
        also what makes the priority-mode write legal at all - the driver refuses
        that switch while the output is on.

        THE PRIORITY MODE IS WRITTEN BEFORE EVERYTHING ELSE, AND THE ORDER IS
        LOAD-BEARING. Switching it reverts every output setting to its reset
        value, so the same write after the setpoints would silently undo them.
        Written unconditionally, so a run begins from the instrument's reset
        state whatever the last user left behind, at the cost of discarding
        settings this stand does not manage - OVP level, OCP, the watchdog, NPLC,
        measurement ranges and digital pin functions are all reset with it.

        The sink limit is written rather than assumed. Recognising an N7909A
        raises what the instrument permits in the negative direction without
        moving the active setpoint, so an unprogrammed supply sinks 10% of its
        rating while this stand is built around it sinking 50%."""
        self.bus.enable_output(False)
        logger.info("motor bus off - configuring setpoints on a de-energized bus")

        self.bus.set_priority_mode(MOTOR_BUS.priority_mode)
        logger.info(
            "motor bus in %s priority - every output setting is now at its reset value, and what "
            "follows is what this stand puts back",
            MOTOR_BUS.priority_mode,
        )

        self.bus.set_voltage(MOTOR_BUS.voltage_v)
        self.bus.set_current_limit(MOTOR_BUS.current_limit_a)
        self.bus.set_current_limit_negative(MOTOR_BUS.sink_current_limit_a)
        self.bus.set_protection_mode(MOTOR_BUS.protection_mode)
        logger.info(
            "%s configured: %.1f V, sourcing to %.1f A (%.0f W), sinking to %.2f A (%.0f W), "
            "%s shutdown",
            MOTOR_BUS.name, MOTOR_BUS.voltage_v,
            MOTOR_BUS.current_limit_a, MOTOR_BUS.source_power_w,
            MOTOR_BUS.sink_current_limit_a, MOTOR_BUS.sink_power_w, MOTOR_BUS.protection_mode,
        )

    def _configure_odrive_limits(self) -> None:
        """Program the ODrive's regen cap and motor current limits.

        These are persistent device state, written every run so the stand does
        not depend on what the last person to touch the board left behind. The
        regen cap in particular ships at 0.0, which returns nothing to the supply
        and would leave the two-quadrant bus doing no work at all."""
        self.command.set_board_config_max_regen_current(ODRIVE_MAX_REGEN_CURRENT_A)
        self.command.set_motor_config_current_soft_max(ODRIVE_MOTOR_SOFT_MAX_A)
        self.command.set_motor_config_current_hard_max(ODRIVE_MOTOR_HARD_MAX_A)
        logger.info(
            "ODrive limits configured: regen cap %.1f A (bus sinks to %.2f A), motor phase "
            "current %.1f A soft / %.1f A hard",
            ODRIVE_MAX_REGEN_CURRENT_A, MOTOR_BUS.sink_current_limit_a,
            ODRIVE_MOTOR_SOFT_MAX_A, ODRIVE_MOTOR_HARD_MAX_A,
        )

    def _configure_rails(self) -> None:
        """Switch every CPX400DP output off, then set each used rail's voltage
        and current setpoints.

        A run starts from a de-energized stand whatever it finds. The supply's
        driver adopts, rather than resets, the output state it is started into,
        so a rail can still be live from a previous run - and writing a setpoint
        to a live output would step that rail under whatever is connected to it.
        Switching off first makes every setpoint below land on a dead rail.

        Powering anything back up is then a test's own decision, taken in
        PreTestSetup."""
        # Every output the instrument has, not just the ones this stand uses -
        # see RAILS for output 2.
        for output in CPX400DP_OUTPUTS:
            self.supply.enable_output(output, False)
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

    SETPOINT_TOLERANCE = 0.02
    """How far a read-back setpoint may sit from its configured value before
    check_rails() calls it wrong. The instrument reports voltage setpoints to
    10 mV and current to 1 mA, so this is a rounding allowance, not a band."""

    SETPOINT_SETTLE_ATTEMPTS = 6
    SETPOINT_SETTLE_DELAY_S = 0.2
    """How many times, and how far apart, check_rails() re-reads before calling a
    setpoint wrong.

    A telemetry frame can be older than the write it is being asked about. The
    CPX400DP driver holds setpoints in a cached tier, re-read at connect and
    after a write rather than every frame, and latest_frame() answers with the
    newest frame already queued rather than waiting for one published after the
    write. So the first read can legitimately still carry the previous run's
    values - which is invisible whenever those happen to match, and a spurious
    failure whenever they do not."""

    def check_rails(self) -> None:
        """Confirm the motor bus and every rail still hold their configured
        setpoints, raising if not.

        Called by start() once everything has been configured, because a supply
        accepts and then silently discards a value it dislikes - a write is not
        evidence of a setpoint.
        Also worth calling from a test wherever the stand's integrity matters,
        because neither driver's ceiling is narrow enough to police this stand:
        the CPX's is per-backend, so it cannot stop 24 V being commanded onto the
        brake rail's neighbour, and the N6974A's is its own 80 V rating, which is
        far above what this bus may see. Both are correct for a driver serving
        more than one stand, and both leave this check as the thing that knows
        zdrive runs at 48 V.

        Re-reads a few times before failing - see SETPOINT_SETTLE_ATTEMPTS."""
        for attempt in range(self.SETPOINT_SETTLE_ATTEMPTS):
            wrong = self._setpoint_disagreements()
            if not wrong:
                return
            if attempt + 1 < self.SETPOINT_SETTLE_ATTEMPTS:
                time.sleep(self.SETPOINT_SETTLE_DELAY_S)
        raise RuntimeError(
            "this stand's setpoints do not match its configuration:\n  "
            + "\n  ".join(wrong)
            + "\nSomething commanded a setpoint outside this testbed, or a write was refused. "
            "See MOTOR_BUS/BRAKE_BUS in this module for what they should be."
        )

    def _setpoint_disagreements(self) -> List[str]:
        """Every configured setpoint the stand is not currently holding, from one
        telemetry frame per device."""
        bus = self.get_bus_channels()
        wrong = []
        for quantity, expected, channel in (
            ("bus voltage", MOTOR_BUS.voltage_v, "setpoint_voltage"),
            ("bus current limit", MOTOR_BUS.current_limit_a, "current_limit"),
            ("bus sink limit", MOTOR_BUS.sink_current_limit_a, "current_limit_negative"),
        ):
            actual = bus[channel]
            if abs(float(actual) - expected) > self.SETPOINT_TOLERANCE:
                wrong.append(
                    f"{MOTOR_BUS.name} {quantity}: expected {expected}, instrument holds {actual}"
                )
        if bus["protection_mode"] != MOTOR_BUS.protection_mode:
            wrong.append(
                f"{MOTOR_BUS.name} shutdown mode: expected {MOTOR_BUS.protection_mode}, "
                f"instrument holds {bus['protection_mode']} - a protection trip would leave the "
                "bus capacitance charged rather than pulling it down"
            )
        if bus["priority_mode"] != MOTOR_BUS.priority_mode:
            wrong.append(
                f"{MOTOR_BUS.name} priority mode: expected {MOTOR_BUS.priority_mode}, instrument "
                f"holds {bus['priority_mode']} - the wrong pair of settings is regulating, so the "
                "bus voltage above is not the value being held. start() writes this before "
                "anything else, so finding it wrong here means the write was refused or something "
                "switched it afterwards"
            )

        channels = self.get_supply_channels()
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
        """Tear the stand down in a safe order, and finish even if a step fails.

        The order matters. The brake is magnet-applied, so dropping its rail
        first makes the brake grab and hold the load before anything else
        changes; only then is the axis disarmed and the motor bus removed. The
        reverse order would leave the load unheld while the drive shuts down, and
        a de-energized stand is one whose brake is holding.

        Each step runs independently, logging a failure rather than raising, so
        one wedged client cannot leave a 48 V bus energized."""
        # A plain sleep rather than TestCase.wait_for(): teardown has no test
        # case to poll, and nothing it could usefully abort for.
        self._safe("engage the brake (drop the 24 V rail)", self._engage_brake_for_teardown)
        self._safe("disarm the ODrive axis", lambda: self.command.set_axis_state("IDLE"))
        self._safe("drop the 48 V motor bus", lambda: self.power_motor_bus(False))

        self._safe("disconnect the ODrive backend", lambda: self.command.disconnect_backend())
        self._safe("disconnect the supply backend", lambda: self.supply.disconnect_backend())
        self._safe("disconnect the motor bus backend", lambda: self.bus.disconnect_backend())

        for client in (self._command, self._telemetry, self._sync_telemetry,
                       self._supply, self._supply_telemetry,
                       self._bus, self._bus_telemetry, self._tc_daq_telemetry):
            if client is not None:
                self._safe(f"close {type(client).__name__}", client.close)
        self._command = self._telemetry = self._sync_telemetry = None
        self._supply = self._supply_telemetry = None
        self._bus = self._bus_telemetry = None
        self._tc_daq_telemetry = None

        for process in self._processes:
            self._safe(f"terminate pid {process.pid}", process.terminate)
        for process in self._processes:
            self._safe(f"reap pid {process.pid}", lambda p=process: p.wait(timeout=5))
        self._processes = []

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
        """Switch the 48 V motor bus on or off at the N6974A.

        The output ramps rather than stepping, so a check immediately after
        enabling reads low. Switching off is a condition of zero output voltage
        and zero source current rather than a disconnection, so the bus is left
        with nothing holding it up but nothing actively pulling it down either -
        only a protection trip does that, which is what MOTOR_BUS's LOWZ mode is
        for."""
        self.bus.enable_output(enabled)
        logger.info("%s %s", MOTOR_BUS.name, "energized" if enabled else "de-energized")

    def power_brake_bus(self, enabled: bool) -> None:
        """Switch the 24 V brake rail (output 1) on or off.

        Powering RELEASES the brake; removing power lets it grab. This only moves
        the rail. It does NOT wait for the brake to act on it, and does NOT touch
        the axis state - so calling it directly can leave the controller driving
        against an engaged brake, or the brake letting go of a load nothing is
        holding. Pairing the rail with the axis state, and waiting through
        TestCase.wait_for() so a fatal bound is still noticed, belongs in a test
        step; see testcases/ydrive/teststeps/teststeps.py's
        engage_brake()/release_brake() for the shape."""
        self.supply.enable_output(BRAKE_BUS.output, enabled)
        logger.info("%s %s", BRAKE_BUS.name, "released (rail energized)" if enabled else "engaged (rail de-energized)")


    # --- reads ------------------------------------------------------------

    def get_channels(self) -> Dict[str, object]:
        """Block for the next ODrive telemetry frame and return its channels.
        Uses a separate sync client so it doesn't contend with whatever else is
        consuming .telemetry (e.g. LiveRulebookRunner)."""
        return self.sync_telemetry.latest_frame().channels

    def get_fet_temperature_c(self) -> float:
        """The inverter FET temperature, in Celsius, off the ODrive's own thermistor.

        The drive's own thermal state, which no thermocouple on this stand measures: the
        TCs are on the brake and the motor. The board derates its current limit above
        board_config_inverter0_temp_limit_lower (83.96 C measured) and disarms above the
        upper limit (103.11 C), and a derate would quietly change what a lift does."""
        return float(self.get_channels()["motor_fet_thermistor_temperature"])

    def get_tc_temperatures_c(self) -> Dict[int, float]:
        """Every wired thermocouple, by channel number, in Celsius.

        Only the channels carrying a number: this DAQ streams eight and reports FAULT for
        one it cannot read, which the driver publishes as None. A caller comparing against
        a limit wants the readings that exist rather than a None to guard against - and a
        channel going open is already fatal through the rulebook's own bound, which is a
        better place to notice it than a flow-control check."""
        channels = self.tc_daq_telemetry.latest_frame().channels
        readings = {}
        for name, value in channels.items():
            if name.startswith("temperature_") and name.endswith("_c"):
                if isinstance(value, (int, float)):
                    readings[int(name.split("_")[1])] = float(value)
        return readings

    def get_supply_channels(self) -> Dict[str, object]:
        """Block for the next supply telemetry frame and return its channels.

        The measured voltage and current are re-read at 5 Hz and held between
        reads, since the instrument's meters refresh at 4 Hz, so consecutive
        frames can carry the same reading."""
        return self.supply_telemetry.latest_frame().channels

    def get_motion(self) -> Motion:
        """Position, velocity and whether the axis is driving, from one frame.

        Raises if the position is not a usable number - see
        _require_finite_position()."""
        channels = self.get_channels()
        return Motion(
            position=self._require_finite_position(channels),
            velocity=channels["vel_estimate"],
            armed=bool(channels["axis_is_armed"]),
        )

    def get_faults(self) -> Dict[str, str]:
        """Every watched ODrive channel currently reading as a fault, decoded -
        empty when the board is clean. One frame, so it describes one instant."""
        return odrive_errors.faults_in_frame(self.get_channels())

    def describe_errors(self) -> Dict[str, str]:
        """Every watched channel decoded, faulted or not - the diagnostic for
        "why did the axis refuse", where a channel reading NOMINAL is as much of
        the answer as one reading a fault. One frame."""
        channels = self.get_channels()
        return {
            name: odrive_errors.describe(name, channels[name])
            for name in odrive_errors.WATCHED_CHANNELS
            if name in channels
        }

    def get_pos_estimate(self) -> float:
        """Where the axis is, in turns.

        Raises if the reading is not a usable number - see
        _require_finite_position()."""
        return self._require_finite_position(self.get_channels())

    def _require_finite_position(self, channels: Dict[str, object]) -> float:
        """This frame's `pos_estimate`, or raise if it is not a finite number.

        pos_estimate READS NaN WHILE EVERY OTHER CHANNEL LOOKS HEALTHY - no active
        errors, `encoder_onboard0_status` NOMINAL, velocity tracking normally.
        Nothing downstream survives that quietly: every comparison against a NaN
        is False, so a move never judges itself arrived and times out at full
        length, and a NaN taken as this run's origin propagates into every target
        derived from it. On this axis a target is a distance off the ground.

        TWO CAUSES, AND THE MAPPER STATUSES TELL THEM APART - which is why the
        message below prints them. RELATIVE_MODE with no valid
        pos_vel_mapper offset means the board has never been calibrated.
        MISSING_INPUT means the encoder is not delivering a usable signal at all,
        and no amount of calibrating fixes that; a dead sensor streams a random
        angle that both mappers reject, and calibration against it can still
        report success.

        So the reading is rejected here, at the one place both position accessors
        pass through, rather than left for each caller to test. Raising is safe
        wherever a position is read: nothing in stop() reads one, so the brake
        still grabs and the bus still drops."""
        raw = channels["pos_estimate"]
        try:
            position = float(raw)
        except (TypeError, ValueError):
            position = float("nan")
        if math.isfinite(position):
            return position

        posvel = odrive_errors.describe("posvelmapper_status", channels.get("posvelmapper_status"))
        commut = odrive_errors.describe("commutmapper_status", channels.get("commutmapper_status"))
        raise RuntimeError(
            f"the ODrive published pos_estimate={raw!r}, which is not a position anything can be "
            f"commanded relative to. posvelmapper_status={posvel}, commutmapper_status={commut}. "
            "MISSING_INPUT on either means the encoder is not delivering a usable signal - check "
            "the sensor and its magnet, since a dead encoder streams a random angle that both "
            "mappers reject and that calibration can still appear to succeed against. "
            "RELATIVE_MODE on posvelmapper with the encoder otherwise healthy means the opposite: "
            "axis0.pos_vel_mapper.config.offset_valid is False and the board needs its encoder "
            "offset calibration run and saved"
        )

    def get_axis_armed_status(self) -> bool:
        """Whether the axis is actively controlling the motor (`axis_is_armed`).

        Requesting an axis state only writes `requested_state`; the ODrive acts on
        it asynchronously and can decline. This is the reading that says whether
        it took."""
        return bool(self.get_channels()["axis_is_armed"])

    def get_vel_estimate(self) -> float:
        return self.get_channels()["vel_estimate"]


    def get_bus_channels(self) -> Dict[str, object]:
        """Block for the next motor-bus telemetry frame and return its channels.

        `voltage`, `current` and `power` all come from one acquisition, so they
        are simultaneous rather than read a sample apart."""
        return self.bus_telemetry.latest_frame().channels

    def get_brake_voltage(self) -> float:
        return self.get_supply_channels()[f"voltage_{BRAKE_BUS.output}"]

    def get_brake_current(self) -> float:
        return self.get_supply_channels()[f"current_{BRAKE_BUS.output}"]

    def get_bus_voltage(self) -> float:
        return self.get_bus_channels()["voltage"]

    def get_bus_current(self) -> float:
        """The bus current at the supply: positive sourcing, negative while the
        supply absorbs regen."""
        return self.get_bus_channels()["current"]


    @property
    def tc_daq_telemetry(self) -> TelemetryClient:
        """The thermocouple DAQ's stream.

        The only interface this device has - it accepts no commands, so there is
        no command client to pair with it."""
        if self._tc_daq_telemetry is None:
            raise RuntimeError("ZdriveTestbed.tc_daq_telemetry accessed before start()")
        return self._tc_daq_telemetry

    @property
    def command(self) -> OdriveCommandClient:
        if self._command is None:
            raise RuntimeError("ZdriveTestbed.command accessed before start()")
        return self._command

    @property
    def telemetry(self) -> TelemetryClient:
        if self._telemetry is None:
            raise RuntimeError("ZdriveTestbed.telemetry accessed before start()")
        return self._telemetry

    @property
    def sync_telemetry(self) -> TelemetryClient:
        if self._sync_telemetry is None:
            raise RuntimeError("ZdriveTestbed.sync_telemetry accessed before start()")
        return self._sync_telemetry

    @property
    def supply(self) -> Cpx400dpCommandClient:
        if self._supply is None:
            raise RuntimeError("ZdriveTestbed.supply accessed before start()")
        return self._supply

    @property
    def supply_telemetry(self) -> TelemetryClient:
        if self._supply_telemetry is None:
            raise RuntimeError("ZdriveTestbed.supply_telemetry accessed before start()")
        return self._supply_telemetry

    @property
    def bus(self) -> N6974aCommandClient:
        if self._bus is None:
            raise RuntimeError("ZdriveTestbed.bus accessed before start()")
        return self._bus

    @property
    def bus_telemetry(self) -> TelemetryClient:
        if self._bus_telemetry is None:
            raise RuntimeError("ZdriveTestbed.bus_telemetry accessed before start()")
        return self._bus_telemetry

    def __enter__(self) -> "ZdriveTestbed":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
