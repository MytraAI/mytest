"""The vision-home camera: finding a taught view, and deciding what to do about it.

Synthetic frames throughout - no camera. What is worth testing here is the search
and the declared surface; what breaks on a bench is a real camera's behaviour,
which a fake of cv2 cannot reproduce and camera.py records instead.
"""
from __future__ import annotations

import sys

import pytest

cv2 = pytest.importorskip("cv2", reason="opencv is an optional install")
import numpy as np  # noqa: E402

from hardware.vision_home import camera  # noqa: E402
from hardware.vision_home.vision_home_channels import (  # noqa: E402
    COMMAND_CHANNELS,
    TELEMETRY_CHANNELS,
)

STRIPE_PITCH_PX = 200
"""Two stripes, comparable to the hardware feature sizes below as on the real bench.
The ratio between them is what decides whether an alias survives."""


def _fixture_frame(shift_px=0, height=720, width=1280, hardware=True):
    """A striped fixture, optionally with the non-repeating hardware that moves with
    it. `hardware=False` is the tape alone, which is what makes the alias visible."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    for x in range(width):
        if ((x + shift_px) // (STRIPE_PITCH_PX // 2)) % 2 == 0:
            frame[:, x] = 255
    if hardware:
        cv2.rectangle(frame, (shift_px + 300, height // 2), (shift_px + 440, height), (90, 90, 90), -1)
        cv2.circle(frame, (shift_px + 620, int(height * 0.78)), 60, (200, 60, 60), -1)
    return frame


def _fixed_comparison(frame, template):
    """What the driver used to do: compare a fixed centre crop, with no search."""
    return float(cv2.matchTemplate(
        camera.center_crop(frame), template, cv2.TM_CCOEFF_NORMED).max())


# --- finding the marker -----------------------------------------------------


def test_an_aligned_view_scores_one_at_zero_displacement():
    frame = _fixture_frame()
    located = camera.locate_marker(frame, camera.center_crop(frame))

    assert located.score == pytest.approx(1.0, abs=1e-6)
    assert (located.dx, located.dy) == (0, 0)


def test_a_search_finds_the_displaced_fixture_a_fixed_crop_calls_absent():
    """The point of searching, and the bug it replaced. Measured on the bench: a
    fixture 175 px off the reference read -0.48 compared, +0.79 searched."""
    template = camera.center_crop(_fixture_frame())

    for shift in (0, 40, 120, 170):
        assert camera.locate_marker(_fixture_frame(shift_px=shift), template).found, shift
    assert _fixed_comparison(_fixture_frame(shift_px=170), template) < camera.RECOGNITION_THRESHOLD


def test_the_search_reports_which_way_and_how_far():
    """The displacement is the measurement a score-only gate threw away."""
    template = camera.center_crop(_fixture_frame())

    right = camera.locate_marker(_fixture_frame(shift_px=90), template)
    left = camera.locate_marker(_fixture_frame(shift_px=-90), template)

    assert right.dx > 40 and left.dx < -40, f"got {right.dx} and {left.dx}"


def test_a_frame_too_small_to_scale_to_is_not_found_rather_than_raising():
    """A camera that drops resolution mid-run is handled by rescaling the template;
    one that drops to nothing must still not kill the driver."""
    template = camera.center_crop(_fixture_frame(height=1080, width=1920))
    located = camera.locate_marker(np.zeros((6, 6, 3), np.uint8), template)

    assert located.found is False and located.score == 0.0


# --- the threshold ----------------------------------------------------------


def test_the_recognition_threshold_separates_the_bench_measurements():
    """0.79 for the right camera 175 px off the reference, 0.26 for one facing a room."""
    assert 0.26 < camera.RECOGNITION_THRESHOLD < 0.79


# --- opening a camera -------------------------------------------------------


def test_ffmpeg_is_never_a_candidate_backend():
    """The prebuilt headless wheel has no libavdevice, so FFMPEG can never open a live
    camera - auto-probing it costs seconds and prints a misleading warning."""
    assert cv2.CAP_FFMPEG not in [flag for flag, _ in camera.candidate_backends()]


def test_windows_tries_msmf_before_dshow(monkeypatch):
    """DSHOW refuses to open by plain index for some camera/driver combinations and
    MSMF usually works; DSHOW stays a fallback because the reverse happens too."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert [name for _, name in camera.candidate_backends()] == ["MSMF", "DSHOW"]

    for platform, expected in (("darwin", "AVFOUNDATION"), ("linux", "V4L2")):
        monkeypatch.setattr(sys, "platform", platform)
        assert camera.candidate_backends()[0][1] == expected


