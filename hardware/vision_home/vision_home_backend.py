"""Camera backend: watches a fixed webcam for a taught view of the moving
fixture, and publishes how well the live view matches it.

A background thread owns the capture and keeps the newest frame; stream_samples
scores that frame. Threaded rather than awaited because cv2.read() blocks for a
frame period and there is no async OpenCV - the same reason OdriveBackend wraps
its blocking calls in asyncio.to_thread.

THE READER THREAD MUST NOT DIE. It runs unsupervised for the life of the
process, so every iteration is wrapped: an unhandled OpenCV or driver error -
exactly what this driver exists to tolerate - would otherwise silently disable
the feature until someone restarts the run, hours later. It recovers by marking
itself disconnected and reopening.

TEACHING IS MEANT TO HAPPEN ONCE. The reference view ships WITH the driver
(PACKAGED_TEMPLATE), so a run starts already knowing what the marker looks like
and nobody has to park the fixture and teach again. A `teach` at runtime still
overrides it, taking effect immediately and persisting to the bench path, which
is what a re-teach after moving something is for.

THE COMMITTED DEFAULT IS ONLY TRUE WHILE THE CAMERA STAYS PUT. It is a picture
of one camera on one mount looking at one fixture, so moving any of the three
invalidates it for everyone using the repository, silently - the match score
simply stops reaching the threshold. Regenerate it with
`python -m hardware.vision_home.main --teach-default <turns>` and commit the
result.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from hardware.backend import HardwareBackend, HardwareError
from protocol.wire import DEVICE_VISION_HOME

from . import camera
from .vision_home_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 0.1
"""Sleep between published frames. The camera runs at its own rate in the reader
thread; this is how often the newest frame is scored, and 10 Hz is well inside
what a template match costs."""

ALIGN_TOLERANCE_FRAC = 0.031
"""How far from the reference the marker may sit and still count as aligned, as a
fraction of frame width.

DISPLACEMENT, NOT SCORE, decides that: locate_marker() searches the whole frame, so
a good score only means the marker was seen. A fraction rather than a pixel count
because backends disagree about resolution - 60 px of a 1920-wide frame is three
times the physical distance at 640 wide, and this window is not a preference.

IT IS THE RESIDUAL ERROR OF EVERY CORRECTION. A correction snaps pos_estimate to
MARKER_POSITION rather than shifting it by the measured offset, so whenever one
fires the fixture may be this far from where the axis is then told it is. Tighter
and corrections are skipped, which drift makes silent; looser and each one leaves
more behind. Both go away once a pixels-to-turns scale lets the measured offset be
used directly."""

STALE_FRAME_S = 2.0
"""How old the newest frame may be before the camera counts as disconnected."""

FRAME_WAIT_S = 10.0
"""How long a command may wait for the reader to have a frame in hand.

