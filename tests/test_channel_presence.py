"""Declared channels must exist, and an unevaluable bound must be loud.

The failure this guards against is the quiet one: a channel a test engineer
declared, that the hardware doesn't actually have, recording nothing for a
whole run while every check reports healthy. You find out at analysis time,
having already believed you had the data to make a decision.

It was real. OdriveBackend swallowed AttributeError into a None value with a
single warning, so the key stayed present in every frame and
TelemetryClient.verify_channels() saw nothing missing - 11 declared channels
behaved this way on the first real ODrive run. And a Bound pointed at one got
None to compare, raising TypeError inside the live evaluation thread, killing
it, leaving fatal_violation unset: the test then ran to completion on real
hardware with no supervision whatsoever.

Two layers are tested here:
  1. Structural absence is detected at connect() and named in the error.
  2. A bound that cannot be evaluated raises, and the runner makes it fatal
     rather than letting it kill the evaluation thread.
"""
from __future__ import annotations

import threading
import time

import pytest

from hardware.backend import MissingChannelError
from hardware.odrive.odrive_backend import OdriveBackend
from protocol.wire import TelemetryFrame
from testcases.asimov.live_rulebook_runner import LiveRulebookRunner
from testcases.asimov.rulebook import Bound, Rulebook, UnevaluableBoundError


class FakeAttrNode:
    """Stands in for the odrive package's attribute graph. Only the
    attributes it's given exist; anything else raises AttributeError exactly
    as the real device handle does."""

    def __init__(self, **attrs):
        for name, value in attrs.items():
            setattr(self, name, value)


def backend_with_device(device):
    backend = OdriveBackend()
    backend._odrv = device
    return backend


# --- layer 1: structural absence at connect() ---------------------------------


def test_path_exists_walks_intermediates_and_leaves():
    device = FakeAttrNode(config=FakeAttrNode(inverter0=FakeAttrNode(current_hard_max=1.0)))
    backend = backend_with_device(device)

    assert backend._path_exists("odrv", "config.inverter0.current_hard_max")
    assert not backend._path_exists("odrv", "config.inverter0.derating_start")  # missing leaf
    assert not backend._path_exists("odrv", "config.brake_resistor0.duty")  # missing intermediate


def test_verify_names_every_missing_channel(monkeypatch):
    """The error must name what's missing - that's the whole point. A count
    alone would leave an engineer guessing which channel to fix."""
    from hardware.odrive import odrive_backend as mod

    monkeypatch.setattr(mod, "_TELEMETRY_PATHS", {
        "present_one": ("odrv", "vbus_voltage"),
        "absent_one": ("odrv", "brake_resistor0.duty"),
        "absent_two": ("axis", "total_charge_used"),
    })
    monkeypatch.setattr(mod, "_SETTERS", {"absent_setter": ("odrv", "config.inverter0.derating_start")})
    monkeypatch.setattr(mod, "_METHODS", {"present_method": ("odrv", "clear_errors", [])})

    device = FakeAttrNode(
        vbus_voltage=48.0,
        clear_errors=lambda: None,
        config=FakeAttrNode(inverter0=FakeAttrNode()),
        axis0=FakeAttrNode(),
        fw_version_major=0, fw_version_minor=6, fw_version_revision=11,
        serial_number=123,
    )
    backend = backend_with_device(device)

    with pytest.raises(MissingChannelError) as exc:
        backend._verify_declared_channels_exist()

    message = str(exc.value)
    assert "3 declared channel(s) do not exist" in message
    for name in ("absent_one", "absent_two", "absent_setter"):
        assert name in message
    assert "present_one" not in message
    assert "brake_resistor0.duty" in message  # the offending path, so it's actionable
    assert "fw 0.6.11" in message


def test_verify_passes_when_everything_resolves(monkeypatch):
    from hardware.odrive import odrive_backend as mod

    monkeypatch.setattr(mod, "_TELEMETRY_PATHS", {"vbus": ("odrv", "vbus_voltage")})
    monkeypatch.setattr(mod, "_SETTERS", {"set_thing": ("axis", "controller.config.pos_gain")})
    monkeypatch.setattr(mod, "_METHODS", {"clear": ("odrv", "clear_errors", [])})

    device = FakeAttrNode(
        vbus_voltage=48.0,
        clear_errors=lambda: None,
        axis0=FakeAttrNode(controller=FakeAttrNode(config=FakeAttrNode(pos_gain=1.0))),
    )
    backend_with_device(device)._verify_declared_channels_exist()  # must not raise


