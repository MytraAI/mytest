"""Evaluation Rulebook for zdrive: four fatal safety-net bounds, checked
regardless of what a given test's main_execution actually does.

THE BOUNDS SPAN TWO DEVICES, and each stream evaluates only the bounds whose
channels it carries. `voltage` and `current` are the N6974A's, measured at the
supply; `motor_foc_iq_measured` and `board_vbus_voltage` are the ODrive's. A
bound whose channel is absent from a frame returns no result, so a runner has to
be started against both streams or half of this rulebook silently never runs -
see ManualTest.

- bus_overvoltage_bound: voltage > 52V, fatal, no persistence.

  THIS IS THE ONLY THING THAT KNOWS THIS STAND RUNS AT 48 V. The N6974A's driver
  clamps to the instrument's own 80 V rating, correctly - it serves any stand,
  including one running a higher bus - so nothing below this bound stops 80 V
  being commanded onto an ODrive that trips itself at 55 V. ZdriveTestbed
  configures 48 V and check_rails() confirms it, which covers a setpoint that was
  never right; this covers one that stopped being right mid-run.

  52 V leaves room above the ~48.5 V the bus actually sits at - the remote sense
  leads are open on this stand, so the instrument regulates from local sense
  about 1% high - and sits under the ODrive's own 55 V trip, so the run ends
  before the board acts on its own behalf.

  Undebounced: a bus that has climbed 4 V above its setpoint is not a sample to
  wait out.

- bus_current_bound: current > 25A or < -12.75A, fatal, debounced.

  Both directions, because this stand's whole purpose lives in the negative one.
  25 A is the supply's rated output and a gross-fault net rather than an
  operating limit: the expected peak is about 8 A, since bus current is
  mechanical power over bus voltage rather than motor phase current. It is
  reachable precisely because current limiting is not done here - the ODrive's
  soft/hard phase limits do that - so the supply's positive limit is left wide
  and the bus really can draw 25 A into a fault.

  -12.75 A is the whole sinking capability one N7909A gives a 2 kW model, so the
  bound fires when absorption saturates. Past that the bus rises instead, and
  what catches it is the external clamp - autonomously, on its own threshold,
  reporting nothing to any channel here. That silence is why this bound is worth
  having: it is the last observable before the invisible part of the regen chain
  takes over.

  Debounced, because energizing the bus charges the ODrive's capacitance and the
  inrush is a spike, not a fault.

- motor_current_bound: motor_foc_iq_measured beyond +/-71A, fatal, no
  persistence.

  71 A is the motor's rated current, and the ODrive's own current_hard_max, which
  ZdriveTestbed writes; firmware trips CURRENT_LIMIT_VIOLATION there within a
  control-loop period, far faster than telemetry samples. So this bound usually
  catches the aftermath rather than the excursion - which is the point. The known
  failure on this stand is the ODrive disarming itself while the run continues,
  the load coasting, and nothing raising. Bounded in both directions: braking
  torque loads a motor exactly as driving torque does.

  IT DOES NOT BOUND WHAT A LIFT MAY DRAW. What the controller is allowed to
  command is ODRIVE_MOTOR_SOFT_MAX_A, 16 A below this, and a demand above that
  clamps rather than being delivered. A lift that needs more torque than the soft
  limit allows therefore falls behind its trajectory - it does not arrive here.
  This is the motor's rating being protected, not the stand's duty being graded.

- undervoltage_bound: board_vbus_voltage < 10.5V, fatal, no persistence -
  trusted instantaneously rather than debounced. Mirrors the ODrive's own
  dc_bus_undervoltage_trip_level, so the run ends on the same threshold the
  firmware acts on.

  Read at the ODrive rather than at the supply deliberately: what matters is the
  voltage where it is consumed, and with the sense leads open the two differ.

- overspeed_bound: vel_estimate beyond +/-40 turns/s, fatal, no persistence.

  THE LOAD IS MOVING FASTER THAN ANYTHING COMMANDED IT TO. This is the bound for
  a fall: a disarmed axis on a loaded stand does not stop, it accelerates under
  1000 lb, and every other bound here stays green while it does. Bus voltage is
  nominal, bus current is zero because the drive is no longer drawing, phase
  current is zero, nothing is hot. Without this the run records a drop and
  reports a pass.

  Both directions, because a load running away upward is the same fault as one
  running away downward and neither is commanded. 40 turns/s is over twice the
  23.8 turns/s peak seen across a 242-cycle run - the controller's own vel_limit
  is 18 turns/s and the estimate overshoots it - and under half the 92.9 turns/s
  a released 1000 lb load reached on this axis, so it separates a runaway from
  the top of a normal stroke by a wide margin in both directions.

  Undebounced. Like brake_slip_bound this cannot PREVENT a fall - at the
  acceleration this axis showed, the load is past 40 turns/s within one telemetry
  frame of letting go - and for the same reason debouncing it would be
  meaningless: the second frame of a fall is not more informative than the first,
  it is just later. What it buys is that the drop lands in the verdict as a fatal
  violation instead of in the log as an error with PASSing bounds.

- overtemperature_bound_<n>: temperature_<n>_c > 70C on every LIVE thermocouple
  channel, fatal, debounced 5s.

  ONLY THE LIVE CHANNELS ARE BOUNDED. The DAQ streams eight and reports FAULT for
  one it cannot read, which the driver publishes as temperature_<n>_c = None. A
  numeric bound on a None is unevaluable, and the runner treats a bound it cannot
  evaluate as a stop - correctly, since a bound that was skipped is not a bound
  that passed. So bounding an unconnected channel would abort every run on its
  first frame. LIVE_TC_CHANNELS is which ones are wired, and it is stand
  configuration: move a thermocouple and it has to change with it.

  That cuts the other way too, deliberately. If a bounded channel goes open
  mid-run its bound becomes unevaluable and the run stops - the wanted behaviour
  for a thermal limit, since losing the sensor you were relying on is not a
  reason to keep driving, but it means a flaky junction ends runs.

  Debounced 5s against electrical spikes, this stand switching 48 V a few feet
  from the harness. Affordable on a thermal limit because thermal mass is slow,
  which is exactly why it is the wrong dial for the bus bounds. THE DEBOUNCE DOES
  NOT COVER AN OPEN CHANNEL: a faulted channel is unevaluable rather than
  violated, so these carry TC_DROPOUT_GRACE_S instead, longer because this DAQ
  drops the odd sample and one dropped sample is not a lost sensor.

- brake_slip_bound: brake_slip_m > 0.010, fatal. A brake-has-let-go trip, not
  a wear threshold - measured slip is one encoder count. See MAX_BRAKE_SLIP_M.
- fet_overtemperature_bound: motor_fet_thermistor_temperature > 80 C for 5 s,
  fatal. Below the board's own 83.96 C derate point, and above the threshold at
  which a cycling test stops lifting and waits. See MAX_FET_TEMPERATURE_C.
- stopping_distance_bound: stopping_distance_m > 0.25, fatal, no persistence.

  NOT A HARDWARE CHANNEL. BrakeEnduranceTest publishes each brake event's stopping
  distance as run state, and the runner merges published state into what it
  evaluates - so a brake that no longer stops the load in 0.25 m aborts the run
  through the same path as any bus or motor bound, and the number that did it lands
  in the verdict's timeline instead of only in a log line.

  Undebounced deliberately, unlike the thermal bounds: this is not a sampled signal
  that can spike, it is one number per brake event, written once and then held until
  the next. One bad stop IS the event, and debouncing would mean waiting for a
  second one - on a stand where the first one already moved the load further than
  it should have.

  The channel is seeded 0.0 rather than None (see ../channels.py): a numeric bound
  on a channel carrying no value is unevaluable, and the runner treats unevaluable
  as a stop, so None would end every run on its first frame.

  A GROSS-FAULT NET, NOT A PERFORMANCE FIGURE. 0.25 m is 23.6 turns, and a healthy
  stop from this test's trigger speed is a fraction of a turn - the axis is
  effectively self-locking, so the screw stops the load about as much as the brake
  does. A stop that ran to 0.25 m would mean the brake and the screw had both let
  go. Placed to catch that rather than to grade a brake.

TEST_NAMES lists every concrete zdrive TestCase.TEST_NAME that starts a runner
against this Rulebook - add a new test's TEST_NAME here when it should be checked
against these same safety bounds too. Lives here rather than on the TestCase to
avoid a circular import (see ydrive's rulebook for the same pattern).
"""
from __future__ import annotations

