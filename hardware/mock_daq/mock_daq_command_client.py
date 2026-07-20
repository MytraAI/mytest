"""DAQ-specific convenience methods layered on the generic CommandClient."""
from __future__ import annotations

from typing import List

from ..clients.command_client import CommandClient


class DaqCommandClient(CommandClient):
    """CommandClient with named sugar for the DAQ backend's actions."""

    def get_channel_list(self) -> List[str]:
        return self.execute("get_channel_list")

    def load_setup(self, setup_name: str) -> None:
        self.execute("load_setup", setup_name=setup_name)

    def start_acquisition(self, test_id: str) -> None:
        self.execute("start_acquisition", test_id=test_id)

    def stop_acquisition(self) -> None:
        self.execute("stop_acquisition")

    def set_digital_output(self, channel: str, state: bool) -> None:
        self.execute("set_digital_output", channel=channel, state=state)

    def trigger_event(self, name: str) -> None:
        self.execute("trigger_event", name=name)
