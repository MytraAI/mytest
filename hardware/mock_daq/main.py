"""Entry point for the DAQ hardware driver process. Run it directly with:

    python -m hardware.mock_daq.main
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from protocol.wire import DEFAULT_COMMAND_ENDPOINT, DEFAULT_TELEMETRY_ENDPOINT
from ..runner import run
from .mock_backend import MockDaqBackend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-endpoint", default=DEFAULT_COMMAND_ENDPOINT)
    parser.add_argument("--telemetry-endpoint", default=DEFAULT_TELEMETRY_ENDPOINT)
    args = parser.parse_args()
    logger.warning("SIMULATED DEVICE - MockDaqBackend, no real hardware connected")
    asyncio.run(run(MockDaqBackend(), args.command_endpoint, args.telemetry_endpoint))
