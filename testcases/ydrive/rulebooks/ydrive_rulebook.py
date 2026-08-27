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
- undervoltage_bound: board_vbus_voltage < 10.5V, no persistence -
  trusted instantaneously rather than debounced.
- power_envelope_bound: in_power_limit_2 is False, RECORDED not fatal -
  the supply's own report that the motor bus has gone unregulated
  against its 420 W envelope. See IN_POWER_LIMIT_EXPECTED. Its channel
  is the supply's, so a test that does not hand runner.start() the
  supply's stream evaluates it against nothing.
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
"""Fatal ceiling on the ODrive's DC bus current.

80% of the highest draw measured on this stand: a 1800 lb cycling run peaked at
14.97 A, against a median of 5.3 A and a p95 of 9.9 A. Set from what the stand
does rather than from what the supply can deliver, so it sits inside the duty
rather than above everything the rail can reach.

Signed, not magnitude: regen ran to -7.74 A on the same run and an upper bound
ignores it, which is right - current flowing back into the supply is not the
failure this guards.

WHETHER IT CAN ENGAGE IS STILL OPEN. Sustaining this needs more than the supply's
420 W envelope allows at 48 V, which caps a steady draw at 8.75 A, so a long
enough overdraw sags the rail and undervoltage_bound catches it first. What
reaching 14.97 A at all shows is that bus capacitance covers brief peaks; whether
one can be held for BUS_CURRENT_PERSISTENCE_S is unmeasured."""

BUS_CURRENT_PERSISTENCE_S = 42.0
"""How long board_ibus must stay above MAX_BUS_CURRENT_A before the run stops.

150% of the longest stroke cycle measured on this stand - 27.8 s at 1800 lb,
giving 41.7 s, rounded up - so a whole cycle of normal duty, peaks and all, cannot
trip it. Only a draw that outlasts the motion producing it can.

Cleared the moment the current drops back, so this is a continuous stretch and
not a total. The same run's longest continuous stretch above MAX_BUS_CURRENT_A
was 0.19 s, against 0.32% of frames above it at all."""

MAX_MOTOR_CURRENT_A = 17.0
"""Motor-phase current, either direction, above which the axis is doing something
other than moving the load.

NOT A HEADROOM BOUND, and this is why the number looks wrong. Normal duty at 1800 lb
sits ABOVE it: |Iq| over a 2169 m run had a median of 17.84 A and a p95 of 18.19 A
against an 18.0 A soft max, with 65% of frames past 17 A. What separates duty from
trouble here is not the height of the current but how long it is held - so the whole
bound is really MOTOR_CURRENT_PERSISTENCE_S, and this is just the floor above which
the clock is allowed to run.

Signed both ways, because Iq's sign is the direction of travel and a stall is a stall
going either way - the same run ran -18.79 A to +18.90 A. Magnitude, in effect, but
expressed as two limits because that is what the evaluator compares.

What it catches: an axis pushing something that will not move. The 2026-08-25 14:23
run drove into a mechanical stop and held 18.0 A at zero velocity with -3.44 Nm for
the 19 s until a person stopped it. Nothing in the rulebook or the test noticed."""

MOTOR_CURRENT_PERSISTENCE_S = 21.0
"""How long |motor_foc_iq_measured| must stay above MAX_MOTOR_CURRENT_A before the
run stops.

150% of one leg of the stroke - 14.0 s median over 180 legs at 1800 lb, giving 21.0 s
- so no single leg of normal duty, current held the whole way, can trip it. Only a
current that outlasts the motion producing it can.

The measured margin is better than that ratio suggests. Cleared the moment the
current drops back, so this is a continuous stretch and not a total, and the
turnaround at each end of the stroke breaks the stretch: the same run's longest
continuous stretch above 17 A was 9.58 s, less than half of this. A stall does not
get that reprieve, which is the whole distinction being drawn."""

MAX_DISTANCE_SINCE_CORRECTION_M = 1000.0
"""How far the load may travel without the camera re-referencing the axis.

NOT A BOUND ON DRIFT, which this test does not measure. It bounds how long the thing
that REMOVES the drift may go on not working. A bumped camera, a turnaround that stops
reaching the marker, a lens that fogs: corrections stop, the load resumes walking
exactly as it did before, and nothing else on this stand can see it - the encoder is on
the motor.

A first cut, deliberately loose. Corrections land on most cycles, and a cycle covers
24.3 m, so this is about 41 consecutive cycles of seeing nothing: an occasional miss
cannot reach it and a camera that has stopped working entirely gets there in under
20 minutes.

Sized by what the clearance can absorb rather than by what is normal. Slip runs about
305 mm per km against a floor mark, so 1000 m is roughly 0.3 m of uncorrected walk
against the 0.59 m between the measured overshoot peak and the mechanical stop. Twice
this would spend all of it, and what that looks like is the 2026-08-25 14:23 run:
1800 lb into a hard stop at the current limit.

No persistence, because the channel cannot spike - it rises monotonically between
corrections and is reset to zero by one."""

IN_POWER_LIMIT_EXPECTED = False
"""What in_power_limit_2 should read: the motor bus inside the supply's power
envelope.

RECORDED, NOT FATAL. This is the channel that actually reports the limit
MAX_BUS_CURRENT_A cannot reach - at 48 V the supply's 420 W envelope caps a steady
draw at 8.75 A, and past it the output goes unregulated and the rail sags rather
than the current climbing. Whether that should end a run is not yet decided, and
deciding it needs to know how often the stand does it: a non-fatal bound publishes
in_power_limit_2_status and puts every transition on the run's timeline, which is
the measurement that answers it. undervoltage_bound is still what stops the run if
the sag gets deep enough to matter.

Not debounced. Across the 5350 cycling frames measured at 1800 lb this never went
true, so there is no flapping to suppress and every hit is worth seeing.

THE CHANNEL ITSELF IS MARKED UNVERIFIED in cpx400dp_channels.py - it is bit 4 of
LSR2, and nothing has confirmed the bit means what the manual says. A bound that is
only recorded is the right place to find that out.

The supply's stream has to be one of the streams a test hands runner.start(), or
this bound is evaluated against no frames and reports a clean pass forever."""

MAX_STOPPING_DISTANCE_M = 3.25
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

LIVE_TC_CHANNELS = (5, 6, 7, 8)
"""Which of the DAQ's eight inputs have a thermocouple on them.

Stand configuration rather than a property of the device: channels 1-4 read
FAULT because nothing is connected to them, and a numeric bound on a channel
reporting no value stops the run once TC_DROPOUT_GRACE_S has passed (see this
module's docstring). Wire another thermocouple and add its channel here; unplug
one and remove it, or the run ends inside the first fifteen seconds."""

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
            lower=10.5,
            name="undervoltage_bound",
            fatal=True,
        ),
        Bound(
            channel="in_power_limit_2",
            expected=IN_POWER_LIMIT_EXPECTED,
            name="power_envelope_bound",
        ),
        Bound(
            channel="distance_since_correction_m",
            upper=MAX_DISTANCE_SINCE_CORRECTION_M,
            name="marker_correction_bound",
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
