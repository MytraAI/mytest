"""DUT-specific convenience methods layered on the generic CommandClient."""
from __future__ import annotations

from ..clients.command_client import CommandClient
from protocol.wire import DEFAULT_DUT_COMMAND_ENDPOINT


class DutCommandClient(CommandClient):
    """CommandClient with named sugar for the DUT backend's command channels."""

    def __init__(self, endpoint: str = DEFAULT_DUT_COMMAND_ENDPOINT, timeout_ms: int = 5000):
        super().__init__(endpoint, timeout_ms)

    def set_position_input(self, value: float) -> None:
        self.execute("set_position_input", value=value)

    def set_position_gain(self, value: float) -> None:
        self.execute("set_position_gain", value=value)

    def set_velocity_gain(self, value: float) -> None:
        self.execute("set_velocity_gain", value=value)

    def set_velocity_integrator(self, value: float) -> None:
        self.execute("set_velocity_integrator", value=value)