def test_an_address_is_passed_through_and_an_index_is_an_int():
    """A stable /dev/v4l/by-id path is the answer on CentOS, where an index is not."""
    assert camera.resolve_source("0") == 0
    assert camera.resolve_source("rtsp://bench-cam/stream") == "rtsp://bench-cam/stream"
    assert camera.resolve_source("/dev/v4l/by-id/usb-Foo_Bar-video-index0").startswith("/dev")


# --- the declared surface ---------------------------------------------------


def test_the_backend_produces_exactly_its_declared_channels():
    from hardware.vision_home.vision_home_backend import VisionHomeBackend

    assert set(VisionHomeBackend(camera_source="0")._sample()) == set(TELEMETRY_CHANNELS)
    # Which backend delivered frames is among them: on Windows that is neither
    # predictable nor stable across benches, and it is the first thing worth knowing.
    assert "camera_backend" in TELEMETRY_CHANNELS


def test_the_backend_accepts_exactly_its_declared_actions():
    import asyncio

    from hardware.vision_home.vision_home_backend import VisionHomeBackend

    assert asyncio.run(VisionHomeBackend(camera_source="0").list_actions()) == COMMAND_CHANNELS


def test_the_backend_names_its_own_device():
    """The runner stamps frames with backend.device and the engine files frames by
    device name, so an unnamed backend publishes a whole device outside the run."""
    from protocol.wire import DEVICE_VISION_HOME
    from hardware.vision_home.vision_home_backend import VisionHomeBackend

    assert VisionHomeBackend(camera_source="0").device == DEVICE_VISION_HOME


def test_nothing_is_visible_before_a_template_exists():
    from hardware.vision_home.vision_home_backend import VisionHomeBackend

    sample = VisionHomeBackend(camera_source="0")._sample()

    assert sample["taught"] is False and sample["aligned"] is False


def test_teaching_without_a_frame_is_refused(monkeypatch):
    """A blank template would score every later frame against nothing and read as
    never aligned - a failure that looks like a mounting problem."""
    import asyncio

    from hardware.backend import HardwareError
    from hardware.vision_home.vision_home_backend import VisionHomeBackend

    from hardware.vision_home import vision_home_backend as backend_module

    monkeypatch.setattr(backend_module, "FRAME_WAIT_S", 0.1)
    with pytest.raises(HardwareError, match="no frame arrived"):
        asyncio.run(VisionHomeBackend(camera_source="0").execute("teach"))


def test_a_reference_view_ships_with_the_driver():
    """Teaching is meant to have happened once. Without a packaged default a run
    corrects nothing and only says so in one log line."""
    import json

    from hardware.vision_home.vision_home_backend import PACKAGED_TEMPLATE

    assert PACKAGED_TEMPLATE.exists(), "no committed reference view"
    image = cv2.imread(str(PACKAGED_TEMPLATE), cv2.IMREAD_GRAYSCALE)
    assert image is not None and image.size > 0, "the committed reference view is unreadable"

    meta = json.loads(PACKAGED_TEMPLATE.with_suffix(".json").read_text())
    assert "reference_position_turns" not in meta, (
        "the driver deals in pixels; what the axis reads at the marker is the test's"
    )
    assert meta["roi_frac"] == camera.ROI_FRAC, (
        "the committed view was cropped at a different ROI_FRAC than the code uses"
    )


