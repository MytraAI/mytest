"""HardwareBackend for the 8-channel thermocouple DAQ.

The whole device is one direction: it streams CSV lines and takes no commands.
That makes this the simplest real backend here, and moves all of the care into
two places.

WHAT ARRIVES IS VALIDATED, NOT TRUSTED. Opening the port succeeds whatever baud
rate is set, and a wrong one delivers bytes that decode into plausible-looking
text - so connect() reads a few lines and checks their shape, and raises naming
what it actually saw. Without that, a misconfigured link would publish garbage
under real channel names for a whole run.

A BAD LINE IS COUNTED, NOT FATAL. Mid-stream, a line that will not parse is
logged, counted into `malformed_lines`, and skipped: one garbled line on a
marginal cable must not end a run that may be hours in. What is fatal is
silence, which the transport raises on, because on a device with no query
silence is the only symptom a dead link has.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from protocol.wire import DEVICE_TC_DAQ

from ..backend import HardwareBackend, HardwareError
from .tc_daq_channels import (
    CHANNEL_COUNT,
    COMMAND_CHANNELS,
    FAULT_TOKEN,
    TELEMETRY_CHANNELS,
)
from .transport import DEFAULT_BAUD, DEFAULT_PORT, SerialLineTransport

logger = logging.getLogger(__name__)

SAMPLE_RATE_HZ = 9.0
"""The rate the device streams at, measured. Not settable - there is no command
to change it, and nothing here paces the stream: a read simply blocks until the
next line arrives."""

PROBE_LINES = 3
"""Lines read at connect() to confirm the shape of what is arriving. More than
one, so a single fragment that survived the partial-line discard cannot pass the
check on its own."""

MALFORMED_LINE_LOG_LIMIT = 5
"""How many bad lines are logged in full before the driver stops repeating
itself. `malformed_lines` keeps counting past this - a device streaming nothing
but garbage would otherwise fill the log faster than anything could read it."""


class TcDaqBackend(HardwareBackend):
    """Streams eight thermocouple temperatures. Accepts no commands."""

    device = DEVICE_TC_DAQ
    sample_interval_s = 1.0 / SAMPLE_RATE_HZ
    """Declared so the telemetry publisher can size its high-water mark in
    seconds of buffer. Unlike every other backend here it is not a sleep:
    stream_samples() blocks on the device's own next line."""

    def __init__(
        self,
        port: str = DEFAULT_PORT,
        baud: int = DEFAULT_BAUD,
        transport: Optional[SerialLineTransport] = None,
    ) -> None:
        self._transport = transport if transport is not None else SerialLineTransport(port, baud)
        self._malformed_lines = 0
        self._malformed_logged = 0

    @property
    def is_connected(self) -> bool:
        return self._transport.is_open

    async def connect(self) -> None:
        """Open the port and confirm the device is streaming the expected shape.

        Idempotent: `runner.run()` connects when the driver process starts and a
        client may then call `connect` over the wire, so a second call has to be
        a no-op rather than opening the port twice."""
        if self.is_connected:
            logger.debug("already connected to %s", self._transport.address)
            return

        await self._transport.open()
        try:
            await self._transport.discard_partial_line()
            for _ in range(PROBE_LINES):
                line = await self._transport.read_line()
                self._parse_line(line, strict=True)
        except Exception:
            # Do not leave the port open on a link this driver has just decided
            # it cannot read - the next attempt would fail to open it.
            await self._transport.close()
            raise

        logger.info(
            "connected to the TC DAQ at %s - %d channels at ~%.0f Hz",
            self._transport.address, CHANNEL_COUNT, SAMPLE_RATE_HZ,
        )

    async def disconnect(self) -> None:
        await self._transport.close()

    async def get_status(self) -> dict:
        self._require_connected()
        return {
            "connected": True,
            "address": self._transport.address,
            "channel_count": CHANNEL_COUNT,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "malformed_lines": self._malformed_lines,
        }

    async def list_actions(self) -> List[str]:
        return list(COMMAND_CHANNELS)

    async def execute(self, action: str, **params: Any) -> Any:
        raise HardwareError(
            f"the TC DAQ accepts no commands, so there is nothing to do for {action!r}: "
            "it streams temperatures and takes no input. Units, thermocouple type and "
            "sample rate are set on the device itself, not from here"
        )

    async def stream_samples(self) -> AsyncIterator[dict]:
        while True:
            line = await self._transport.read_line()
            frame = self._parse_line(line)
            if frame is not None:
                yield frame

    def _parse_line(self, line: str, strict: bool = False) -> Optional[Dict[str, Any]]:
        """One CSV line into one frame, or None if it could not be read.

        `strict` raises instead of returning None, for connect()'s check: a bad
        line before a run starts means the wrong baud rate or the wrong device,
        which is worth refusing to start over. The same line mid-run is worth
        skipping and counting."""
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != CHANNEL_COUNT:
            return self._reject(
                line, f"expected {CHANNEL_COUNT} comma-separated fields, got {len(fields)}", strict
            )

        temperatures: List[Optional[float]] = []
        for field in fields:
            if field.upper() == FAULT_TOKEN:
                temperatures.append(None)
                continue
            try:
                temperatures.append(float(field))
            except ValueError:
                return self._reject(line, f"{field!r} is neither a number nor {FAULT_TOKEN}", strict)

        frame: Dict[str, Any] = {}
        for index, value in enumerate(temperatures, start=1):
            frame[f"temperature_{index}_c"] = value
            frame[f"fault_{index}"] = value is None
        frame["fault_count"] = sum(1 for value in temperatures if value is None)
        frame["malformed_lines"] = self._malformed_lines
        return frame

    def _reject(self, line: str, why: str, strict: bool) -> None:
        """Account for a line that could not be read, and decide its fate."""
        if strict:
            raise HardwareError(
                f"{self._transport.address} is not streaming what this driver expects: {why}. "
                f"Line was {line!r}. Check the baud rate, and that this port is the TC DAQ"
            )
        self._malformed_lines += 1
        if self._malformed_logged < MALFORMED_LINE_LOG_LIMIT:
            self._malformed_logged += 1
            logger.warning("skipping an unreadable line (%s): %r", why, line)
            if self._malformed_logged == MALFORMED_LINE_LOG_LIMIT:
                logger.warning(
                    "further unreadable lines will be counted in malformed_lines without logging"
                )
        return None


def _validate_channel_coverage() -> None:
    """Fail at import if the declared channels and what _parse_line builds have
    drifted apart, the way the ODrive and CPX400DP backends check their own
    tables. A frame missing a declared channel would otherwise surface as a
    MissingChannelError in whichever test first ran against the device."""
    built = {f"temperature_{n}_c" for n in range(1, CHANNEL_COUNT + 1)}
    built |= {f"fault_{n}" for n in range(1, CHANNEL_COUNT + 1)}
    built |= {"fault_count", "malformed_lines"}
    declared = set(TELEMETRY_CHANNELS)
    if built != declared:
        raise AssertionError(
            "TC DAQ channel drift - "
            f"declared but never built: {sorted(declared - built)}, "
            f"built but not declared: {sorted(built - declared)}"
        )


_validate_channel_coverage()
