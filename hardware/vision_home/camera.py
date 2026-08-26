"""Opening a webcam with OpenCV, and keeping it open.

Everything here is hard-won on a Windows test bench. cv2.VideoCapture looks
like one line and is not, so the reasoning is kept with the code rather than
in a commit message.

THE BACKEND MUST BE NAMED, AND ORDERED PER PLATFORM. Left to auto-detect
(cv2.CAP_ANY) OpenCV tries FFMPEG first on several platforms and builds, and
the prebuilt opencv-python-headless wheel is not compiled with libavdevice, so
FFMPEG can never open a live camera at all. That attempt is guaranteed to fail,
costs seconds, and prints a warning that sends people looking in the wrong
place. FFMPEG is excluded deliberately.

  - Windows: MSMF first, DSHOW second. DSHOW is a dead end for some
    camera/driver combinations, refusing to open by plain index at all
    ("backend is generally available but can't be used to capture by index").
    DSHOW stays as a fallback because the reverse - works under DSHOW, not
    MSMF - is also seen in the wild.
  - macOS: AVFOUNDATION. Linux: V4L2.

isOpened() IS NOT PROOF. A backend can report a device open and then never
deliver a frame. The only test that means anything is reading one, so each
backend gets OPEN_READ_ATTEMPTS tries before it is rejected: the first read
after open can legitimately fail while a sensor is still waking up. Between
backends there is a short settle, because the OS and driver do not release
instantly after a failed attempt.

AN UNSPECIFIED VideoCapture IS KEPT AS A LAST RESORT even though it risks the
FFMPEG probe above. OpenCV's own internal backend selection has been seen to
succeed on a device where every explicit attempt failed, and losing that is a
worse regression than an ugly warning.

A DEVICE INDEX IS NOT A CONSTANT. Numbering is per-machine and per-OS, and a
laptop's built-in camera or a docking station can push a USB webcam to an
unpredictable and sometimes high index. Windows is the worst of them. So the
source is configuration, and it accepts either an integer index or a string -
an RTSP/HTTP URL, or a backend-specific device path - passed through untouched.

A FAILED READ IS NOT A DISCONNECT. MSMF in particular drops the occasional
frame in normal operation. Only MAX_CONSECUTIVE_READ_FAILURES in a row means
the camera is actually gone; a lone hiccup is skipped and retried.

release() IS NOT IMMEDIATE, especially on Windows. Anything that reopens or
probes the same device has to wait after releasing, or it opens a device the OS
still thinks is busy.

macOS asks for camera permission the first time a process calls
VideoCapture, once, through System Settings - the same prompt any other app
gets. Windows does not gate a desktop process this way.
"""
from __future__ import annotations

import logging
import sys
import time
from typing import NamedTuple, Optional, Tuple, Union

logger = logging.getLogger(__name__)

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:  # the driver reports this rather than failing at import
    CV2_AVAILABLE = False

Source = Union[int, str]

OPEN_READ_ATTEMPTS = 3
"""Reads a backend gets to produce one real frame before it is rejected. More
than one, because the first read after open can fail while a sensor wakes."""

OPEN_READ_DELAY_S = 0.2
"""Wait between those attempts."""

BACKEND_SETTLE_S = 0.3
"""Wait between two backends' attempts, so the second is not opening a device
the OS has not finished letting go of."""

RELEASE_SETTLE_S = 1.0
"""Wait after release() before reopening or probing the same device - release
is not immediate, and least so on Windows."""

MAX_CONSECUTIVE_READ_FAILURES = 5
"""Failed reads in a row that mean the camera is gone rather than hiccupping."""

BLANK_FRAME_STDDEV = 1.0
"""Pixel spread below which a frame carries no image at all.

A THIRD THING read() RETURNING TRUE DOES NOT PROVE. MSMF in particular opens a
camera and delivers all-black frames when the sensor has not started, and a black
frame is a successful read by every check that matters to OpenCV. It correlates
against anything at exactly 0.000, which reads as "cannot see the marker" and sends
people looking at the mount instead of the driver."""


def is_blank(frame) -> bool:
    """Whether a frame carries no image - see BLANK_FRAME_STDDEV."""
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    return float(grey.std()) < BLANK_FRAME_STDDEV


def resolve_source(source: str) -> Source:
    """A source that parses as an int is a local device index; anything else is
    passed to cv2.VideoCapture untouched (a URL or a device path)."""
    try:
        return int(source)
    except (TypeError, ValueError):
        return source


