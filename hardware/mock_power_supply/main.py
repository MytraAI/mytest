"""Entry point for the power supply hardware driver process. Run it directly with:

    python -m hardware.mock_power_supply.main
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from protocol.wire import DEFAULT_POWER_SUPPLY_COMMAND_ENDPOINT, DEFAULT_POWER_SUPPLY_TELEMETRY_ENDPOINT
from ..runner import run
from .mock_backend import MockPowerSupplyBackend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-endpoint", default=DEFAULT_POWER_SUPPLY_COMMAND_ENDPOINT)
    parser.add_argument("--telemetry-endpoint", default=DEFAULT_POWER_SUPPLY_TELEMETRY_ENDPOINT)
    args = parser.parse_args()
    logger.warning("SIMULATED DEVICE - MockPowerSupplyBackend, no real hardware connected")
    asyncio.run(run(MockPowerSupplyBackend(), args.command_endpoint, args.telemetry_endpoint))
