"""Evaluation Rulebook for ydrive: fatal safety-net bounds on board-level
DC bus and motor channels, checked regardless of what a given test's
main_execution actually does:

- overcurrent_bound: board_ibus > 12A for 42s continuous (persistence_s
  debounces a brief spike, e.g. during a fast direction reversal).

  Both numbers come off a measured run rather than off a datasheet -
  80% of the highest draw seen and 150% of the longest stroke cycle. See
  MAX_BUS_CURRENT_A and BUS_CURRENT_PERSISTENCE_S for the measurements.

  WHETHER IT CAN ENGAGE IS UNMEASURED. At 48 V the supply's 420 W
  envelope caps a steady draw at 8.75 A, so an overdraw held long enough
  to satisfy the debounce makes the output go *unregulated* and the bus
  sag, which undervoltage_bound catches first. Bus capacitance does cover
  brief peaks - the run this is sized from reached 14.97 A - but nothing
  says a peak can be held for 42 s. The channel that reports the envelope
  being hit is the supply's own in_power_limit_2, and a bound on it is an
  open action (see AI/Mytest.md); it needs a decision about whether
  hitting the envelope should be fatal or merely recorded.
- motor_current_bound: |motor_foc_iq_measured| > 17A for 21s continuous,
  fatal. A STALL DETECTOR, not a headroom bound: normal duty at 1800 lb
  sits above 17 A for 65% of frames, and what separates it from an axis
  pushing a hard stop is that the turnaround breaks the stretch every
  leg. 150% of one leg (14.0 s median) against a measured worst healthy
  stretch of 9.58 s. See MAX_MOTOR_CURRENT_A.
- marker_correction_bound: distance_since_correction_m > 1000 m, fatal. Not
  a bound on drift - a bound on how long the mechanism that removes it may go
  on not working, since nothing else on this stand can see the load slip past
  the motor. See MAX_DISTANCE_SINCE_CORRECTION_M.
- undervoltage_bound: board_vbus_voltage < 33.43V, fatal, no persistence -
  trusted instantaneously rather than debounced. 90% of the lowest reading
  measured with the bus up. See MIN_BUS_VOLTAGE_V, including why it is measured
  over energised frames only and why it replaced the drive's own 10.5 V trip.
- overvoltage_bound: board_vbus_voltage > 55.39V, fatal - 110% of the highest
  measured, and below the drive's own 64 V trip so the rulebook ends the run
  first. The rail rises on regen, which nothing bounded before. See
  MAX_BUS_VOLTAGE_V.
- in_power_limit_2 is no longer bounded. It was recorded-not-fatal to find out
  how often this stand leaves the supply's 420 W envelope; two 1800 lb runs
  answered ~45-49 brief excursions an hour, the longest 43 ms, which made every
  healthy run record FAIL for a direction reversal. Still recorded in the
  cpx400dp stream, just no longer deciding pass/fail - see the note above
  MIN_BUS_VOLTAGE_V.
- overtemperature_bound_<n>: temperature_<n>_c > 80C on every LIVE
  thermocouple channel, fatal, debounced 5s.

  ONLY THE LIVE CHANNELS ARE BOUNDED. The DAQ streams eight channels and
  reports FAULT for one it cannot read, which the driver publishes as
  temperature_<n>_c = None. A numeric bound on a None raises
  UnevaluableBoundError, and the runner treats a bound it cannot evaluate
  as a stop - correctly, since a bound that was skipped is not a bound
  that passed. So bounding an unconnected channel ends the run once
  TC_DROPOUT_GRACE_S has passed. LIVE_TC_CHANNELS below is which are wired,
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

- cycle_time_bound: cycle_time_s > 34s, fatal, no persistence. Not a hardware
  channel: CycleBrakeEnduranceTest publishes each completed cycle's duration as
  run state. A cycle that slows is how a jam or a current-limited axis first
  shows itself, and CYCLE_VELOCITY_TOLERANCE is wide enough to hide it. Sized
  ~19% above the slowest of 308 measured cycles - see MAX_CYCLE_TIME_S, which
  also says why it cannot catch a cycle that never finishes.
- stopping_distance_bound: stopping_distance_m > 3.25, fatal, no
  persistence. Not a hardware channel: brake_from_speed() publishes each
  brake event's stopping distance as run state, and the runner merges
  published state into what it evaluates. So a brake that no longer
  stops the load in 3.25 m aborts the run through the same path as any
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
a runner against this Rulebook - add a new test's TEST_NAME here when it should be
checked against these same safety bounds too. Lives here rather than
on the TestCase to avoid a circular import (see example_dut's
rulebooks for the same pattern).
"""
from __future__ import annotations

