"""Entry point for the Keysight N6974A hardware driver process.

Always talks to the real instrument - this driver has no mock backend, so there
is no --mock flag and no way to run it without a supply reachable at --host.
Tests substitute a fake transport instead (tests/test_n6974a.py), which
exercises the real message building and parsing rather than replacing it.

Run with (from the repo root):
    python -m hardware.n6974a.main --dissipators 1
    python -m hardware.n6974a.main --dissipators 1 --host 169.254.236.129
    python -m hardware.n6974a.main --dissipators 0

--dissipators is required. It is how many Keysight N7909A power dissipator
units are connected, and it decides how much current this supply may sink:
none means 10% of its rating, one means 50%, two means 100%. There is no safe
default, because guessing low would refuse sinking the stand can really do and
guessing high would license discharging a device under test harder than the
hardware can absorb. The value is checked against the instrument at connect and
a mismatch refuses to start - note an N7909A is only recognised at power-on, so
one cabled to a running supply reads as absent.

--host defaults to the instrument on this stand, but that address is link-local
and self-assigned, so it is not guaranteed stable; a testbed is expected to pass
one from its own config.

Nothing here configures the output. connect() is passive: it does not enable the
output, does not disable one it finds on, sets no protection level and arms no
watchdog. Setpoints and protection are a test's decisions, sent as commands.
"""
from __future__ import annotations

import argparse
import logging

from protocol.wire import (
    DEFAULT_N6974A_COMMAND_ENDPOINT,
    DEFAULT_N6974A_TELEMETRY_ENDPOINT,
    DEVICE_N6974A,
)

from ..driver_logging import add_logging_args, configure as configure_logging
from ..runner import run
from protocol import asyncio_compat
from .n6974a_backend import DEFAULT_N6974A_HOST, N6974aBackend
from .n6974a_channels import SINK_FRACTION_BY_DISSIPATORS
from .transport import DEFAULT_PORT

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    add_logging_args(parser)
    parser.add_argument("--command-endpoint", default=DEFAULT_N6974A_COMMAND_ENDPOINT)
    parser.add_argument("--telemetry-endpoint", default=DEFAULT_N6974A_TELEMETRY_ENDPOINT)
    parser.add_argument(
        "--host", default=DEFAULT_N6974A_HOST,
        help="instrument address (link-local by default - see module docstring)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="raw SCPI socket port on the instrument")
    parser.add_argument(
        "--dissipators", type=int, required=True, choices=sorted(SINK_FRACTION_BY_DISSIPATORS),
        help="how many N7909A power dissipators are connected; sets how much current this supply "
             "may sink, and is verified against the instrument at connect",
    )
    args = parser.parse_args()
    configure_logging(args.log_file, device=DEVICE_N6974A)

    backend = N6974aBackend(host=args.host, port=args.port, dissipators=args.dissipators)
    logger.info(
        "REAL HARDWARE - N6974A at %s:%s, %d N7909A dissipator(s) declared (sink limit %.0f%% of rating)",
        args.host, args.port, args.dissipators,
        SINK_FRACTION_BY_DISSIPATORS[args.dissipators] * 100,
    )

    asyncio_compat.run(run(backend, args.command_endpoint, args.telemetry_endpoint))
