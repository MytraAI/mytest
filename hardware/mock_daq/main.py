"""Entry point for the DAQ hardware driver process. Run it directly with:

    python -m hardware.mock_daq.main
"""
from __future__ import annotations

import argparse
import logging

from protocol.wire import DEFAULT_COMMAND_ENDPOINT, DEFAULT_TELEMETRY_ENDPOINT, DEVICE_DAQ
from ..driver_logging import add_logging_args, configure as configure_logging
from ..runner import run
from protocol import asyncio_compat
from .mock_backend import MockDaqBackend

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    add_logging_args(parser)
    parser.add_argument("--command-endpoint", default=DEFAULT_COMMAND_ENDPOINT)
    parser.add_argument("--telemetry-endpoint", default=DEFAULT_TELEMETRY_ENDPOINT)
    args = parser.parse_args()
    configure_logging(args.log_file, device=DEVICE_DAQ)
    logger.warning("SIMULATED DEVICE - MockDaqBackend, no real hardware connected")
    asyncio_compat.run(run(MockDaqBackend(), args.command_endpoint, args.telemetry_endpoint))
