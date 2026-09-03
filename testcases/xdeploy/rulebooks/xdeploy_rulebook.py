"""Evaluation Rulebook for xdeploy: a deliberately minimal safety net - the bus
either side, the drive's own FET, and one bound per wired thermocouple - checked
regardless of what a given test's main_execution actually does.

THE BOUNDS SPAN TWO DEVICES, and each stream evaluates only the bounds whose
channels it carries. `board_vbus_voltage` is the ODrive's; `temperature_<n>_c`
is the DAQ's. A bound whose channel is absent from a frame returns no result, so
a runner has to be started against both streams or half of this rulebook
silently never runs - see ManualTest.

WHAT THIS RULEBOOK DELIBERATELY DOES NOT BOUND, AND WHAT THAT COSTS.

    There is no motor-current bound, no overspeed bound, no cycle-time bound and
    nothing watching `axis_is_armed`. The speeds a normal move reaches and the
    time a normal cycle takes have not been measured here, and a bound carrying a
    figure borrowed from another axis is not a smaller safety net than none - it
    is a number that looks measured.

    THE COST IS SPECIFIC AND IT IS ACCEPTED. This axis is gravity-loaded and has
    no brake, so a drive that disarms itself lets the load run positive until it
    reaches the ground, and NOTHING IN THIS RULEBOOK NOTICES. Bus voltage stays
    nominal, the drive is no longer drawing, the thermocouples stay cool, and the
    run records the drop and reports a pass. The fall is bounded by the ground
    rather than by anything here. Closing the gap needs measurements a shakedown
    run would produce - `cycle_time_s` is published from cycle one for exactly
    that - plus, needing no measurement at all, an `axis_is_armed` bound gated on
    a flag the test publishes while it expects to be driving.

- overvoltage_bound: board_vbus_voltage > 52V, fatal, no persistence.

  THE ONLY OVERVOLTAGE PROTECTION ON THIS STAND BEYOND THE BRAKE RESISTOR: this
  board has no dc_bus_overvoltage_trip_level set, so there is no firmware trip
  underneath this bound and nothing else acts if it is passed. Every cycle lowers
  a gravity load onto a bench supply that cannot sink, which is what pushes the
  bus up in the first place - see XdeployTestbed's ODRIVE_MAX_REGEN_CURRENT_A.

- fet_overtemperature_bound: motor_fet_thermistor_temperature > 80C, fatal,
  debounced 5s. A board figure rather than a stand figure, set below the point at
  which this ODrive family begins derating its own current limit - past that a
  move gets less current than the test believes it asked for. Above the
  teststeps' FET_WAIT_C, where a cycle stops moving and waits, so reaching this
  one means the stand went on heating while already being held back.

- undervoltage_bound: board_vbus_voltage < 10.5V, fatal, no persistence -
  trusted instantaneously rather than debounced. Mirrors the ODrive's own
  dc_bus_undervoltage_trip_level, so the run ends on the same threshold the
  firmware acts on.

  Read at the ODrive because there is nowhere else to read it: the bus comes from
  a bench supply outside this framework, with no client, no setpoint and no
  telemetry of its own. What the drive measures is the only account of the bus
  this stand has.

  THIS BOUND IS ALSO WHAT MAKES A COLD START FAIL, AND THAT IS WHY ManualTest
  ASKS BEFORE IT STARTS THE RUNNER. On a de-energized stand the first frame
  carrying `board_vbus_voltage` near zero ends the run, since the bound is fatal,
  ungated and undebounced - confirmed on zdrive at 0.020 V, 0.01 s into
  main_execution, and written up in AI/Mytest.md's known issues. On zdrive a test
  could energize the bus itself; here nobody can but a person at the supply, so
  the prompt is not a workaround for the bound, it is the only way the condition
  the bound describes can be met.

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

TEST_NAMES lists every concrete xdeploy TestCase.TEST_NAME that starts a runner
against this Rulebook - add a new test's TEST_NAME here when it should be checked
against these same safety bounds too. Lives here rather than on the TestCase to
avoid a circular import (see ydrive's and zdrive's rulebooks for the same
pattern).
"""
from __future__ import annotations