# --- picking the camera -----------------------------------------------------


def _streaming(backend, frame=None):
    """Pose the backend as already having a frame, standing in for the reader thread."""
    import time
    with backend._lock:
        backend._frame = frame if frame is not None else _fixture_frame()
        backend._frame_t = time.time()
    return backend


def _fake_cameras(monkeypatch, frames):
    """Pose a machine's cameras: index -> frame, or absent."""
    class _Cap:
        def __init__(self, frame): self._frame = frame
        def read(self): return True, self._frame
        def release(self): pass

    monkeypatch.setattr(camera, "open_capture",
                        lambda i: ((_Cap(frames[i]), "FAKE") if i in frames else (None, "none")))
    monkeypatch.setattr(camera, "RELEASE_SETTLE_S", 0.0)


def test_a_displaced_fixture_is_still_enough_to_identify_the_camera(monkeypatch):
    """The resilience that matters: the fixture need only be in view, not parked to
    the pixel. On the bench, 0.79 displaced against 0.26 for a room."""
    import asyncio

    from hardware.vision_home.vision_home_backend import VisionHomeBackend

    backend = _streaming(VisionHomeBackend(camera_source="0"))
    backend._template = camera.center_crop(_fixture_frame())
    _fake_cameras(monkeypatch, {0: np.full((720, 1280, 3), 90, np.uint8),
                                1: _fixture_frame(shift_px=170)})

    assert asyncio.run(backend.execute("select_best_camera", max_index=2))["camera_source"] == "1"


def test_selection_refuses_when_no_camera_can_see_the_marker(monkeypatch):
    """Refusing beats choosing the least bad: a camera that cannot see the marker
    corrects nothing and says nothing, leaving a whole run unsupervised."""
    import asyncio

    from hardware.backend import HardwareError
    from hardware.vision_home.vision_home_backend import VisionHomeBackend

    backend = VisionHomeBackend(camera_source="0")
    backend._template = camera.center_crop(_fixture_frame())
    rng = np.random.default_rng(0)
    _fake_cameras(monkeypatch, {0: rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8),
                                1: np.full((720, 1280, 3), 200, np.uint8)})

    with pytest.raises(HardwareError, match="no camera can see the reference view"):
        asyncio.run(backend.execute("select_best_camera", max_index=2))


def test_selection_names_every_candidate_it_considered(monkeypatch):
    """So a run records that the choice was decisive, not a coin flip between two
    cameras that both scored like noise."""
    import asyncio

    from hardware.vision_home.vision_home_backend import VisionHomeBackend

    backend = _streaming(VisionHomeBackend(camera_source="1"))
    backend._template = camera.center_crop(_fixture_frame())
    _fake_cameras(monkeypatch, {0: np.full((720, 1280, 3), 30, np.uint8), 1: _fixture_frame()})

    chosen = asyncio.run(backend.execute("select_best_camera", max_index=2))

    assert chosen["camera_source"] == "1"
    assert len(chosen["considered"]) == 2


# --- the teststeps ----------------------------------------------------------


class FakeMarkerStand:
    """A stand whose camera and axis can be posed. `calls` records every command, so a
    test can assert that re-referencing commands no motion."""

    def __init__(self, position, aligned=True, taught=True, score=0.99, raises=None):
        self.calls = []
        self.reads = 0
        self._raises = raises
        self.command = self
        self.vision = self
        self._position = position
        self._alignment = (aligned, score, taught)

    def get_marker_alignment(self):
        from testbeds.ydrive_testbed.ydrive_testbed import MarkerAlignment
        self.reads += 1
        if self._raises is not None:
            raise self._raises
        return MarkerAlignment(*self._alignment)

    def select_best_camera(self):
        self.calls.append("select")
        return {"camera_source": "0", "match_score": 0.79, "considered": []}

    def teach(self):
        self.calls.append("teach")
        return {"template_shape": [360, 640]}

    def get_pos_estimate(self):
        return self._position

    def set_pos_estimate(self, value):
        self.calls.append(f"pos_estimate:{value}")

    def set_position(self, value):
        self.calls.append(f"move:{value}")

    def set_axis_state(self, state):
        self.calls.append(f"axis:{state}")

    def power_brake_bus(self, on):
        self.calls.append("brake:release" if on else "brake:engage")

    def get_axis_armed_status(self):
        return True


