"""Entry point for the DUT hardware driver process. Run it directly with:

    python -m hardware.mock_dut.main
"""
from __future__ import annotations

import argparse
import logging

from protocol.wire import DEFAULT_DUT_COMMAND_ENDPOINT, DEFAULT_DUT_TELEMETRY_ENDPOINT, DEVICE_DUT
from ..driver_logging import add_logging_args, configure as configure_logging
from ..runner import run
from protocol import asyncio_compat
from .mock_backend import MockDutBackend

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    add_logging_args(parser)
    parser.add_argument("--command-endpoint", default=DEFAULT_DUT_COMMAND_ENDPOINT)
    parser.add_argument("--telemetry-endpoint", default=DEFAULT_DUT_TELEMETRY_ENDPOINT)
    args = parser.parse_args()
    configure_logging(args.log_file, device=DEVICE_DUT)
    logger.warning("SIMULATED DEVICE - MockDutBackend, no real hardware connected")
    asyncio_compat.run(run(MockDutBackend(), args.command_endpoint, args.telemetry_endpoint))
