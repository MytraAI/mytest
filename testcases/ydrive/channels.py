"""Documents every channel ydrive's tests can see.

TELEMETRY_CHANNELS/COMMAND_CHANNELS re-export hardware/odrive/
odrive_channels.py's own declared lists (the actual source of truth)
so this file documents the full channel surface in one place;
YdriveTestbed.start() imports directly from odrive_channels.py and
positively confirms both against the live driver process.

DEFAULT_STATE seeds test-published state channels (see
BaseYdriveTest.pre_test_setup()) so they exist in the stream from
frame 1 instead of appearing incrementally as steps happen to compute
them. Bound-status channels ({bound.label}_status, plus test_status)
aren't listed here - they're derived from RULEBOOKS instead.
"""
from __future__ import annotations

from typing import Any, Dict

from hardware.odrive.odrive_channels import COMMAND_CHANNELS
from hardware.odrive.odrive_channels import TELEMETRY_CHANNELS as HARDWARE_CHANNELS

DEFAULT_STATE: Dict[str, Any] = {
    "position_target": None,
    "current_step": None,
    "brake_engaged": True,
    # True, not None or False: the brake is magnet-applied, so it is holding from
    # the moment the stand comes up and stays that way until a step powers its
    # rail. Present from frame 1 so a recorded run reads as "engaged except
    # during moves" rather than the channel appearing at the first dwell.
    "dut_serial_number": None,
    "er_ticket": None,
    "load_lb": None,
    # Answered by the operator before anything is energized, and then carried on
    # every recorded row - so a stored run says which DUT it was, under what load,
    # against which ticket, without anyone keeping a separate note. Seeded because
    # the engine fixes a file's header from the first frame and drops channels that
    # appear later; free text, because a load is written on a plate and a ticket is
    # whatever the tracker calls it.
    "operator_prompt": None,
    # What the test is waiting for a person to do, or None when it is waiting for
    # nobody. A recorded run then shows how long the stand sat waiting on an
    # operator, which is otherwise indistinguishable from a hang.
    "position_origin": None,
    # The pos_estimate captured where the operator put the load, in turns. Every
    # position this test commands is relative to it, so a stored run's absolute
    # positions stay interpretable without it.
    "brake_cycles": 0,
    "brake_speed_m_s": 0.0,
    "stopping_distance_m": 0.0,
    # The last brake event's speed at engagement and how far the load then
    # travelled. Published as state rather than only logged, because
    # ydrive_rulebook bounds stopping_distance_m - the runner merges published
    # state into what it evaluates, so a bad stop aborts the run through the same
    # path as any hardware bound and lands in the verdict's timeline.
    #
    # Seeded 0.0, not None: a numeric bound on a channel carrying no value is
    # unevaluable, and unevaluable stops a run - so None here aborted every run on
    # its first frame, before anything moved. The cost is that rows before the
    # first brake event read as a stop in no distance rather than as no stop yet.
    "distance_since_correction_m": 0.0,
    # How far the load has gone since the camera last re-referenced the axis. Rises
    # and keeps rising when a camera is bumped or the turnaround stops reaching the
    # marker, which stops corrections dead - and with nothing bounding drift, nothing
    # else would say so. Metres because that is the unit the brake's clearance is in.
    "camera_selected_by": None,
    # Whether the camera was identified by recognising the committed reference view
    # or taken from configuration. Those are different claims about the marker
    # position: recognised means the fixture was verified to be there, configured
    # means an operator said so. A run that corrected against the wrong reference
    # is only interpretable if the record says which.
    "marker_match_score": 0.0,
    "position_claimed_at_marker": None,
    # What the camera saw and what was taken out because of it. The correction is
    # the load's slip past the motor over one cycle, which no other channel on
    # this stand can see: the encoder is on the motor. Seeded 0.0 rather than
    # None because a run records these from frame 1 and a numeric channel
    # carrying no value is unevaluable.
    "total_distance_m": 0.0,
    "distance_since_brake_m": 0.0,
    # How far the load has travelled in all, and how far since the last brake
    # event.
    #
    # HOW total_distance_m IS ARRIVED AT DEPENDS ON WHICH TEST PUBLISHED IT, so a
    # bound or a report reading it needs test_name too. EnduranceCycleTest computes
    # it from its setpoints and assumes every cycle reaches them;
    # CycleBrakeEnduranceTest reads the odrive's turns_traveled, the driver's own
    # frame-by-frame count of the path, because it accepts overshoot and so does not
    # stop at its setpoints. The driver's channel is the record either way - this one
    # is that count in metres, from the start of this run.
    # distance_since_brake_m has one publisher and is derived from the other two.
    #
    # Seeded for the same reason the operator's answers are: the engine fixes a
    # file's header from the union of its first frames and drops channels that
    # appear later, and neither of these is published until the stand is open and
    # the load has moved.
    "cycle_time_s": 0.0,
    # How long the last full stroke cycle took, published by
    # CycleBrakeEnduranceTest at the end of each one. ydrive_rulebook bounds it -
    # see MAX_CYCLE_TIME_S - because a cycle that slows is how a jam, rising
    # friction or a current-limited axis shows itself on a test built to run
    # unattended for months, and nothing else on this stand reports it.
    #
    # Seeded 0.0 rather than None for the reason stopping_distance_m is: a numeric
    # bound on a channel carrying no value is UNEVALUABLE, which stops a run, so
    # None here would abort every ydrive run on its first frame. Every ydrive test
    # carries the channel; only the cycling one ever writes it, and 0.0 reads as
    # "no cycle completed yet" while satisfying the bound.
}
