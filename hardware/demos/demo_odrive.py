"""ODrive smoke test.

Launches a hardware driver process running the simulated ODrive
backend, connects, verifies the declared command/telemetry channel
surface against the live process, commands velocity control, then
reads back telemetry as the simulated axis spins up, and tears
everything down.

Always uses --mock - this script never needs (or attempts) a real USB
connection, matching every other demo_*.py here. To exercise the real
backend instead, run `python -m hardware.odrive.main` directly (see
hardware/README.md).

Run with (from the repo root, Mytest/): python -m hardware.demos.demo_odrive
"""
from __future__ import annotations

import subprocess
import sys
import time

from ..clients.telemetry_client import TelemetryClient
from ..odrive.odrive_channels import COMMAND_CHANNELS, TELEMETRY_CHANNELS
from ..odrive.odrive_command_client import OdriveCommandClient
from protocol.wire import DEFAULT_ODRIVE_COMMAND_ENDPOINT, DEFAULT_ODRIVE_TELEMETRY_ENDPOINT


def main():
    driver = subprocess.Popen([sys.executable, "-m", "hardware.odrive.main", "--mock"])
    try:
        time.sleep(0.5)  # let the driver bind its sockets

        cmd = OdriveCommandClient(endpoint=DEFAULT_ODRIVE_COMMAND_ENDPOINT)
        print("connect:", cmd.connect_backend())
        cmd.verify_actions(COMMAND_CHANNELS)
        print(f"verify_actions: all {len(COMMAND_CHANNELS)} declared command channels present")
        print("status (before commands):", cmd.get_status())

        telem = TelemetryClient(endpoint=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
        telem.verify_channels(TELEMETRY_CHANNELS)
        print(f"verify_channels: all {len(TELEMETRY_CHANNELS)} declared telemetry channels present")

        frames = telem.frames()
        print("reading 3 frames before any command (should sit idle at 0)...")
        for _ in range(3):
            frame = next(frames)
            print(frame.seq, round(frame.t, 3), frame.channels["axis_current_state"], frame.channels["vel_estimate"])

        print("set_control_mode:", cmd.set_control_mode("VELOCITY_CONTROL"))
        print("set_axis_state:", cmd.set_axis_state("CLOSED_LOOP_CONTROL"))
        print("set_velocity:", cmd.set_velocity(5.0))
        print("status (after commands):", cmd.get_status())

        print("reading 20 frames as velocity spins up...")
        for _ in range(20):
            frame = next(frames)
            print(frame.seq, round(frame.t, 3), frame.channels["axis_current_state"], frame.channels["vel_estimate"])
        telem.close()

        print("set_axis_state(IDLE):", cmd.set_axis_state("IDLE"))
        print("disconnect:", cmd.disconnect_backend())
        cmd.close()
    finally:
        driver.terminate()
        driver.wait(timeout=5)


if __name__ == "__main__":
    main()
