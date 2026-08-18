"""Entry point for the power supply hardware driver process. Run it directly with:

    python -m hardware.mock_power_supply.main
"""
from __future__ import annotations

import argparse
import logging

from protocol.wire import (
    DEFAULT_POWER_SUPPLY_COMMAND_ENDPOINT,
    DEFAULT_POWER_SUPPLY_TELEMETRY_ENDPOINT,
    DEVICE_POWER_SUPPLY,
)
from ..driver_logging import add_logging_args, configure as configure_logging
from ..runner import run
from protocol import asyncio_compat
from .mock_backend import MockPowerSupplyBackend

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    add_logging_args(parser)
    parser.add_argument("--command-endpoint", default=DEFAULT_POWER_SUPPLY_COMMAND_ENDPOINT)
    parser.add_argument("--telemetry-endpoint", default=DEFAULT_POWER_SUPPLY_TELEMETRY_ENDPOINT)
    args = parser.parse_args()
    configure_logging(args.log_file, device=DEVICE_POWER_SUPPLY)
    logger.warning("SIMULATED DEVICE - MockPowerSupplyBackend, no real hardware connected")
    asyncio_compat.run(run(MockPowerSupplyBackend(), args.command_endpoint, args.telemetry_endpoint))
