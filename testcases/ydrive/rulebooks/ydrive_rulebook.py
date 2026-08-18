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

  THE DEBOUNCE DOES NOT COVER AN OPEN CHANNEL. A faulted channel is
  unevaluable, not violated, and that stops the run on the first frame -
  no 5s of grace. Losing the sensor is a different failure from being
  too hot, and it is not one that waiting improves.

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

MAX_TEMPERATURE_C = 80.0
"""Fatal ceiling for every wired thermocouple."""

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

TEST_NAMES = [ENDURANCE_CYCLE_TEST_NAME, MANUAL_TEST_NAME]

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
        *[
            Bound(
                channel=f"temperature_{n}_c",
                upper=MAX_TEMPERATURE_C,
                name=f"overtemperature_bound_{n}",
                fatal=True,
                persistence_s=TC_PERSISTENCE_S,
            )
            for n in LIVE_TC_CHANNELS
        ],
    ],
)