from asimov.rulebook import Bound, Rulebook

MAX_BUS_VOLTAGE_V = 52.0
"""Fatal ceiling on the N6974A's measured output voltage. See this module's
docstring for why this bound, rather than the driver, is what holds this stand to
a 48 V bus."""

MAX_BUS_CURRENT_A = 25.0
"""Fatal ceiling on bus current drawn from the supply - this model's rating, and
roughly three times the expected peak."""

MIN_BUS_CURRENT_A = -12.75
"""Fatal floor on bus current, i.e. the most this supply will absorb: 50% of
rating, which is what one N7909A dissipator buys a 2 kW model."""

BUS_CURRENT_PERSISTENCE_S = 1.0
"""How long bus current must stay outside its bounds before the run stops.
Covers the inrush as the ODrive's bus capacitance charges on energize."""

MAX_MOTOR_CURRENT_A = 71.0
"""Fatal ceiling on measured motor phase current, in both directions - the
ODrive's own current_hard_max, which ZdriveTestbed writes to the board, and the
zdrive motor's rated current."""

MAX_TEMPERATURE_C = 70.0
"""Fatal ceiling on every live thermocouple channel."""

TC_PERSISTENCE_S = 5.0
"""How long a channel must stay above MAX_TEMPERATURE_C before the run stops.
A thermocouple spikes from electrical noise as well as from heat, and this stand
switches 48 V a few feet from the harness. Affordable on a thermal limit because
thermal mass is slow; the same dial would be wrong on a bus bound."""

