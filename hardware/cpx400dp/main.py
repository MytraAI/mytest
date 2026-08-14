"""Entry point for the CPX400DP hardware driver process.

Always talks to the real instrument - this driver has no mock backend, so
there is no --mock flag and no way to run it without a supply reachable at
--host. Tests substitute a fake transport instead (tests/test_cpx400dp.py),
which exercises the real parsing code rather than replacing it.

Run with (from the repo root):
    python -m hardware.cpx400dp.main
    python -m hardware.cpx400dp.main --host 169.254.229.133
    python -m hardware.cpx400dp.main --max-voltage 15 --max-current 2
    python -m hardware.cpx400dp.main --interface-lock

--host defaults to the instrument on this stand, but that address is
link-local and self-assigned, so it is not guaranteed stable; a testbed is
expected to pass one from its own config.

--max-voltage/--max-current are the driver-side ceiling. They change nothing
on the instrument: they make this process refuse to *command* a setpoint above
them. Worth setting to whatever the load can survive, since the instrument
itself will happily accept any value inside its own 60 V / 20 A range.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from protocol.wire import DEFAULT_CPX400DP_COMMAND_ENDPOINT, DEFAULT_CPX400DP_TELEMETRY_ENDPOINT

from ..runner import run
from .cpx400dp_backend import DEFAULT_CPX400DP_HOST, Cpx400dpBackend
from .transport import DEFAULT_PORT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-endpoint", default=DEFAULT_CPX400DP_COMMAND_ENDPOINT)
    parser.add_argument("--telemetry-endpoint", default=DEFAULT_CPX400DP_TELEMETRY_ENDPOINT)
    parser.add_argument(
        "--host", default=DEFAULT_CPX400DP_HOST, help="instrument address (link-local by default - see module docstring)"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="raw socket port on the instrument")
    parser.add_argument(
        "--max-voltage", type=float, default=None, help="refuse to command a voltage setpoint above this (volts)"
    )
    parser.add_argument(
        "--max-current", type=float, default=None, help="refuse to command a current setpoint above this (amps)"
    )
    parser.add_argument(
        "--interface-lock",
        action="store_true",
        help="take exclusive control (IFLOCK) at connect, so the web page and VXI-11 cannot change settings mid-run",
    )
    args = parser.parse_args()

    backend = Cpx400dpBackend(
        host=args.host,
        port=args.port,
        max_voltage=args.max_voltage,
        max_current=args.max_current,
        take_interface_lock=args.interface_lock,
    )
    logger.info(
        "REAL HARDWARE - CPX400DP at %s:%s (max_voltage=%s, max_current=%s)",
        args.host, args.port, args.max_voltage, args.max_current,
    )

    asyncio.run(run(backend, args.command_endpoint, args.telemetry_endpoint))
