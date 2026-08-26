"""Documents every state channel zdrive's tests publish.

DEFAULT_STATE seeds them (see BaseZdriveTest.pre_test_setup) so they exist in the
stream from frame 1 instead of appearing when a step first computes them.

THIS IS NOT COSMETIC. The telemetry engine fixes a wide file's header from the
first HEADER_SAMPLE_FRAMES it sees and drops any channel that appears later. The
drivers are already publishing before a run's first step runs, so a channel first
written partway through a sequence - `brake_slip_m` is written only after the
hold, tens of seconds in - would be logged once and dropped, and the measurement
the run exists to take would be missing from the recorded file while the run
reported a clean pass.

Bound-status channels ({bound.label}_status, plus test_status) are not listed
here - they are derived from RULEBOOKS, which is already the single source of
truth for bound names.
"""
from __future__ import annotations

from typing import Any, Dict

DEFAULT_STATE: Dict[str, Any] = {
    "brake_engaged": True,
    # True, not None or False: the brake is magnet-applied, so it is holding from
    # the moment the stand comes up and stays that way until a step powers its
    # rail. Present from frame 1 so a recorded run reads as "engaged except while
    # the controller has the load" rather than the channel appearing at the first
    # release.
    "position_target": None,
    "position_origin": None,
    # Where the operator left the load, which every target is relative to. The
    # device is not zeroed, so without this a stored run's absolute positions
    # cannot be interpreted.
    "operator_prompt": None,
    # What the run is waiting for a person to do, or None when it is not waiting.
    "dut_serial_number": None,
    "er_ticket": None,
    "load_lb": None,
    # Answered by the operator before anything is energized, and then carried on
    # every recorded row - so a stored run says which DUT it was, under what load,
    # against which ticket, without anyone keeping a separate note. Published
    # under a channel name rather than a label, so rewording the prompt cannot
    # rename the channel stored runs are keyed by.
    "loaded_brake_holds": 0,
    # Holds where the brake was actually carrying the load, which is the count that
    # means anything for wear. CycleBrakeHoldTest engages the brake twice per cycle -
    # once at the top, where it holds 1000 lb, and once at the bottom, where the load
    # is already resting on its hard stop and the brake is holding nothing. Only the
    # first is counted, and the name says so rather than leaving a reader to work out
    # which of the two a bare "brake_holds" meant.
    "brake_slip_m": 0.0,
    "total_distance_m": 0.0,
    "bottom_dwell_s": 0.0,
    "thermal_waits": 0,
    "fet_temperature_c": 0.0,
    # How far the drive has travelled in all, how long the last bottom dwell
    # actually took, how many 60 s thermal waits it contained, and what the FET read
    # when that was decided. The dwell and the wait count are what let a cycle that
    # cooled down first be told from one that did not: the brake enters the next hold
    # at a different temperature, and slip is compared across them.
    #
    # Seeded 0.0 rather than None because a run records these from frame 1 and the
    # engine drops a channel that first appears later. total_distance_m is derived
    # (sampled every tick from the ODrive's turns_traveled) rather than published by a
    # step, so its seed is what the record holds until the run's cycling begins.
    # How far the load moved while the brake alone held it. None rather than 0.0
    # until a hold has happened, because 0.0 would read as a perfect hold that
    # never took place - and unlike a numeric Bound's channel, nothing bounds
    # this, so None costs nothing here.
    "brake_cycles": 0,
    "brake_speed_m_s": 0.0,
    "stopping_distance_m": 0.0,
    # The last brake-from-speed event: how fast the load was moving when the brake
    # was commanded, and how far it then travelled. In turns/s because that is the
    # unit the trigger is set in, so the number asked for and the number recorded
    # are directly comparable; in millimetres because that is the unit the bound is
    # written in and what an operator measures with.
    #
    # Seeded 0.0 rather than None, as brake_slip_m now is too: zdrive_rulebook puts a
    # numeric bound on both, a numeric bound on a channel carrying no value is
    # unevaluable, and unevaluable stops a run - so None would abort every run on its
    # first frame, before anything moved.
    #
    # brake_slip_m WAS None, and the comment here used to contrast the two. It changed
    # when brake_slip_bound was added: a channel stops being allowed to say "nothing
    # has happened yet" the moment something bounds it. The cost, both times, is that
    # rows before the first event read as a measurement of zero rather than as an
    # absence - which for slip is at least the truthful answer, since a brake that has
    # not been asked to hold anything has not slipped.
}