TC_DROPOUT_GRACE_S = 10.0
"""How long a bounded channel may read FAULT before the run stops. This DAQ drops
the odd sample, and one dropped sample is not a lost sensor; a thermocouple that
has come out reads FAULT forever."""

LIVE_TC_CHANNELS = (2,)
"""Which thermocouple inputs are wired on this stand, and so the only ones
bounded. One channel, so this is the whole of the stand's thermocouple cover. Stand configuration: unplug one and this has to change with it, because
a numeric bound on an unread channel is unevaluable and stops every run on its
first frame."""

MAX_STOPPING_DISTANCE_M = 0.25
"""How far the load may travel after the brake is commanded, measured from the
command rather than from when the brake bites - so it includes the coast through
BRAKE_SETTLE_S.

Metres, matching ZdriveTestbed.METERS_PER_TURN: 0.25 m is 23.6 turns of this
drive. Measured stops on a 1000 lb load ran 0.066 to 0.080 m, and that figure is
known to UNDER-report - the baseline is taken a telemetry frame after the brake
was commanded, which at the speeds involved omits 0.047 to 0.091 m."""

MAX_BRAKE_SLIP_M = 0.010
"""How far the load may slip while the brake alone is holding it.

A "THE BRAKE HAS LET GO" TRIP, NOT A WEAR THRESHOLD, and the measurements say so: over
73 holds of 5 s at 1000 lb the recorded slip was +/-0.000001 m, one encoder count. This
is ten thousand times that, so nothing short of the brake releasing reaches it.

It cannot prevent a fall either. At the free-fall acceleration this axis showed, a fully
released brake covers 10 mm inside one telemetry frame, so by the time this fires the
load is already moving. What it buys is that a brake which has failed does not get
cycled another thousand times.

What would catch WEAR is the trend in brake_slip_m across a run, which at one micron of
resolution would show long before this threshold - and which no bound can express,
because a threshold tight enough to see it would fire on encoder noise."""

MAX_FET_TEMPERATURE_C = 80.0
"""Fatal ceiling on the ODrive's own inverter FET thermistor.

Below the 83.96 C at which this board begins derating its current limit, measured off
the drive: past that point a lift gets less current than the test believes it asked for.
Above teststeps.FET_WAIT_C, which is where a cycle stops lifting and waits - so reaching
this one means the stand went on heating while it was already being held back, which is
not a warm lab.

Debounced, unlike undervoltage_bound: this is one thermistor sampled every frame, and a
single bad reading should not end a run that may have been cycling for days."""

