"""Entry point for the DUT hardware driver process. Run it directly with:

    python -m hardware.mock_dut.main
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from ..protocol import DEFAULT_DUT_COMMAND_ENDPOINT, DEFAULT_DUT_TELEMETRY_ENDPOINT
from ..runner import run
from .mock_backend import MockDutBackend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-endpoint", default=DEFAULT_DUT_COMMAND_ENDPOINT)
    parser.add_argument("--telemetry-endpoint", default=DEFAULT_DUT_TELEMETRY_ENDPOINT)
    args = parser.parse_args()
    logger.warning("SIMULATED DEVICE - MockDutBackend, no real hardware connected")
    asyncio.run(run(MockDutBackend(), args.command_endpoint, args.telemetry_endpoint))
