"""Entry point for the ODrive hardware driver process.

Defaults to the real backend - this folder's whole purpose is talking
to actual ODrive hardware over USB - so no --mock flag means an
attempt to open a real USB connection. Pass --mock to run the
simulated backend instead, for local development without hardware
attached; this is the one device folder in this codebase where a
single entry point picks between real and mock, since a real backend
exists here at all (the other three devices have no real backend, so
their main.py never had a choice to make).

Run with (from the repo root):
    python -m hardware.odrive.main
    python -m hardware.odrive.main --mock
    python -m hardware.odrive.main --serial-number 1234ABCD
    python -m hardware.odrive.main --discovery-timeout 5
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from protocol.wire import DEFAULT_ODRIVE_COMMAND_ENDPOINT, DEFAULT_ODRIVE_TELEMETRY_ENDPOINT, DEVICE_ODRIVE

from ..runner import run
from .mock_backend import SAMPLE_INTERVAL_S as MOCK_SAMPLE_INTERVAL_S
from .mock_backend import MockOdriveBackend
from .odrive_backend import SAMPLE_INTERVAL_S as REAL_SAMPLE_INTERVAL_S
from .odrive_backend import DEFAULT_DISCOVERY_TIMEOUT_S, OdriveBackend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-endpoint", default=DEFAULT_ODRIVE_COMMAND_ENDPOINT)
    parser.add_argument("--telemetry-endpoint", default=DEFAULT_ODRIVE_TELEMETRY_ENDPOINT)
    parser.add_argument("--mock", action="store_true", help="run the simulated backend instead of real USB hardware")
    parser.add_argument(
        "--serial-number", default=None, help="ODrive serial number to connect to, if more than one is attached"
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=DEFAULT_DISCOVERY_TIMEOUT_S,
        help="seconds to wait for odrive.find_any() before giving up (real hardware only)",
    )
    args = parser.parse_args()

    if args.mock:
        logger.warning("SIMULATED DEVICE - MockOdriveBackend, no real hardware connected")
        backend = MockOdriveBackend()
        # The mock streams from in-memory state at 50 Hz; the real backend
        # does a USB round-trip per channel and runs 20 Hz. The publisher's
        # buffer is sized in seconds, so it needs whichever is actually running.
        sample_interval_s = MOCK_SAMPLE_INTERVAL_S
    else:
        backend = OdriveBackend(serial_number=args.serial_number, discovery_timeout_s=args.discovery_timeout)
        sample_interval_s = REAL_SAMPLE_INTERVAL_S

    asyncio.run(
        run(
            backend,
            args.command_endpoint,
            args.telemetry_endpoint,
            device=DEVICE_ODRIVE,
            sample_interval_s=sample_interval_s,
        )
    )
