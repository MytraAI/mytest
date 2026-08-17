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
}