def test_setters_are_probed_without_writing():
    """A health check must not have side effects on real hardware, so the
    probe resolves the parent and uses hasattr rather than assigning."""
    class WriteRejecting:
        pos_gain = 1.0  # a class attribute, so no instance write is needed

        def __setattr__(self, name, value):
            raise AssertionError(f"probe must never write to a channel (tried {name}={value!r})")

    device = FakeAttrNode(axis0=FakeAttrNode(controller=FakeAttrNode(config=WriteRejecting())))
    assert backend_with_device(device)._path_exists("axis", "controller.config.pos_gain")


def test_declared_channel_lists_match_the_implementation_tables():
    """Import-time coverage validation already enforces this; asserting it
    here means pruning a channel from one place and forgetting the other
    fails in the suite, not only when someone imports the backend.

    Mirrors _validate_channel_coverage()'s own invariant, including
    _SPECIAL_COMMANDS - set_axis_state/set_control_mode are implemented as
    dedicated methods with enum validation rather than table entries - and
    _COMPUTED_CHANNELS, which the driver works out rather than reads off the board."""
    from hardware.odrive.odrive_backend import (
        _COMPUTED_CHANNELS,
        _METHODS,
        _SETTERS,
        _SPECIAL_COMMANDS,
        _TELEMETRY_PATHS,
    )
    from hardware.odrive.odrive_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS

    assert set(TELEMETRY_CHANNELS) == set(_TELEMETRY_PATHS) | _COMPUTED_CHANNELS
    assert set(COMMAND_CHANNELS) == set(_SETTERS) | set(_METHODS) | _SPECIAL_COMMANDS


def test_the_odrive_mock_publishes_every_declared_telemetry_channel():
    """A channel added to the real driver but not to the mock makes every
    use_mock run fail at verify_channels() - a testbed that will not stand up at
    all, and only on the path taken when no board is attached, which is the one
    path nobody exercises before they need it."""
    from hardware.odrive.mock_backend import DEFAULTS
    from hardware.odrive.odrive_channels import TELEMETRY_CHANNELS

    missing = set(TELEMETRY_CHANNELS) - set(DEFAULTS)
    assert not missing, f"declared but absent from MockOdriveBackend.DEFAULTS: {sorted(missing)}"


# --- layer 2: an unevaluable bound must be loud -------------------------------


def test_absent_channel_means_bound_does_not_apply():
    """Genuinely absent from the frame is 'not applicable', unchanged - the
    gate/absent case must stay skippable."""
    bound = Bound(name="uv", channel="vbus", lower=10.5)
    assert bound.evaluate({"something_else": 1.0}) is None


def test_none_value_raises_instead_of_typeerror():
    """The exact real-hardware failure: channel present, value None."""
    bound = Bound(name="uv", channel="vbus", lower=10.5)
    with pytest.raises(UnevaluableBoundError) as exc:
        bound.evaluate({"vbus": None})
    assert "vbus" in str(exc.value)
    assert "no value" in str(exc.value)


def test_uncomparable_type_raises_against_a_numeric_limit():
    bound = Bound(name="oc", channel="ibus", upper=30.0)
    with pytest.raises(UnevaluableBoundError):
        bound.evaluate({"ibus": "not a number"})


def test_expected_only_bound_accepts_any_type():
    """An equality check needs no ordering, so a string state channel is
    fine and must not be condemned."""
    bound = Bound(name="state", channel="axis_state", expected="IDLE")
    assert bound.evaluate({"axis_state": "IDLE"}) is False
    assert bound.evaluate({"axis_state": "CLOSED_LOOP"}) is True
    assert bound.evaluate({"axis_state": None}) is True  # not the expected value


def test_runner_makes_an_unevaluable_bound_fatal_instead_of_dying():
    """Regression test for the worst version of this bug: the exception used
    to escape the runner's thread, leaving fatal_violation unset and the test
    running unsupervised while reporting healthy."""
    class Publisher:
        def set_state(self, name, value):
            pass

        def state_snapshot(self):
            return {}

        def record_frame(self, device, channels):
            pass  # derived channels are exercised in tests/test_derived_channels.py

        def await_derivation_frames(self):
            pass

    class Client:
        def discard_backlog(self):
            return 0  # nothing queued: this stream starts when the runner does

        def frames(self):
            for i in range(10):
                yield TelemetryFrame(seq=i, t=float(i), channels={"vbus": None}, device="odrive")
                time.sleep(0.01)

    rulebook = Rulebook(
        name="rb",
        test_names=["t"],
        # No grace: this test is about what the runner does with an unevaluable
        # bound, not about how long one is tolerated first.
        bounds=[Bound(name="uv", channel="vbus", lower=10.5, fatal=True, unevaluable_grace_s=0.0)],
    )
    runner = LiveRulebookRunner(test_id="x", rulebooks=[rulebook], publisher=Publisher())

    escaped = []
    original_hook = threading.excepthook
    threading.excepthook = lambda args: escaped.append(args.exc_type.__name__)
    try:
        runner.start(Client())
        deadline = time.time() + 2.0
        while runner.fatal_violation is None and time.time() < deadline:
            time.sleep(0.01)
        runner.stop()
    finally:
        threading.excepthook = original_hook

    assert escaped == [], f"exception escaped the runner thread: {escaped}"
    assert isinstance(runner.fatal_violation, UnevaluableBoundError)