Selecting a camera releases the device to scan the others, so for a moment after it
there is no frame at all - and the next command is a teach, which cannot work
without one. Anything that needs a frame waits rather than failing on a gap it
caused itself."""


def _await_frame(backend, timeout_s: Optional[float] = None) -> bool:
    """Block until the reader has a frame, or give up. See FRAME_WAIT_S.

    The default is read at call time, not bound as an argument default: a default
    argument freezes the constant at import, so a caller overriding FRAME_WAIT_S
    would silently get the original."""
    deadline = time.time() + (FRAME_WAIT_S if timeout_s is None else timeout_s)
    while time.time() < deadline:
        with backend._lock:
            if backend._frame is not None:
                return True
        time.sleep(0.1)
    return False

PACKAGED_TEMPLATE = Path(__file__).parent / "reference_template.png"
"""The reference view that ships with the driver, and its position in
reference_template.json beside it. Loaded when no bench template overrides it,
so teaching is a thing that happened once rather than a step in every run."""


class VisionHomeBackend(HardwareBackend):
    """A webcam, a taught template, and a match score."""

    device = DEVICE_VISION_HOME
    sample_interval_s = SAMPLE_INTERVAL_S

    def __init__(self, camera_source: str = "0", template_path: Optional[Path] = None):
        self._source_str = camera_source
        self._template_path = Path(template_path) if template_path else None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Reader-thread state, all under _lock.
        self._frame = None
        self._frame_t = 0.0
        self._backend_name = ""
        self._failures = 0
        self._frames_read = 0
        self._reopen = threading.Event()
        self._pause = threading.Event()
        # Template state.
        self._template = None

    # --- universal core ----------------------------------------------------

    async def connect(self) -> None:
        """Start the reader and confirm a frame actually arrives, since isOpened() proves
        nothing - see camera.py. Raises naming the backends it tried if none comes."""
        if not camera.CV2_AVAILABLE:
            raise HardwareError(
                "opencv is not installed - `pip install opencv-python-headless` "
                "(headless deliberately: a test bench has no display and the GUI "
                "build drags in libraries it cannot use)"
            )
        self._load_template()
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader_loop, daemon=True, name="vision-home-reader")
        self._thread.start()
        deadline = time.time() + 10.0
        while time.time() < deadline:
            with self._lock:
                if self._frame is not None:
                    logger.info("camera %s streaming on %s", self._source_str, self._backend_name)
                    return
            await asyncio.sleep(0.2)
        raise HardwareError(f"no frame from camera {self._source_str!r} within 10s")

    async def disconnect(self) -> None:
        """Stop the reader. Tolerates a thread that is already gone, because
        this runs on the teardown path."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    async def get_status(self) -> dict:
        with self._lock:
            return {
                "camera_source": self._source_str,
                "camera_backend": self._backend_name,
                "connected": self._frame is not None,
                "taught": self._template is not None,
                "frames_read": self._frames_read,
            }

    async def stream_samples(self) -> AsyncIterator[dict]:
        while not self._stop.is_set():
            yield self._sample()
            await asyncio.sleep(SAMPLE_INTERVAL_S)

    async def list_actions(self) -> List[str]:
        return list(COMMAND_CHANNELS)

    async def execute(self, action: str, **params: Any) -> Any:
        if action == "teach":
            return await asyncio.to_thread(self._teach)
        if action == "select_best_camera":
            return await asyncio.to_thread(self._select_best_camera, int(params.get("max_index", 6)))
        raise HardwareError(f"unknown action: {action}")

    # --- the measurement ---------------------------------------------------

    def _sample(self) -> Dict[str, Any]:
        with self._lock:
            frame, frame_t, template = self._frame, self._frame_t, self._template
            backend_name, failures, frames_read = self._backend_name, self._failures, self._frames_read
        fresh = frame is not None and (time.time() - frame_t) <= STALE_FRAME_S
        located = (camera.locate_marker(frame, template)
                   if (fresh and template is not None) else camera.Located(False, 0.0, 0, 0))
        tolerance_px = int(frame.shape[1] * ALIGN_TOLERANCE_FRAC) if frame is not None else 0
        return {
            "match_score": located.score,
            # Seen AND at the reference. Two questions, and the displacement answers
            # the second - a search scores well wherever the marker is.
            "aligned": bool(located.found and abs(located.dx) <= tolerance_px),
            "taught": template is not None,
            "camera_connected": fresh,
            "camera_backend": backend_name,
            "camera_source": self._source_str,
            "consecutive_read_failures": failures,
            "frames_read": frames_read,
        }

    def _select_best_camera(self, max_index: int) -> dict:
        """Score every camera against the reference view and keep the best, refusing rather
        than ranking if none clears the threshold. Needs the fixture in view - see camera.py."""
        if self._template is None:
            raise HardwareError("no reference view to select a camera against")
        self._pause.set()
        try:
            time.sleep(camera.RELEASE_SETTLE_S)
            scored = []
            for index in range(max_index):
                cap, name = camera.open_capture(index)
                if cap is None:
                    continue  # nothing opened, so nothing to wait for releasing
                frame = None
                for _ in range(3):  # let exposure settle before scoring
                    ok, candidate = cap.read()
                    if ok:
                        frame = candidate
                    time.sleep(0.1)
                cap.release()
                time.sleep(camera.RELEASE_SETTLE_S)
                if frame is not None:
                    # Searched, not compared: the fixture need only be IN VIEW for a
                    # camera to be identifiable, not parked to the pixel.
                    scored.append((
                        camera.locate_marker(frame, self._template).score,
                        index,
                        f"{name} {frame.shape[1]}x{frame.shape[0]}"
                        + (" BLANK" if camera.is_blank(frame) else ""),
                    ))
            if not scored:
                raise HardwareError("no camera on this machine delivered a frame")
            scored.sort(reverse=True)
            best_score, best_index, best_name = scored[0]
            if best_score < camera.RECOGNITION_THRESHOLD:
                seen = ", ".join(f"index {i} at {sc:+.3f} ({what})" for sc, i, what in scored)
                # Name the two failures that look identical from a score alone: a
                # blank frame and a mismatched view both score around 0.000.
                blank = [i for _, i, what in scored if "BLANK" in what]
                why = (
                    f"every frame from index {blank} carried no image - a camera that opens "
                    "and delivers black frames is an MSMF symptom, not a mounting problem"
                    if blank else
                    "either the fixture is not at the marker, or the camera has moved since "
                    "the reference view was taught"
                )
                raise HardwareError(
                    f"no camera can see the reference view - best was {best_score:+.3f} "
                    f"against a {camera.RECOGNITION_THRESHOLD} threshold ({seen}). {why}. Refusing "
                    "than choosing the least-bad: a camera that cannot see the marker "
                    "corrects nothing and says nothing"
                )
            self._source_str = str(best_index)
        finally:
            self._pause.clear()
            self._reopen.set()
        # The scan released the device, so nothing is streaming for a moment. Do not
        # return until the chosen camera is back: the caller's next command is a
        # teach, and it cannot work without a frame.
        if not _await_frame(self):
            raise HardwareError(
                f"camera {best_index} scored {best_score:+.3f} but did not resume "
                f"streaming after being selected"
            )
        logger.info("selected camera %d on %s, scoring %.4f against the reference view",
                    best_index, best_name, best_score)
        return {
            "camera_source": str(best_index),
            "match_score": best_score,
            "backend": best_name,
            "considered": [{"index": i, "score": round(sc, 4)} for sc, i, _ in scored],
        }

    def _teach(self) -> dict:
        """Take the current frame as the reference view. Refuses without a frame: a blank
        template reads as never aligned, which looks like a mounting problem."""
        # Waits rather than failing immediately: a teach that follows a camera
        # selection arrives while the reader is still reopening the device.
        _await_frame(self)
        with self._lock:
            frame = self._frame
        if frame is None:
            raise HardwareError(
                "nothing to teach from - no frame arrived. The camera is "
                "open but delivering nothing, which on Windows means MSMF opened a "
                "device whose sensor never started"
            )
        template = camera.center_crop(frame)
        with self._lock:
            self._template = template
        self._save_template(template)
        logger.info("taught the reference view, %dx%d", template.shape[1], template.shape[0])
        return {"template_shape": list(template.shape)}

    # --- the reader --------------------------------------------------------

    def _reader_loop(self) -> None:
        """Keep the newest frame available, and keep running whatever happens - the broad
        except is the point, since this thread has no supervisor."""
        cap = None
        while not self._stop.is_set():
            try:
                if self._pause.is_set():
                    # Something else needs the device - two captures cannot hold one.
                    if cap is not None:
                        cap.release()
                        cap = None
                        with self._lock:
                            self._frame = None
                    time.sleep(0.1)
                    continue
                if cap is None or self._reopen.is_set():
                    if cap is not None:
                        cap.release()
                        time.sleep(camera.RELEASE_SETTLE_S)  # release is not immediate on Windows
                    self._reopen.clear()
                    cap, name = camera.open_capture(camera.resolve_source(self._source_str))
                    with self._lock:
                        self._backend_name = name if cap is not None else ""
                    if cap is None:
                        logger.error("camera %s: %s", self._source_str, name)
                        time.sleep(1.0)
                        continue
                ok, frame = cap.read()
                if ok:
                    with self._lock:
                        self._frame, self._frame_t = frame, time.time()
                        self._failures = 0
                        self._frames_read += 1
                    continue
                # A single failed read is normal on some backends - only a run
                # of them means the camera is gone. See camera.py.
                with self._lock:
                    self._failures += 1
                    failures = self._failures
                if failures >= camera.MAX_CONSECUTIVE_READ_FAILURES:
                    logger.warning("camera %s: %d reads failed in a row - reopening",
                                   self._source_str, failures)
                    with self._lock:
                        self._frame = None
                    cap.release()
                    cap = None
                    time.sleep(camera.RELEASE_SETTLE_S)
            except Exception as exc:  # never let this thread die
                logger.exception("camera reader recovering from: %s", exc)
                try:
                    if cap is not None:
                        cap.release()
                except Exception:
                    pass
                cap = None
                with self._lock:
                    self._frame = None
                time.sleep(1.0)
        if cap is not None:
            cap.release()

    # --- template persistence ---------------------------------------------

    def _save_template(self, template) -> None:
        if self._template_path is None:
            return
        try:
            self._template_path.parent.mkdir(parents=True, exist_ok=True)
            camera.cv2.imwrite(str(self._template_path.with_suffix(".png")), template)
            self._template_path.write_text(json.dumps({
                "camera_source": self._source_str,
                "roi_frac": camera.ROI_FRAC,
            }, indent=2))
        except Exception as exc:  # a teach that worked must not fail on the write
            logger.error("could not persist the taught template: %s", exc)

    def _load_template(self) -> None:
        """Load a bench teach if there is one, else the default shipped with the driver. In
        that order: a teach here is a statement about this bench and outranks a committed one."""
        if self._template_path is not None and self._template_path.exists():
            if self._read_template(self._template_path, "taught at this bench"):
                return
        if PACKAGED_TEMPLATE.exists():
            self._read_template(PACKAGED_TEMPLATE.with_suffix(".json"), "shipped with the driver")

    def _read_template(self, meta_path: Path, provenance: str) -> bool:
        try:
            meta = json.loads(meta_path.read_text())
            image = camera.cv2.imread(str(meta_path.with_suffix(".png")),
                                      camera.cv2.IMREAD_GRAYSCALE)
            if image is None:
                return False
            self._template = image
            logger.info("reference view %s, %dx%d, roi %.2f", provenance,
                        image.shape[1], image.shape[0], meta.get("roi_frac", 0.0))
            return True
        except Exception as exc:
            logger.error("could not load the reference view from %s: %s", meta_path, exc)
            return False


def _validate_channel_coverage() -> None:
    """Every declared channel is produced, and nothing else is - the same
    import-time check odrive_backend makes, so drift fails loudly."""
    produced = set(VisionHomeBackend(camera_source="0")._sample())
    declared = set(TELEMETRY_CHANNELS)
    assert not declared - produced, f"declared but never produced: {sorted(declared - produced)}"
    assert not produced - declared, f"produced but not declared: {sorted(produced - declared)}"


_validate_channel_coverage()
