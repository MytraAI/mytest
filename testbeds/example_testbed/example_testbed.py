"""Physical testbed for example_dut.

Starts and stops the hardware driver processes this DUT's tests run
against - a DAQ and a power supply - and owns a ready-to-use command
client for the power supply, so a test case only has to instantiate
this one object to get control of its supporting test hardware. These
are the instruments/fixtures surrounding the DUT, not the DUT itself.

Controlling the DUT directly is a separate concern owned by ExampleDut
(see ../../testcases/example_dut/dut/example_dut.py), not this testbed -
this testbed only owns bringing the supporting hardware up and down
around it.
"""
from __future__ import annotations

import subprocess
import sys
import time
from typing import List, Optional

from hardware.mock_power_supply.mock_power_supply_command_client import PowerSupplyCommandClient

STARTUP_DELAY_S = 0.5


class ExampleTestbed:
    """Starts/stops the DAQ and power supply hardware driver processes for example_dut, and owns a connected power supply command client.

    Use as a context manager:

        with ExampleTestbed() as testbed:
            testbed.power_supply.set_output(voltage=24.0, current=2.0)
            testbed.power_supply.enable_output(True)
            ...  # drivers are up, and the power supply is controllable, for the duration of this block
    """

    def __init__(self) -> None:
        self._processes: List[subprocess.Popen] = []
        self.power_supply: Optional[PowerSupplyCommandClient] = None

    def start(self) -> None:
        self._processes = [
            subprocess.Popen([sys.executable, "-m", "hardware.mock_daq.main"]),
            subprocess.Popen([sys.executable, "-m", "hardware.mock_power_supply.main"]),
        ]
        time.sleep(STARTUP_DELAY_S)  # let both drivers bind their sockets

        self.power_supply = PowerSupplyCommandClient()
        self.power_supply.connect_backend()

    def stop(self) -> None:
        if self.power_supply is not None:
            self.power_supply.enable_output(False)  # safe even if it was never enabled
            self.power_supply.disconnect_backend()
            self.power_supply.close()
            self.power_supply = None

        for process in self._processes:
            process.terminate()
        for process in self._processes:
            process.wait(timeout=5)
        self._processes = []

    def __enter__(self) -> "ExampleTestbed":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