def candidate_backends() -> list:
    """The backends to try for a local index, best first for this platform.
    FFMPEG is excluded - see this module's docstring."""
    if sys.platform.startswith("win"):
        return [(cv2.CAP_MSMF, "MSMF"), (cv2.CAP_DSHOW, "DSHOW")]
    if sys.platform == "darwin":
        return [(cv2.CAP_AVFOUNDATION, "AVFOUNDATION")]
    return [(cv2.CAP_V4L2, "V4L2")]


def open_capture(source: Source) -> Tuple[Optional["cv2.VideoCapture"], str]:
    """Open `source` and return the capture plus the backend that actually delivered a
    frame, or (None, reason). The backend name is the first thing worth knowing."""
    if not isinstance(source, int):
        cap = cv2.VideoCapture(source)
        return (cap, "direct") if cap.isOpened() else (None, "could not open the address")

    for index, (flag, name) in enumerate(candidate_backends()):
        if index > 0:
            time.sleep(BACKEND_SETTLE_S)
        cap = cv2.VideoCapture(source, flag)
        if cap.isOpened():
            for attempt in range(OPEN_READ_ATTEMPTS):
                ok, frame = cap.read()
                if ok and not is_blank(frame):
                    logger.info("camera %s opened on %s, %dx%d",
                                source, name, frame.shape[1], frame.shape[0])
                    return cap, name
                if ok:
                    logger.warning("camera %s on %s delivered a blank frame", source, name)
                if attempt < OPEN_READ_ATTEMPTS - 1:
                    time.sleep(OPEN_READ_DELAY_S)
            logger.warning("camera %s opened on %s but delivered no usable frame", source, name)
        cap.release()

    # Last resort. This risks OpenCV probing FFMPEG, which this wheel cannot
    # use for a live camera - but its internal selection has been seen to
    # succeed where the explicit attempts above did not.
    cap = cv2.VideoCapture(source)
    if cap.isOpened() and cap.read()[0]:
        logger.warning("camera %s opened only on OpenCV's own backend choice", source)
        return cap, "auto"
    cap.release()
    tried = ", ".join(name for _, name in candidate_backends())
    return None, f"no backend delivered a frame (tried {tried}, then auto)"


RECOGNITION_THRESHOLD = 0.60
"""Default score at which the marker counts as SEEN, wherever it is in frame -
between 0.79 measured for the fixture camera 175 px off the reference and 0.26 for
one facing a room. Answers "can I see it", not "is it at the reference".

A STRIPE-PITCH ERROR IS ACCEPTED: the tape is periodic, so the search can pick the
wrong stripe, and being off by a stripe or two per re-reference is tolerable here
where refusing to detect at all is not."""


class Located(NamedTuple):
    """Where the taught view was found and how well. `dx`/`dy` are pixels from where
    the template was cropped, so zero means the fixture is where it was taught."""

    found: bool
    score: float
    dx: int
    dy: int


def locate_marker(frame, template, threshold: float = RECOGNITION_THRESHOLD) -> Located:
    """Find `template` anywhere in `frame` and say where and how well - a SEARCH, not a
    fixed comparison, so the score says SEEN and the displacement says AT THE REFERENCE."""
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame

    # THE TEMPLATE IS A FRACTION OF A FRAME, NOT A NUMBER OF PIXELS. A view taught
    # through a 1920x1080 camera is 960x540, and matching it against a camera that
    # delivers 640x480 is not a poor match - it does not fit, and scores exactly
    # 0.000 on every candidate, which reads as "no camera can see the marker".
    # Backends disagree about default resolution, so this is the normal case on a
    # second machine rather than an edge one.
    want = (int(grey.shape[1] * ROI_FRAC), int(grey.shape[0] * ROI_FRAC))
    if template.shape[:2][::-1] != want:
        if min(want) < 8:
            return Located(False, 0.0, 0, 0)
        template = cv2.resize(template, want, interpolation=cv2.INTER_AREA)
    th, tw = template.shape[:2]

    surface = cv2.matchTemplate(grey, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, peak = cv2.minMaxLoc(surface)
    taught_x, taught_y = (grey.shape[1] - tw) // 2, (grey.shape[0] - th) // 2
    return Located(
        found=bool(score >= threshold),
        score=float(score),
        dx=int(peak[0] - taught_x),
        dy=int(peak[1] - taught_y),
    )


ROI_FRAC = 0.5
"""Fraction of the frame taken as the template. Half, so it holds the tape AND the
non-repeating hardware moving with it - a caster, a cable, a connector - which is
what makes one alignment unique, since periodic tape alone locates nothing."""


def center_crop(frame):
    """The centre ROI_FRAC of a frame, in greyscale."""
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = grey.shape[:2]
    ch, cw = int(h * ROI_FRAC), int(w * ROI_FRAC)
    top, left = (h - ch) // 2, (w - cw) // 2
    return grey[top:top + ch, left:left + cw]