class MarkerTestCase:
    test_id = "test-marker"

    def __init__(self, testbed, position_claimed_at_marker=None, total_distance_m=0.0):
        self.testbed = testbed
        self.position_claimed_at_marker = position_claimed_at_marker
        self.total_distance_m = total_distance_m
        self.distance_at_last_correction_m = 0.0
        self._last_position = 0.0
        self.state = {}

    def set_state(self, name, value):
        self.state[name] = value

    def check_should_continue(self):
        pass

    def wait_for(self, seconds):
        pass


def _sweep(watch, positions, velocity=-7.7):
    """Drive a watcher over one leg's worth of frames."""
    from testbeds.ydrive_testbed.ydrive_testbed import Motion

    for position in positions:
        watch(Motion(position=position, velocity=velocity, armed=True))


def _corrected_to(stand):
    """The single pos_estimate write in `calls`, as a number."""
    writes = [c for c in stand.calls if c.startswith("pos_estimate:")]
    assert len(writes) == 1, stand.calls
    return float(writes[0].split(":")[1])


def test_the_camera_is_only_asked_while_the_axis_is_near_the_marker():
    """The tape is periodic and the search runs over the whole frame, so a match
    somewhere else in the stroke is possible and is not the marker."""
    from testcases.ydrive.teststeps.teststeps import MarkerWatch

    stand = FakeMarkerStand(position=-6.0)
    watch = MarkerWatch(MarkerTestCase(stand, position_claimed_at_marker=-15.0))

    _sweep(watch, [110.0, 80.0, 40.0, 10.0])
    assert stand.reads == 0, "the far end of the stroke is not a place the marker can be"

    _sweep(watch, [-14.9])
    assert stand.reads == 1


def test_a_sighting_re_references_the_axis_and_commands_no_motion():
    """The fixture was seen at the marker 0.1 turns late, so the axis reads 0.1 turns
    past where it should - which is what gets taken back, not the load."""
    from testcases.ydrive.teststeps.teststeps import MarkerWatch

    stand = FakeMarkerStand(position=-6.0)          # pulled back by the time it lands
    case = MarkerTestCase(stand, position_claimed_at_marker=-15.0)
    watch = MarkerWatch(case)

    _sweep(watch, [-15.1])
    watch.apply()

    # -15.0 + (-6.0 - -15.1) = -5.9: the axis should read -5.9 where it reads -6.0.
    assert _corrected_to(stand) == pytest.approx(-5.9)
    # Nothing but that write: writing pos_estimate changes what positions MEAN, and a
    # set_position here would drive 1800 lb at a reversal.
    assert len(stand.calls) == 1, stand.calls


def test_the_first_sighting_of_the_leg_is_the_one_used():
    """Always the same crossing of the same view at the same sign of speed, so whatever
    lag there is between the two streams biases every correction the same way."""
    from testcases.ydrive.teststeps.teststeps import MarkerWatch

    stand = FakeMarkerStand(position=-6.0)
    watch = MarkerWatch(MarkerTestCase(stand, position_claimed_at_marker=-15.0))

    _sweep(watch, [-12.0, -14.0, -16.0, -17.4])
    watch.apply()

    # seen at -12.0: -15.0 + (-6.0 - -12.0) = -9.0
    assert _corrected_to(stand) == pytest.approx(-9.0)
    assert stand.reads == 1, "it stops asking once it has its answer"


