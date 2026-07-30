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
    dedicated methods with enum validation rather than table entries."""
    from hardware.odrive.odrive_backend import (
        _METHODS,
        _SETTERS,
        _SPECIAL_COMMANDS,
        _TELEMETRY_PATHS,
    )
    from hardware.odrive.odrive_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS

    assert set(TELEMETRY_CHANNELS) == set(_TELEMETRY_PATHS)
    assert set(COMMAND_CHANNELS) == set(_SETTERS) | set(_METHODS) | _SPECIAL_COMMANDS


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

    class Client:
        def frames(self):
            for i in range(10):
                yield TelemetryFrame(seq=i, t=float(i), channels={"vbus": None}, device="odrive")
                time.sleep(0.01)

    rulebook = Rulebook(
        name="rb", test_names=["t"], bounds=[Bound(name="uv", channel="vbus", lower=10.5, fatal=True)]
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
