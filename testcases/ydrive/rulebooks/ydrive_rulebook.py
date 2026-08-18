"""Evaluation Rulebook for ydrive: two fatal safety-net bounds on
board-level DC bus channels, checked regardless of what a given test's
main_execution actually does:

- overcurrent_bound: board_ibus > 20A for 10s continuous (persistence_s
  debounces a brief spike, e.g. during a fast direction reversal).

  STILL UNFIREABLE ON THIS STAND, and the arithmetic is the reason
  rather than the number: 20 A is the CPX400DP's absolute ceiling, and it
  is only reachable below about 21 V. On the 48 V this rail runs at, the
  supply's 420 W envelope caps the output at 8.75 A - so an overdraw
  makes the output go *unregulated* and the bus voltage sag long before
  board_ibus reaches 20 A, let alone stays there for 10s. A limit that
  can actually engage on this rail has to sit under 8.75 A.

  It is left in place because it costs nothing and stays correct if the
  bus is ever fed by something bigger - but the channel that actually
  reports the limit being hit is the supply's own in_power_limit_2, and
  undervoltage_bound below is what now catches the sag. Adding a bound on
  in_power_limit_2 is an open action (see AI/Mytest.md); it needs a
  decision about whether hitting the envelope should be fatal or merely
  recorded.
- undervoltage_bound: board_vbus_voltage < 10.5V, no persistence -
  trusted instantaneously rather than debounced.
- overtemperature_bound_<n>: temperature_<n>_c > 80C on every LIVE
  thermocouple channel, fatal, debounced 5s.

  ONLY THE LIVE CHANNELS ARE BOUNDED. The DAQ streams eight channels and
  reports FAULT for one it cannot read, which the driver publishes as
  temperature_<n>_c = None. A numeric bound on a None raises
  UnevaluableBoundError, and the runner treats a bound it cannot evaluate
  as a stop - correctly, since a bound that was skipped is not a bound
  that passed. So bounding an unconnected channel would abort every run
  on its first frame. LIVE_TC_CHANNELS below is which ones are wired,
  and it is stand configuration: unplug one and this list has to change
  with it.

  That cuts the other way too, deliberately. If a bounded channel goes
  open mid-run, its bound becomes unevaluable and the run stops. That is
  the wanted behaviour for a thermal limit - losing the sensor you were
  relying on is not a reason to keep driving - but it means a flaky
  thermocouple connection ends runs.

  Debounced by 5s, so a channel has to read above 80C continuously for
  that long before the run stops - about 45 consecutive samples at the
  DAQ's 9 Hz. A thermocouple spikes from electrical noise as well as from
  heat, and this stand switches 48 V a few feet from the harness. The
  trade is explicit: a genuine overtemperature keeps being driven for up
  to 5s. That is affordable because thermal mass is slow - nothing here
  goes from safe to damaged inside 5s - which is exactly what makes it
  the wrong dial for the undervoltage bound, where a sagging bus is a
  different kind of event.

  Debounce delays a violation only; clearing is immediate, and a single
  sample back under 80C resets the clock rather than accumulating (see
  Rulebook's asymmetric-debounce note). So an intermittently spiking
  channel never trips it - which is the point, and also why a channel
  that spikes constantly hides a real rise until it stops spiking.

  THE DEBOUNCE DOES NOT COVER AN OPEN CHANNEL, and a separate window
  does. A faulted channel is unevaluable rather than violated, which
  stops a run - so these bounds carry TC_DROPOUT_GRACE_S, ten seconds,
  because this DAQ drops the odd sample and one dropped sample is not a
  lost sensor. Past that window it still stops the run: a thermocouple
  that has come out reads FAULT forever.

- stopping_distance_bound: stopping_distance_m > 2.0, fatal, no
  persistence. Not a hardware channel: BrakeEnduranceTest publishes each
  brake event's stopping distance as run state, and the runner merges
  published state into what it evaluates. So a brake that no longer
  stops the load in 2 m aborts the run through the same path as any
  hardware bound, and the value that did it lands in the verdict's
  timeline instead of only in a log line.

  Undebounced deliberately, unlike the thermal bounds: this channel is
  not a sampled signal that can spike, it is one number per brake event,
  written once and then held until the next event. One bad stop is the
  event, and debouncing would mean waiting for a second one.

  The channel is seeded 0.0, and has to carry a number rather than no
  value: a numeric bound on a channel carrying None is UNEVALUABLE,
  which stops a run exactly as a faulted thermocouple does. Seeded None,
  this bound aborted every run on its first frame before anything moved.
  The cost of 0.0 is that every row before the first brake event reads as
  a stop in no distance rather than as no stop yet.

TEST_NAMES lists every concrete ydrive TestCase.TEST_NAME that starts
a runner against this Rulebook (today, EnduranceCycleTest and
ManualTest) - add a new test's TEST_NAME here when it should be
checked against these same safety bounds too. Lives here rather than
on the TestCase to avoid a circular import (see example_dut's
rulebooks for the same pattern).
"""
from __future__ import annotations