from asimov.rulebook import Bound, Rulebook

MIN_BUS_VOLTAGE_V = 10.5
"""Fatal floor on the DC bus measured at the ODrive - the same value as the
board's dc_bus_undervoltage_trip_level, so the run ends on the threshold the
firmware acts on rather than on a threshold of this stand's invention.

A DEVICE FIGURE, NOT A STAND FIGURE, which is what makes it usable here when
nothing else about this stand has been measured: it is true of the board
whatever the bench supply is set to."""

MAX_BUS_VOLTAGE_V = 52.0
"""Fatal ceiling on the DC bus measured at the drive.

The same figure zdrive bounds its bus at. Unlike zdrive's it has no firmware trip
beneath it - this board has no overvoltage trip level set - so nothing acts on an
overvoltage except the brake resistor and this bound ending the run."""

MAX_FET_TEMPERATURE_C = 80.0
"""Fatal ceiling on the ODrive's own inverter FET thermistor, below the
temperature at which this board family starts derating its current limit."""

FET_PERSISTENCE_S = 5.0
"""How long the FET must stay above MAX_FET_TEMPERATURE_C before the run stops."""

MAX_TEMPERATURE_C = 70.0
"""Fatal ceiling on every live thermocouple channel.

INHERITED FROM zdrive AND UNMEASURED ON THIS STAND. It is a plausible limit for
a winding or a housing rather than a figure derived from what this mechanism is
made of or rated for, and the first xdeploy run that reaches it should be treated
as a question about this number as much as about the hardware."""

TC_PERSISTENCE_S = 5.0
"""How long a channel must stay above MAX_TEMPERATURE_C before the run stops.
A thermocouple spikes from electrical noise as well as from heat. Affordable on
a thermal limit because thermal mass is slow; the same dial would be wrong on a
bus bound."""

TC_DROPOUT_GRACE_S = 10.0
"""How long a bounded channel may read FAULT before the run stops. This DAQ drops
the odd sample, and one dropped sample is not a lost sensor; a thermocouple that
has come out reads FAULT forever."""

LIVE_TC_CHANNELS = (1, 2)
"""Which thermocouple inputs are wired on this stand, and so the only ones
bounded.

CONFIRMED AGAINST THE HARNESS on 2026-09-03: read directly off the DAQ on COM7,
channels 1 and 2 carried 31.3 C and 34.1 C and channels 3-8 all reported FAULT.
Stand configuration, so unplug or move a thermocouple and this has to change with
it. Getting it wrong fails loudly rather than quietly, which is the one mercy: a
bound on an unwired channel is unevaluable and stops the run on its first frame,
naming the channel."""

BASE_XDEPLOY_TEST_NAME = "base_xdeploy_test"
MANUAL_TEST_NAME = "xdeploy_manual_test"
CYCLE_TEST_NAME = "xdeploy_cycle_test"

TEST_NAMES = [
    BASE_XDEPLOY_TEST_NAME,
    MANUAL_TEST_NAME,
    CYCLE_TEST_NAME,
]

XDEPLOY_RULEBOOK = Rulebook(
    name="xdeploy_rulebook",
    test_names=TEST_NAMES,
    bounds=[
        Bound(
            channel="board_vbus_voltage",
            lower=MIN_BUS_VOLTAGE_V,
            name="undervoltage_bound",
            fatal=True,
        ),
        Bound(
            channel="board_vbus_voltage",
            upper=MAX_BUS_VOLTAGE_V,
            name="overvoltage_bound",
            fatal=True,
        ),
        Bound(
            channel="motor_fet_thermistor_temperature",
            upper=MAX_FET_TEMPERATURE_C,
            name="fet_overtemperature_bound",
            fatal=True,
            persistence_s=FET_PERSISTENCE_S,
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
