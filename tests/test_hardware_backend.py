"""The contract HardwareBackend carries for every backend: the
connected-state guard, the declared device name and sample interval that
runner.run() reads off the backend, and value coercion for the JSON wire.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from hardware.backend import HardwareBackend, HardwareError, to_jsonable
from hardware.cpx400dp.cpx400dp_backend import Cpx400dpBackend
from hardware.mock_daq.mock_backend import MockDaqBackend
from hardware.mock_dut.mock_backend import MockDutBackend
from hardware.mock_power_supply.mock_backend import MockPowerSupplyBackend
from hardware.odrive.mock_backend import MockOdriveBackend
from hardware.odrive.odrive_backend import OdriveBackend
from protocol.wire import UNKNOWN_DEVICE

ALL_BACKENDS = [
    MockDaqBackend,
    MockDutBackend,
    MockPowerSupplyBackend,
    MockOdriveBackend,
    OdriveBackend,
    Cpx400dpBackend,
]


class BareBackend(HardwareBackend):
    """Implements only the abstract methods, so what remains is whatever the
    base class supplies."""

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def get_status(self) -> dict:
        return {}

    def stream_samples(self) -> AsyncIterator[dict]:  # pragma: no cover - never iterated here
        raise NotImplementedError

    async def execute(self, action: str, **params: Any) -> Any:
        raise NotImplementedError

    async def list_actions(self) -> List[str]:
        return []


def test_guard_is_inherited_not_reimplemented():
    """No backend should carry its own copy of the guard."""
    for cls in ALL_BACKENDS:
        assert "_require_connected" not in cls.__dict__, f"{cls.__name__} still defines its own guard"


def test_guard_raises_until_connected():
    backend = BareBackend()
    assert backend.is_connected is False
    with pytest.raises(HardwareError, match="not connected"):
        backend._require_connected()

    backend._connected = True
    assert backend.is_connected is True
    backend._require_connected()  # must not raise


def test_connection_state_can_be_a_handle_instead_of_a_flag():
    """OdriveBackend's connection is the device handle, so it overrides
    is_connected rather than keeping a parallel bool that could drift."""
    backend = OdriveBackend()
    assert backend.is_connected is False
    backend._odrv = object()
    assert backend.is_connected is True
    backend._require_connected()


@pytest.mark.parametrize("cls", ALL_BACKENDS)
def test_every_backend_declares_device_and_interval(cls):
    """runner.run() reads both off the backend, so a backend that forgot to
    declare them would silently publish as 'unknown'."""
    assert cls.device != UNKNOWN_DEVICE, f"{cls.__name__} did not declare a device name"
    assert cls.device == cls.device.lower(), "device names become directory names"
    assert cls.sample_interval_s > 0


def test_real_and_mock_odrive_declare_different_intervals():
    """The mock reads in-memory state; the real backend pays a USB round-trip
    per channel, so the two intervals must differ while the device matches."""
    assert OdriveBackend.sample_interval_s > MockOdriveBackend.sample_interval_s
    assert OdriveBackend.device == MockOdriveBackend.device


def test_runner_takes_no_device_wiring():
    """If these reappear as parameters, every entry point has to pass them."""
    import inspect

    from hardware.runner import run

    params = list(inspect.signature(run).parameters)
    assert params == ["backend", "command_endpoint", "telemetry_endpoint"]


@pytest.mark.parametrize("value", [1.5, 7, True, False, "IDLE", None])
def test_to_jsonable_passes_primitives_through(value):
    """Type must survive too: coercing a bool to int would silently change
    what a Bound with expected=True compares against."""
    result = to_jsonable(value)
    assert result == value
    assert type(result) is type(value)


def test_to_jsonable_coerces_enum_like_objects():
    class EnumLike:
        def __int__(self):
            return 3

    assert to_jsonable(EnumLike()) == 3


def test_to_jsonable_falls_back_to_str_rather_than_failing_the_frame():
    """One uncoercible channel must not take the whole telemetry frame down."""
    class Opaque:
        def __int__(self):
            raise TypeError("not a number")

        def __str__(self):
            return "opaque"

    assert to_jsonable(Opaque()) == "opaque"
