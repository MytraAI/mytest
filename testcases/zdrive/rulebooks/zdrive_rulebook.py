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

- motor_current_bound: motor_foc_iq_measured beyond +/-36A, fatal, no
  persistence.

  36 A is the ODrive's own current_hard_max, which ZdriveTestbed writes; firmware
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

MAX_MOTOR_CURRENT_A = 36.0
"""Fatal ceiling on measured motor phase current, in both directions - the
ODrive's own current_hard_max, which ZdriveTestbed writes to the board."""

MIN_BUS_VOLTAGE_V = 10.5
"""Fatal floor on the DC bus measured at the ODrive - the same value as the
board's dc_bus_undervoltage_trip_level."""

BASE_ZDRIVE_TEST_NAME = "base_zdrive_test"
MANUAL_TEST_NAME = "zdrive_manual_test"

TEST_NAMES = [BASE_ZDRIVE_TEST_NAME, MANUAL_TEST_NAME]

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
    ],
)
