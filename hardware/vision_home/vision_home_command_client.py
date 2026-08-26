"""Named-method wrapper over the vision-home driver's command surface, the same
thin façade OdriveCommandClient and Cpx400dpCommandClient are.

Nothing here decides anything: teaching is an operator's act and the threshold
is a tuning value. When a match should be believed is the test's call, taken
from the telemetry - see vision_home_channels.py.
"""
from __future__ import annotations

from hardware.clients.command_client import CommandClient


class VisionHomeCommandClient(CommandClient):
    def teach(self) -> dict:
        """Take the camera's current view as the reference. The fixture has to actually be
        at the marker, which nothing here can check - hence an operator step."""
        return self.execute("teach")

    def select_best_camera(self, max_index: int = 6) -> dict:
        """Pick the camera that can see the reference view. Returns the choice, its score and
        what every candidate scored, so a run records the choice was decisive."""
        return self.execute("select_best_camera", max_index=max_index)