FET_PERSISTENCE_S = 5.0
"""How long the FET must stay above MAX_FET_TEMPERATURE_C before the run stops. The same
5 s the thermocouples get, for the same reason."""

MIN_BUS_VOLTAGE_V = 10.5
"""Fatal floor on the DC bus measured at the ODrive - the same value as the
board's dc_bus_undervoltage_trip_level."""

MAX_AXIS_SPEED_TURNS_S = 40.0
"""Fatal ceiling on axis speed in either direction: the load is no longer under
control of anything that commands a velocity.

Turns/s, matching the control path rather than METERS_PER_TURN - 40 turns/s is
0.423 m/s - because what this is compared against is the controller's vel_limit,
which is set in turns/s.

Placed to separate a runaway from a stroke, with room on both sides: measured
peaks over a 242-cycle run reach 23.8 turns/s against a controller vel_limit of
18, and a released 1000 lb load on this axis reached 92.9."""

BASE_ZDRIVE_TEST_NAME = "base_zdrive_test"
MANUAL_TEST_NAME = "zdrive_manual_test"
BRAKE_HOLD_TEST_NAME = "zdrive_brake_hold_test"
BRAKE_ENDURANCE_TEST_NAME = "zdrive_brake_endurance_test"
CYCLE_BRAKE_HOLD_TEST_NAME = "zdrive_cycle_brake_hold_test"

TEST_NAMES = [
    BASE_ZDRIVE_TEST_NAME,
    MANUAL_TEST_NAME,
    BRAKE_HOLD_TEST_NAME,
    BRAKE_ENDURANCE_TEST_NAME,
    CYCLE_BRAKE_HOLD_TEST_NAME,
]

ZDRIVE_RULEBOOK = Rulebook(
    name="zdrive_rulebook",
    test_names=TEST_NAMES,
    bounds=[
        Bound(
            channel="voltage",
            upper=MAX_BUS_VOLTAGE_V,
            name="bus_overvoltage_bound",
            fatal=True,
        ),
        Bound(
            channel="current",
            upper=MAX_BUS_CURRENT_A,
            lower=MIN_BUS_CURRENT_A,
            name="bus_current_bound",
            fatal=True,
            persistence_s=BUS_CURRENT_PERSISTENCE_S,
        ),
        Bound(
            channel="motor_foc_iq_measured",
            upper=MAX_MOTOR_CURRENT_A,
            lower=-MAX_MOTOR_CURRENT_A,
            name="motor_current_bound",
            fatal=True,
        ),
        Bound(
            channel="board_vbus_voltage",
            lower=MIN_BUS_VOLTAGE_V,
            name="undervoltage_bound",
            fatal=True,
        ),
        Bound(
            channel="vel_estimate",
            upper=MAX_AXIS_SPEED_TURNS_S,
            lower=-MAX_AXIS_SPEED_TURNS_S,
            name="overspeed_bound",
            fatal=True,
        ),
        Bound(
            channel="brake_slip_m",
            upper=MAX_BRAKE_SLIP_M,
            name="brake_slip_bound",
            fatal=True,
        ),
        Bound(
            channel="motor_fet_thermistor_temperature",
            upper=MAX_FET_TEMPERATURE_C,
            name="fet_overtemperature_bound",
            fatal=True,
            persistence_s=FET_PERSISTENCE_S,
        ),
        Bound(
            channel="stopping_distance_m",
            upper=MAX_STOPPING_DISTANCE_M,
            name="stopping_distance_bound",
            fatal=True,
        ),
        *[
            Bound(
                channel=f"temperature_{n}_c",
                upper=MAX_TEMPERATURE_C,
                name=f"overtemperature_bound_{n}",
                fatal=True,
                persistence_s=TC_PERSISTENCE_S,
                unevaluable_grace_s=TC_DROPOUT_GRACE_S,
            )
            for n in LIVE_TC_CHANNELS
        ],
    ],
)