def test_an_unaligned_leg_corrects_nothing():
    """The camera not seeing the marker is not evidence about position."""
    from testcases.ydrive.teststeps.teststeps import MarkerWatch

    stand = FakeMarkerStand(position=-6.0, aligned=False, score=0.31)
    case = MarkerTestCase(stand, position_claimed_at_marker=-15.0)
    watch = MarkerWatch(case)

    _sweep(watch, [-12.0, -15.0, -17.4])
    watch.apply()

    assert stand.calls == []
    assert case.state["marker_match_score"] == 0.31


def test_a_leg_with_no_reference_view_corrects_nothing():
    """No committed default and no teach means there is nothing to recognise."""
    from testcases.ydrive.teststeps.teststeps import MarkerWatch

    stand = FakeMarkerStand(position=-15.0, taught=False, aligned=False)
    case = MarkerTestCase(stand, position_claimed_at_marker=-15.0)
    watch = MarkerWatch(case)

    _sweep(watch, [-15.0])
    watch.apply()

    assert stand.calls == []
    assert "distance_since_correction_m" not in case.state, (
        "an untaught camera is a different problem from one that cannot see the marker"
    )


def test_a_camera_that_has_stopped_publishing_does_not_stop_the_stroke():
    """This watcher runs inside the arrival loop of a leg carrying 1800 lb. A dead
    camera means no corrections, which the distance counter says - it does not mean
    the stroke stops mid-leg."""
    from hardware.clients.telemetry_client import TelemetryTimeout
    from testcases.ydrive.teststeps.teststeps import MarkerWatch

    stand = FakeMarkerStand(position=-6.0, raises=TelemetryTimeout("no frame"))
    case = MarkerTestCase(stand, position_claimed_at_marker=-15.0, total_distance_m=430.0)
    watch = MarkerWatch(case)

    _sweep(watch, [-12.0, -15.0, -17.4])
    watch.apply()

    assert stand.calls == []
    assert case.distance_at_last_correction_m == 0.0, "the mark did not move, so nothing landed"


def test_setup_selects_against_the_committed_view_then_re_teaches():
    """Two views, two jobs: the committed one proves this is the right camera at the
    marker, the fresh one measures in today's light at this exact park."""
    import testcases.ydrive.teststeps.teststeps as steps

    stand = FakeMarkerStand(position=0.0)
    case = MarkerTestCase(stand)
    original = steps.await_operator
    steps.await_operator = lambda tc, instruction: None
    try:
        steps.establish_reference_by_camera(case, marker_position=-15.0)
    finally:
        steps.await_operator = original

    assert [c for c in stand.calls if not c.startswith(("axis:", "brake:", "move:"))] == [
        "select", "pos_estimate:-15.0", "teach"
    ]
    assert case.state["position_claimed_at_marker"] == -15.0


# --- the marker's place in the stroke ---------------------------------------


def test_the_marker_is_above_the_top_of_the_stroke_so_its_number_is_negative():
    """Setup writes pos_estimate at the marker, so the same turn count means the same
    place in every run. THE SIGN IS THE FAILURE MODE: the camera looks at the top, the
    top is 0, position decreases going up, so a positive number here says the fixture is
    a whole stroke below where it is and the first commanded leg drives it into the stop."""
    from testcases.ydrive.testcases.testcases import CycleBrakeEnduranceTest as T

    test = T(require_engine=False)

    assert test.position_claimed_at_marker == T.MARKER_POSITION
    assert T.BRAKE_TARGET_POSITION < T.START_POSITION, "0 is the top, 110 the bottom"
    assert T.MARKER_POSITION < T.BRAKE_TARGET_POSITION, (
        f"the marker is above the top of the stroke, so it is negative - "
        f"{T.MARKER_POSITION} is on the wrong side of {T.BRAKE_TARGET_POSITION}"
    )


def test_the_marker_sits_inside_the_measured_turnaround():
    """The load must reach the marker every cycle or corrections just stop. Measured
    turnarounds at 1800 lb reached 17.4 to 18.5 turns past the commanded end."""
    from testcases.ydrive.testcases.testcases import CycleBrakeEnduranceTest

    past_the_top = (CycleBrakeEnduranceTest.BRAKE_TARGET_POSITION
                    - CycleBrakeEnduranceTest.MARKER_POSITION)

    assert 0 < past_the_top < 17.4, (
        "the marker is beyond the shallowest turnaround measured, so the load would "
        "not reach it and corrections would silently stop"
    )


