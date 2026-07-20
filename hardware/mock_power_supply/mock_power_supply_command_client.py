"""Power-supply-specific convenience methods layered on the generic CommandClient."""
from __future__ import annotations

from ..clients.command_client import CommandClient
from ..protocol import DEFAULT_POWER_SUPPLY_COMMAND_ENDPOINT


class PowerSupplyCommandClient(CommandClient):
    """CommandClient with named sugar for the power supply backend's actions."""

    def __init__(self, endpoint: str = DEFAULT_POWER_SUPPLY_COMMAND_ENDPOINT, timeout_ms: int = 5000):
        super().__init__(endpoint, timeout_ms)

    def set_output(self, voltage: float, current: float) -> None:
        self.execute("set_output", voltage=voltage, current=current)

    def enable_output(self, enabled: bool) -> None:
        self.execute("enable_output", enabled=enabled)
