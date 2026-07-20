"""End-to-end smoke test.

Launches the hardware driver process, drives it through connect ->
load setup -> start acquisition via the command client, reads a
handful of telemetry frames, then stops and tears everything down.

Run with (from the repo root, Mytest/): python -m hardware.demo_end_to_end
"""
from __future__ import annotations

import subprocess
import sys
import time

from .clients.telemetry_client import TelemetryClient
from .mock_daq.mock_daq_command_client import DaqCommandClient


def main():
    driver = subprocess.Popen([sys.executable, "-m", "hardware.mock_daq.main"])
    try:
        time.sleep(0.5)  # let the driver bind its sockets

        cmd = DaqCommandClient()
        print("channels:", cmd.get_channel_list())
        print("load_setup:", cmd.load_setup("default_setup"))
        print("start_acquisition:", cmd.start_acquisition(test_id="demo-001"))
        print("status:", cmd.get_status())

        telem = TelemetryClient()
        print("reading 5 telemetry frames...")
        for i, frame in enumerate(telem.frames()):
            print(frame.seq, round(frame.t, 3), frame.channels)
            if i >= 4:
                break
        telem.close()

        print("stop_acquisition:", cmd.stop_acquisition())
        cmd.close()
    finally:
        driver.terminate()
        driver.wait(timeout=5)


if __name__ == "__main__":
    main()