# --- a channel that drops a sample vs one that is gone ------------------------


def test_other_bounds_are_still_judged_while_one_channel_is_absent():
    """A flaky channel must not blind the rest of the frame: only the bound that
    cannot be evaluated is skipped, and only for as long as it is tolerated."""
    from testcases.asimov.rulebook import Bound, Rulebook, RulebookEvaluator

    evaluator = RulebookEvaluator()
    evaluator.register(Rulebook(name="rb", test_names=["t"], bounds=[
        Bound(name="flaky", channel="temperature", upper=80.0),
        Bound(name="solid", channel="vbus", lower=10.5, fatal=True),
    ]))

    transitions = evaluator.evaluate({"temperature": None, "vbus": 3.0}, 100.0)

    assert [x.bound_label for x in transitions] == ["solid"], (
        "the absent channel stopped the frame's other bound being judged"
    )


def test_a_recovered_channel_starts_its_grace_again():
    """Otherwise a channel that drops one sample a minute would eventually exhaust
    a window it never actually stayed absent for."""
    from testcases.asimov.rulebook import Bound, Rulebook, RulebookEvaluator, UnevaluableBoundError

    bound = Bound(name="flaky", channel="temperature", upper=80.0, unevaluable_grace_s=1.0)
    evaluator = RulebookEvaluator()
    evaluator.register(Rulebook(name="rb", test_names=["t"], bounds=[bound]))

    for minute in range(3):
        evaluator.evaluate({"temperature": None}, 100.0 + minute * 60)      # a dropped sample
        evaluator.evaluate({"temperature": 24.0}, 100.1 + minute * 60)      # and back

    evaluator.evaluate({"temperature": None}, 400.0)
    with pytest.raises(UnevaluableBoundError):
        evaluator.evaluate({"temperature": None}, 401.5)


# --- state channels must exist before the first device frame ----------------
#
# The same quiet failure one layer up. The engine fixes a device file's header
# from the union of its first frames and drops channels that appear later, so a
# state channel published after the drivers are already streaming may never make
# the header - and a run then records everything except the numbers the test was
# run to produce, with nothing reporting a problem.


class _RecordingPublisher:
    """A state publisher that only remembers the order it was called in."""

    def __init__(self, log):
        self._log = log

    def set_state(self, name, value):
        self._log.append(("state", name))


class _RecordingTestbed:
    """A testbed that records when its drivers would have started."""

    def __init__(self, log):
        self._log = log

    def start(self):
        self._log.append(("testbed", "start"))


def _seed_order(monkeypatch):
    """Drive BaseYdriveTest.pre_test_setup() against fakes and report what was
    published before the drivers started, and what after.

    TestCase.__init__ is skipped deliberately: what is under test is the order of
    two calls, and the rest of the lifecycle needs a run, an engine and a supply."""
    from testcases.ydrive.testcases import base_ydrive_test

    log = []
    monkeypatch.setattr(base_ydrive_test, "YdriveTestbed", lambda **kwargs: _RecordingTestbed(log))
    monkeypatch.setattr(base_ydrive_test, "LiveRulebookRunner", lambda **kwargs: None)

    case = object.__new__(base_ydrive_test.BaseYdriveTest)
    case.test_id = "test-seed-order"
    case._use_mock = True
    case._output_dir = None
    case._publisher = _RecordingPublisher(log)

    case.pre_test_setup()

    started = log.index(("testbed", "start"))
    return (
        {name for kind, name in log[:started] if kind == "state"},
        [name for kind, name in log[started:] if kind == "state"],
    )


