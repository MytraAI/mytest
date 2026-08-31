"""Evaluation Rulebook for xdeploy: a deliberately minimal safety net - one
bound at the drive and one per wired thermocouple - checked regardless of what a
given test's main_execution actually does.

THE BOUNDS SPAN TWO DEVICES, and each stream evaluates only the bounds whose
channels it carries. `board_vbus_voltage` is the ODrive's; `temperature_<n>_c`
is the DAQ's. A bound whose channel is absent from a frame returns no result, so
a runner has to be started against both streams or half of this rulebook
silently never runs - see ManualTest.

WHAT THIS RULEBOOK DELIBERATELY DOES NOT BOUND, AND WHAT THAT COSTS.

    There is no motor-current bound and no overspeed bound. Both exist on zdrive
    and both would have to be given numbers, and no number about this stand has
    been measured: the motor's rating, the speeds a normal move reaches, and the
    speed a released load reaches are all unknown here. A bound carrying a figure
    borrowed from another axis is not a smaller safety net than none - it is a
    number that looks measured, and every later reader would take it as one.

    THE COST IS SPECIFIC AND IT IS NOT SMALL: this axis is gravity-loaded and has
    no brake, so a drive that disarms itself leaves the load descending, and
    NOTHING IN THIS RULEBOOK NOTICES. Bus voltage stays nominal, the drive is no
    longer drawing, the thermocouples stay cool, and the run records the fall and
    reports a pass. zdrive carries `overspeed_bound` for exactly this, and its
    docstring is worth reading before this stand runs unattended. Closing this
    gap needs one measurement - the speed a normal move reaches - and the bound
    that follows from it.

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

STAND CONFIGURATION, AND UNCONFIRMED AGAINST THE xdeploy HARNESS - the first two
channels are what zdrive wires, not something checked here. Getting it wrong
fails loudly rather than quietly, which is the one mercy: a bound on an unwired
channel is unevaluable and stops the run on its first frame, naming the channel.
Unplug or move a thermocouple and this has to change with it."""

BASE_XDEPLOY_TEST_NAME = "base_xdeploy_test"
MANUAL_TEST_NAME = "xdeploy_manual_test"

TEST_NAMES = [
    BASE_XDEPLOY_TEST_NAME,
    MANUAL_TEST_NAME,
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
