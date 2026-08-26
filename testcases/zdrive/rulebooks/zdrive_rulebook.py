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

- motor_current_bound: motor_foc_iq_measured beyond +/-60A, fatal, no
  persistence.

  60 A is the ODrive's own current_hard_max, which ZdriveTestbed writes; firmware
  trips CURRENT_LIMIT_VIOLATION there within a control-loop period, far faster
  than telemetry samples. So this bound usually catches the aftermath rather than
  the excursion - which is the point. The known failure on this stand is the
  ODrive disarming itself while the run continues, the load coasting, and nothing
  raising. Bounded in both directions: braking torque loads a motor exactly as
  driving torque does.

- undervoltage_bound: board_vbus_voltage < 10.5V, fatal, no persistence -
  trusted instantaneously rather than debounced. Mirrors the ODrive's own
  dc_bus_undervoltage_trip_level, so the run ends on the same threshold the
  firmware acts on.

  Read at the ODrive rather than at the supply deliberately: what matters is the
  voltage where it is consumed, and with the sense leads open the two differ.

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

  A GROSS-FAULT NET, NOT A PERFORMANCE FIGURE. 0.25 m is 26 turns, and a healthy
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

from testcases.asimov.rulebook import Bound, Rulebook

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

MAX_MOTOR_CURRENT_A = 60.0
"""Fatal ceiling on measured motor phase current, in both directions - the
ODrive's own current_hard_max, which ZdriveTestbed writes to the board."""

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

LIVE_TC_CHANNELS = (1, 2)
"""Which thermocouple inputs are wired on this stand, and so the only ones
bounded. Stand configuration: unplug one and this has to change with it, because
a numeric bound on an unread channel is unevaluable and stops every run on its
first frame."""

MAX_STOPPING_DISTANCE_M = 0.25
"""How far the load may travel after the brake is commanded, measured from the
command rather than from when the brake bites - so it includes the coast through
BRAKE_SETTLE_S.

Metres, matching ZdriveTestbed.METERS_PER_TURN: 0.25 m is 26 turns of this drive.
Measured stops on a 1000 lb load ran 0.060 to 0.073 m, and that figure is known to
UNDER-report - the baseline is taken a telemetry frame after the brake was
commanded, which at the speeds involved omits 0.043 to 0.083 m."""

MIN_BUS_VOLTAGE_V = 10.5
"""Fatal floor on the DC bus measured at the ODrive - the same value as the
board's dc_bus_undervoltage_trip_level."""

BASE_ZDRIVE_TEST_NAME = "base_zdrive_test"
MANUAL_TEST_NAME = "zdrive_manual_test"
BRAKE_HOLD_TEST_NAME = "zdrive_brake_hold_test"
BRAKE_ENDURANCE_TEST_NAME = "zdrive_brake_endurance_test"

TEST_NAMES = [
    BASE_ZDRIVE_TEST_NAME,
    MANUAL_TEST_NAME,
    BRAKE_HOLD_TEST_NAME,
    BRAKE_ENDURANCE_TEST_NAME,
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