def test_every_state_channel_is_published_before_any_driver_starts(monkeypatch):
    """Seeding has to precede testbed.start(), not merely happen in pre_test_setup.

    Both orders look identical in a passing run on a slow stand: the seed lands
    inside the header window and everything is recorded. On a stand whose drivers
    come up quickly the frames win the race, and the only symptom is a missing
    column in a CSV nobody reads until analysis."""
    from testcases.ydrive.channels import DEFAULT_STATE

    seeded_first, late = _seed_order(monkeypatch)

    assert not late, f"published after the drivers were already streaming: {late}"
    missing = sorted(set(DEFAULT_STATE) - seeded_first)
    assert not missing, f"state channels that could miss the header: {missing}"

    # Bound-status channels too. Derived from RULEBOOKS rather than hand-listed and
    # just as droppable: a run missing one reads as a run with no supervision of it.
    from testcases.ydrive.testcases.base_ydrive_test import BaseYdriveTest
    assert "test_status" in seeded_first
    for rulebook in BaseYdriveTest.RULEBOOKS:
        for bound in rulebook.bounds:
            assert f"{bound.label}_status" in seeded_first


def test_one_streams_frames_do_not_reset_anothers_unevaluable_grace():
    """A dead sensor has to stay dead across every stream, not just its own.

    One RulebookEvaluator is shared by every telemetry stream a runner watches,
    and a stream that does not carry a bound's channel reaches the "didn't apply"
    path on every one of its frames. Clearing the grace timer there lets the
    ODrive's ~12.6 Hz reset the thermocouple DAQ's grace before it can ever
    expire, so a channel reading FAULT for a whole run is tolerated for all of
    it, with its status channel still reporting the PASS it was seeded with.

    Every ydrive test watches two streams and every zdrive test three, so this
    path is the normal one rather than an edge case."""
    from testcases.asimov.rulebook import Bound, Rulebook, RulebookEvaluator

    rulebook = Rulebook(
        name="two_stream",
        test_names=["t"],
        bounds=[Bound(channel="temperature_5_c", upper=80.0, name="thermal",
                      fatal=True, unevaluable_grace_s=10.0)],
    )
    dead_daq = {"temperature_5_c": None}
    other_stream = {"board_vbus_voltage": 48.0}

    evaluator = RulebookEvaluator()
    evaluator.register(rulebook)

    t = 0.0
    with pytest.raises(UnevaluableBoundError, match="temperature_5_c"):
        for _ in range(2000):
            for frame in (dead_daq, other_stream):
                t += 0.079
                evaluator.evaluate(frame, t)

    assert t < 20.0, (
        f"the grace ran to {t:.1f}s against a 10s budget - the other stream is "
        "still resetting the timer"
    )


def test_a_stream_that_lacks_the_channel_reports_nothing_either_way():
    """The other half of the rule: a stream carrying no temperatures must neither
    reset the grace nor make the bound look evaluated. Its status stays whatever
    it was seeded with, which is what keeps 'no transitions' honest."""
    from testcases.asimov.rulebook import Bound, Rulebook, RulebookEvaluator

    rulebook = Rulebook(
        name="two_stream",
        test_names=["t"],
        bounds=[Bound(channel="temperature_5_c", upper=80.0, name="thermal", fatal=True)],
    )
    evaluator = RulebookEvaluator()
    evaluator.register(rulebook)

    transitions = []
    for i in range(200):
        transitions += evaluator.evaluate({"board_vbus_voltage": 48.0}, i * 0.079)

    assert not transitions, f"a stream with no temperatures moved a thermal bound: {transitions}"


def test_every_device_a_testbed_declares_is_one_the_engine_records():
    """A testbed's DEVICES and the engine's TELEMETRY_ENDPOINTS have to agree, or a
    run refuses to start with DeviceNotRecorded - correctly, since the alternative is
    a run directory quietly missing a whole device.

    Two lists in two files, so adding a driver to a stand and forgetting the engine
    is a one-line omission that only shows up on the bench. This is that check."""
    from protocol.wire import TELEMETRY_ENDPOINTS
    from testbeds.example_testbed.example_testbed import ExampleTestbed
    from testbeds.ydrive_testbed.ydrive_testbed import YdriveTestbed
    from testbeds.zdrive_testbed.zdrive_testbed import ZdriveTestbed

    for testbed in (YdriveTestbed, ZdriveTestbed, ExampleTestbed):
        unrecorded = [d for d in testbed.DEVICES if d not in TELEMETRY_ENDPOINTS]
        assert not unrecorded, (
            f"{testbed.__name__} declares {unrecorded}, which the engine does not "
            "subscribe to - every run of it would refuse to start"
        )
