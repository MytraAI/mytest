"""DUT smoke test.

Launches a hardware driver process running the DUT backend, connects,
sets a position setpoint and gains, then reads back position/velocity/
current as the simulated servo approaches the setpoint, and tears
everything down.

Proves the same command/telemetry server code drives a third, still
completely different device - a control loop, not just a readback -
with zero server-side changes.

Run with (from the repo root, Mytest/): python -m hardware.demos.demo_dut
"""
from __future__ import annotations

import subprocess
import sys
import time

from ..clients.telemetry_client import TelemetryClient
from ..mock_dut.mock_dut_command_client import DutCommandClient
from protocol.wire import DEFAULT_DUT_COMMAND_ENDPOINT, DEFAULT_DUT_TELEMETRY_ENDPOINT


def main():
    driver = subprocess.Popen([sys.executable, "-m", "hardware.mock_dut.main"])
    try:
        time.sleep(0.5)  # let the driver bind its sockets

        cmd = DutCommandClient(endpoint=DEFAULT_DUT_COMMAND_ENDPOINT)
        print("connect:", cmd.connect_backend())
        print("status (before commands):", cmd.get_status())

        telem = TelemetryClient(endpoint=DEFAULT_DUT_TELEMETRY_ENDPOINT)
        frames = telem.frames()
        print("reading 3 frames before any command (should sit at 0)...")
        for _ in range(3):
            frame = next(frames)
            print(frame.seq, round(frame.t, 3), frame.channels)

        print("set_position_input:", cmd.set_position_input(10.0))
        print("set_position_gain:", cmd.set_position_gain(2.0))
        print("set_velocity_gain:", cmd.set_velocity_gain(5.0))
        print("set_velocity_integrator:", cmd.set_velocity_integrator(0.1))
        print("status (after commands):", cmd.get_status())

        print("reading 20 frames as position approaches setpoint...")
        for _ in range(20):
            frame = next(frames)
            print(frame.seq, round(frame.t, 3), frame.channels)
        telem.close()

        print("disconnect:", cmd.disconnect_backend())
        cmd.close()
    finally:
        driver.terminate()
        driver.wait(timeout=5)


if __name__ == "__main__":
    main()