def test_the_template_is_a_fraction_of_a_frame_not_a_number_of_pixels():
    """A view taught through a 1920x1080 camera is 960x540, and backends disagree
    about default resolution - so matching it against a camera delivering 640x480 is
    the normal case on a second machine. Unscaled it does not fit and scores exactly
    0.000 on every candidate, which reads as "no camera can see the marker"."""
    template = camera.center_crop(_fixture_frame(height=1080, width=1920))
    scene = _fixture_frame(height=1080, width=1920)

    scores = [camera.locate_marker(cv2.resize(scene, (w, h)), template).score
              for w, h in ((1920, 1080), (1280, 720), (640, 480))]

    assert all(s > camera.RECOGNITION_THRESHOLD for s in scores), scores


def test_a_blank_frame_is_not_a_working_camera():
    """A third thing read() returning True does not prove. MSMF opens a camera and
    delivers all-black frames when the sensor has not started, and a black frame
    correlates against anything at exactly 0.000 - indistinguishable from a
    mismatched view unless it is named."""
    assert camera.is_blank(np.zeros((480, 640, 3), np.uint8))
    assert camera.is_blank(np.full((480, 640, 3), 255, np.uint8)), "uniform white too"
    assert not camera.is_blank(_fixture_frame())


def test_selection_does_not_return_until_the_chosen_camera_is_streaming(monkeypatch):
    """Selecting releases the device to scan the others, so nothing is streaming for a
    moment afterwards - and the caller's next command is a teach, which needs a frame.
    Returning early made that a race, and the race lost on the bench."""
    import asyncio

    from hardware.backend import HardwareError
    from hardware.vision_home import vision_home_backend as backend_module
    from hardware.vision_home.vision_home_backend import VisionHomeBackend

    backend = VisionHomeBackend(camera_source="0")
    backend._template = camera.center_crop(_fixture_frame())
    _fake_cameras(monkeypatch, {0: _fixture_frame()})
    # No reader thread here, so no frame ever arrives - selection must say so rather
    # than hand back a camera the next command cannot use.
    monkeypatch.setattr(backend_module, "FRAME_WAIT_S", 0.2)

    with pytest.raises(HardwareError, match="did not resume streaming"):
        asyncio.run(backend.execute("select_best_camera", max_index=1))


def test_teaching_waits_for_a_frame_before_giving_up(monkeypatch):
    """Same gap, from the other side: a teach right after a selection arrives while the
    reader is still reopening, so it waits rather than failing on a gap it caused."""
    import asyncio
    import threading
    import time as time_module

    from hardware.vision_home.vision_home_backend import VisionHomeBackend

    backend = VisionHomeBackend(camera_source="0")

    def deliver_late():
        time_module.sleep(0.3)
        with backend._lock:
            backend._frame, backend._frame_t = _fixture_frame(), time_module.time()

    threading.Thread(target=deliver_late, daemon=True).start()
    result = asyncio.run(backend.execute("teach"))

    assert result["template_shape"], "it waited, then taught"


def test_setup_proceeds_on_the_configured_camera_when_nothing_recognises_the_view():
    """A camera moved since the view was committed invalidates the view without
    invalidating the mount, and blocking the run over that is too brittle. It goes on
    with the marker position taken on the operator's word - which is what every
    hand-set origin on this stand has always rested on."""
    import testcases.ydrive.teststeps.teststeps as steps
    from hardware.clients.command_client import CommandClientError

    class Stale(FakeMarkerStand):
        def select_best_camera(self):
            self.calls.append("select")
            raise CommandClientError("no camera can see the reference view")

    stand = Stale(position=0.0)
    case = MarkerTestCase(stand)
    original = steps.await_operator
    steps.await_operator = lambda tc, instruction: None
    try:
        steps.establish_reference_by_camera(case, marker_position=-15.0)
    finally:
        steps.await_operator = original

    assert [c for c in stand.calls if not c.startswith(("axis:", "brake:", "move:"))] == [
        "select", "pos_estimate:-15.0", "teach"
    ]
    assert case.state["camera_selected_by"] == "configuration", (
        "the record has to say the marker position was asserted, not verified"
    )


