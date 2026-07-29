"""Power supply smoke test.

Launches a hardware driver process running the power supply backend,
connects, sets an output, enables it, reads a handful of telemetry
frames, then disables and tears everything down.

Proves the same command/telemetry server code drives a completely
different device than the DAQ with zero server-side changes - only
the backend and command client differ.

Run with (from the repo root, Mytest/): python -m hardware.demos.demo_power_supply
"""
from __future__ import annotations

import subprocess
import sys
import time

from ..clients.telemetry_client import TelemetryClient
from ..mock_power_supply.mock_power_supply_command_client import PowerSupplyCommandClient
from protocol.wire import DEFAULT_POWER_SUPPLY_COMMAND_ENDPOINT, DEFAULT_POWER_SUPPLY_TELEMETRY_ENDPOINT


def main():
    driver = subprocess.Popen([sys.executable, "-m", "hardware.mock_power_supply.main"])
    try:
        time.sleep(0.5)  # let the driver bind its sockets

        cmd = PowerSupplyCommandClient(endpoint=DEFAULT_POWER_SUPPLY_COMMAND_ENDPOINT)
        print("connect:", cmd.connect_backend())
        print("set_output:", cmd.set_output(voltage=12.0, current=0.5))
        print("status (output disabled):", cmd.get_status())

        telem = TelemetryClient(endpoint=DEFAULT_POWER_SUPPLY_TELEMETRY_ENDPOINT)
        print("reading 3 frames with output disabled...")
        frames = telem.frames()
        for _ in range(3):
            frame = next(frames)
            print(frame.seq, round(frame.t, 3), frame.channels)

        print("enable_output:", cmd.enable_output(True))
        print("status (output enabled):", cmd.get_status())

        print("reading 5 frames with output enabled...")
        for _ in range(5):
            frame = next(frames)
            print(frame.seq, round(frame.t, 3), frame.channels)
        telem.close()

        print("enable_output(False):", cmd.enable_output(False))
        print("disconnect:", cmd.disconnect_backend())
        cmd.close()
    finally:
        driver.terminate()
        driver.wait(timeout=5)


if __name__ == "__main__":
    main()