from asimov.rulebook import Bound, Rulebook

MAX_BUS_CURRENT_A = 12.0
"""Fatal ceiling on DC bus current. 80% of the highest draw measured at 1800 lb
(14.97 A peak); signed, so regen does not trip it."""

BUS_CURRENT_PERSISTENCE_S = 42.0
"""Debounce for MAX_BUS_CURRENT_A: 150% of the longest measured stroke cycle, so
a whole cycle of normal duty cannot trip it."""

MAX_MOTOR_CURRENT_A = 17.0
"""Fatal phase-current limit, bounded in both directions. A STALL DETECTOR, not
headroom: normal 1800 lb duty sits above this for 65% of frames."""

MOTOR_CURRENT_PERSISTENCE_S = 21.0
"""Debounce for MAX_MOTOR_CURRENT_A: 150% of one leg (14.0 s median), against a
measured worst healthy stretch of 9.58 s."""

MAX_DISTANCE_SINCE_CORRECTION_M = 1000.0
"""Fatal ceiling on travel since the camera last re-referenced the axis. Bounds
how long the correction may go on not working, not drift itself."""

MIN_BUS_VOLTAGE_V = 33.43
"""Fatal floor on the motor bus: 90% of the lowest reading measured with the bus
up (37.14 V). An 11% margin, undebounced - one sag ends the run."""

MAX_BUS_VOLTAGE_V = 55.39
"""Fatal ceiling on the motor bus: 110% of the highest measured (50.36 V, the
rail rises on regen). Below the drive's own 64 V trip, so this fires first."""

MAX_CYCLE_TIME_S = 34.0
"""Fatal ceiling on one completed stroke cycle: ~19% above the slowest of 308
measured (28.59 s). Catches a slow cycle, not a hung one."""

MAX_STOPPING_DISTANCE_M = 3.25
"""Fatal ceiling on brake stopping distance - the clearance above the top of the
stroke."""

MAX_TEMPERATURE_C = 80.0
"""Fatal thermal ceiling, applied to every wired thermocouple."""

TC_DROPOUT_GRACE_S = 10.0
"""How long a thermocouple may report no value before that stops the run. This
DAQ drops the odd sample; a channel that has come out reads FAULT forever."""

TC_PERSISTENCE_S = 5.0
"""Debounce for MAX_TEMPERATURE_C. Thermocouples spike from electrical noise, and
thermal mass is slow enough that 5 s of genuine overtemperature is affordable."""

LIVE_TC_CHANNELS = (5, 6, 7, 8)
"""Which thermocouples are wired. STAND CONFIGURATION: bounding an unconnected
channel reads None, which is unevaluable, which stops the run."""

ENDURANCE_CYCLE_TEST_NAME = "endurance_cycle_test"
MANUAL_TEST_NAME = "manual_test"
BRAKE_ENDURANCE_TEST_NAME = "brake_endurance_test"
CYCLE_BRAKE_ENDURANCE_TEST_NAME = "cycle_brake_endurance_test"

TEST_NAMES = [
    ENDURANCE_CYCLE_TEST_NAME,
    MANUAL_TEST_NAME,
    BRAKE_ENDURANCE_TEST_NAME,
    CYCLE_BRAKE_ENDURANCE_TEST_NAME,
]

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
            channel="motor_foc_iq_measured",
            upper=MAX_MOTOR_CURRENT_A,
            lower=-MAX_MOTOR_CURRENT_A,
            name="motor_current_bound",
            fatal=True,
            persistence_s=MOTOR_CURRENT_PERSISTENCE_S,
        ),
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
            channel="distance_since_correction_m",
            upper=MAX_DISTANCE_SINCE_CORRECTION_M,
            name="marker_correction_bound",
            fatal=True,
        ),
        Bound(
            channel="cycle_time_s",
            upper=MAX_CYCLE_TIME_S,
            name="cycle_time_bound",
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