def test_a_recognised_camera_is_recorded_as_verified():
    """The other half: recognised means the fixture was verified to be at the marker,
    configured means somebody said so. Different claims about the same number."""
    import testcases.ydrive.teststeps.teststeps as steps

    stand = FakeMarkerStand(position=0.0)
    case = MarkerTestCase(stand)
    original = steps.await_operator
    steps.await_operator = lambda tc, instruction: None
    try:
        steps.establish_reference_by_camera(case, marker_position=-15.0)
    finally:
        steps.await_operator = original

    assert case.state["camera_selected_by"] == "reference view"


def test_the_alignment_window_scales_with_the_frame():
    """A pixel count is not a window: 60 px of a 1920-wide frame is three times the
    physical distance at 640 wide, and this window is the residual error of every
    correction rather than a preference. Backends disagree about resolution and a
    bench changes it, so it is a fraction - like the template."""
    from hardware.vision_home.vision_home_backend import ALIGN_TOLERANCE_FRAC

    windows = {w: int(w * ALIGN_TOLERANCE_FRAC) for w in (1920, 1280, 640)}

    assert windows[1920] > windows[1280] > windows[640], windows
    # Same share of the frame at every resolution, which is what makes it meaningful.
    for w in (1920, 1280, 640):
        assert windows[w] / w == pytest.approx(ALIGN_TOLERANCE_FRAC, abs=0.002)


def test_only_a_landed_correction_moves_the_mark_the_distance_is_measured_from():
    """distance_since_correction_m is derived from this mark on every frame, so leaving it
    alone is how a dead camera shows up: the channel climbs and never resets. Not a
    measurement of the drive - whether this mechanism is still working."""
    from testcases.ydrive.teststeps.teststeps import MarkerWatch

    stand = FakeMarkerStand(position=-6.0, aligned=False, score=0.2)
    case = MarkerTestCase(stand, position_claimed_at_marker=-15.0)

    for travelled in (120.0, 260.0, 400.0):
        case.total_distance_m = travelled
        watch = MarkerWatch(case)
        _sweep(watch, [-15.0])
        watch.apply()
        assert case.distance_at_last_correction_m == 0.0, "nothing seen, so nothing landed"

    stand._alignment = (True, 0.99, True)
    watch = MarkerWatch(case)
    _sweep(watch, [-15.0])
    watch.apply()

    assert case.distance_at_last_correction_m == pytest.approx(400.0)


def test_a_camera_that_stops_correcting_ends_the_run():
    """Not a bound on drift, which this test does not measure - a bound on how long the
    mechanism that removes it may go on not working. Nothing else on this stand can see the
    load slip past the motor, so a bumped camera is otherwise silent."""
    from testcases.ydrive.rulebooks.ydrive_rulebook import (
        MAX_DISTANCE_SINCE_CORRECTION_M,
        YDRIVE_RULEBOOK,
    )

    bound = next(b for b in YDRIVE_RULEBOOK.bounds if b.name == "marker_correction_bound")

    assert bound.fatal, "the load walks into the clearance the brake needs"
    assert bound.evaluate({"distance_since_correction_m": 0.0}) is False
    assert bound.evaluate({"distance_since_correction_m": 900.0}) is False
    assert bound.evaluate({"distance_since_correction_m": 1100.0}) is True
    # A cycle covers 24.3 m and corrections land on most of them, so an occasional
    # miss cannot reach this - it takes about 41 consecutive cycles of seeing nothing.
    assert MAX_DISTANCE_SINCE_CORRECTION_M / 24.3 > 20