from testcases.asimov.rulebook import Bound, Rulebook

MAX_BUS_CURRENT_A = 20.0
"""Fatal ceiling on the ODrive's DC bus current. See this module's docstring for
why this cannot engage on a rail fed at 48 V by a 420 W supply."""

BUS_CURRENT_PERSISTENCE_S = 10.0
"""How long board_ibus must stay above MAX_BUS_CURRENT_A before the run stops."""

MAX_STOPPING_DISTANCE_M = 2.0
"""How far the load may travel after the brake is commanded, measured from the
command rather than from when the brake bites - so it includes the coast through
BRAKE_SETTLE_S, which at the brake test's engagement speed is up to 0.18 m of
it."""

MAX_TEMPERATURE_C = 80.0
"""Fatal ceiling for every wired thermocouple."""

TC_DROPOUT_GRACE_S = 10.0
"""How long a thermocouple may report no reading before the run stops.

Ten times the framework default, because this DAQ drops samples: it reports FAULT
for a channel it cannot read that instant, and a wired thermocouple on this stand
has been seen doing that for a single frame in a twelve-minute run. A window this
wide costs ten seconds of thermal supervision in the worst case, which is
affordable on a quantity that moves as slowly as temperature - the bound it guards
already waits five seconds before believing a rise.

What it still catches, and must: a thermocouple pulled out or broken, which reads
FAULT forever rather than for a frame."""

TC_PERSISTENCE_S = 5.0
"""How long a channel must stay above MAX_TEMPERATURE_C before the run stops -
roughly 45 consecutive samples at the DAQ's 9 Hz. See this module's docstring for
why a thermal bound can afford it and the bus bounds cannot."""

LIVE_TC_CHANNELS = (4, 5, 6, 7, 8)
"""Which of the DAQ's eight inputs have a thermocouple on them.

Stand configuration rather than a property of the device: channels 1-3 read
FAULT because nothing is connected to them, and a numeric bound on a channel
reporting no value stops the run (see this module's docstring). Wire another
thermocouple and add its channel here; unplug one and remove it, or every run
will abort on the first frame."""

ENDURANCE_CYCLE_TEST_NAME = "endurance_cycle_test"
MANUAL_TEST_NAME = "manual_test"
BRAKE_ENDURANCE_TEST_NAME = "brake_endurance_test"

TEST_NAMES = [ENDURANCE_CYCLE_TEST_NAME, MANUAL_TEST_NAME, BRAKE_ENDURANCE_TEST_NAME]

YDRIVE_RULEBOOK = Rulebook(
    name="ydrive_rulebook",
    test_names=TEST_NAMES,
    bounds=[
        Bound(
            channel="board_ibus",
            upper=MAX_BUS_CURRENT_A,
            name="overcurrent_bound",
            fatal=True,
            persistence_s=BUS_CURRENT_PERSISTENCE_S,
        ),
        Bound(
            channel="board_vbus_voltage",
            lower=10.5,
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
