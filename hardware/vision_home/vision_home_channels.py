"""Declared telemetry/command surface for the vision-home camera - the source
of truth both VisionHomeBackend (real) and MockVisionHomeBackend implement
identically, so a test cannot tell which one it is running against from the
channel list alone.

WHAT THIS DEVICE DOES AND DOES NOT DECIDE. It reports how well the fixture in
front of it matches a taught reference, and nothing else. It does not know the
axis position, does not decide when a match should be believed, and never
commands the drive. Whether to re-reference the axis is the test's call, taken
in the testcase process from `aligned` plus its own `pos_estimate` - which is
what keeps live decision-making in the one process holding the hardware (see
AI/Mytest.md, no feedback loop of downstream results into command).

`camera_backend` is here because on Windows which OpenCV backend actually
delivers frames is neither predictable nor stable across benches, and it is the
first thing worth knowing when a rig that worked yesterday does not - see
camera.py.
"""
from __future__ import annotations

TELEMETRY_CHANNELS = [
    "match_score",  # 0..1 - how well the taught view matches, searched over the whole frame
    "aligned",  # bool - the marker was found AND is at the reference; the only gate on a correction
    "taught",  # bool - whether a reference view exists at all; nothing is aligned without one
    "camera_connected",  # bool - a frame arrived recently
    "camera_backend",  # which OpenCV backend delivered frames (MSMF/DSHOW/AVFOUNDATION/V4L2/auto)
    "camera_source",  # the index or address this driver settled on
    "consecutive_read_failures",  # a lone failed read is normal; a run of them is a lost camera
    "frames_read",  # cumulative, so a stalled camera is visible as a flat line
]

COMMAND_CHANNELS = [
    "teach",  # capture the current frame as the reference view
    "select_best_camera",  # score every camera against the reference view and keep the best
]
