"""Entry point for the TC DAQ driver process.

    python -m hardware.tc_daq.main --list-ports
    python -m hardware.tc_daq.main --port /dev/cu.usbserial-21210
    python -m hardware.tc_daq.main --port COM4

--port has a default, but a USB serial port's name is assigned by the OS and
changes with enumeration order and platform, so a testbed passes its stand's
own. --list-ports prints what this machine currently has, with the USB
description and serial number of each, since the device announces itself only as
the USB-UART bridge it sits behind and there is no way to ask a port what is on
the other end of it.

There is no --mock. The device streams a documented eight-field line and nothing
else, so a fake serial transport in the tests covers the whole surface (see
tests/test_tc_daq.py) - a mock backend would be a second implementation of the
one thing worth testing, the parsing.
"""
from __future__ import annotations

import argparse
import logging
import sys

from protocol import asyncio_compat
from protocol.wire import (
    DEFAULT_TC_DAQ_COMMAND_ENDPOINT,
    DEFAULT_TC_DAQ_TELEMETRY_ENDPOINT,
    DEVICE_TC_DAQ,
)

from ..driver_logging import add_logging_args, configure as configure_logging
from ..runner import run
from .tc_daq_backend import TcDaqBackend
from .transport import DEFAULT_BAUD, DEFAULT_PORT

logger = logging.getLogger(__name__)


def list_ports() -> int:
    """Print this machine's serial ports, or say why it cannot."""
    try:
        from serial.tools import list_ports as enumerate_ports
    except ImportError:
        print("pyserial is not installed - run `uv sync`", file=sys.stderr)
        return 1
    ports = sorted(enumerate_ports.comports(), key=lambda port: port.device)
    if not ports:
        print("no serial ports found")
        return 1
    for port in ports:
        print(f"{port.device}\t{port.description}\t{port.serial_number or '-'}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=DEFAULT_PORT, help="serial port the device is on")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument(
        "--list-ports", action="store_true", help="print this machine's serial ports and exit"
    )
    add_logging_args(parser)
    args = parser.parse_args()

    if args.list_ports:
        sys.exit(list_ports())

    configure_logging(args.log_file, device=DEVICE_TC_DAQ)
    backend = TcDaqBackend(port=args.port, baud=args.baud)
    logger.info("REAL HARDWARE - TC DAQ at %s@%s", args.port, args.baud)
    asyncio_compat.run(
        run(backend, DEFAULT_TC_DAQ_COMMAND_ENDPOINT, DEFAULT_TC_DAQ_TELEMETRY_ENDPOINT)
    )
