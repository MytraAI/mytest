"""The TC DAQ's serial line protocol, isolated from the backend.

The device streams one ASCII line per sample and accepts nothing: no query, no
handshake, no configuration. Open the port and lines arrive, CRLF-terminated,
at about 9 Hz.

Isolated into its own class for the same reason the CPX400DP's transport is:
it is the only part of this driver that touches a port, so tests substitute a
fake device and still exercise the real parsing above it (see
tests/test_tc_daq.py).

READS RUN IN A THREAD. pyserial is blocking and has no asyncio support, so each
read is handed to a worker thread rather than stalling the event loop the
command server shares.

SILENCE IS A DEAD DEVICE. There is no command to probe with, so the only
evidence the link is alive is a line arriving. A read that times out repeatedly
is therefore reported as an error rather than as "no data yet": at 9 Hz a
multi-second gap means the cable is out or the device has stopped, and a driver
that quietly published nothing would leave a test believing its temperatures
were simply unchanging.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..backend import HardwareError

logger = logging.getLogger(__name__)

DEFAULT_PORT = "/dev/cu.usbserial-21210"
"""Where the device was found on the machine this driver was written on. Not
stable: a USB serial port's name depends on the OS and the enumeration order
(`COM<n>` on Windows, `/dev/ttyUSB<n>` on Linux), so a testbed passes its own
`--port`. `python -m hardware.tc_daq.main --list-ports` prints the candidates."""

DEFAULT_BAUD = 115200
"""Confirmed against the device. Worth stating because a wrong baud rate here
does not fail loudly - the port opens and delivers bytes that decode into
plausible-looking garbage, which is why connect() validates the shape of what
arrives rather than trusting that reading succeeded."""

READ_TIMEOUT_S = 1.0
"""How long one read waits for a line. Comfortably longer than the ~110 ms
between samples, so a normal read never times out."""

SILENCE_TIMEOUT_S = 10.0
"""How long the stream may be silent before the link is called dead - about 90
missed samples at 9 Hz.

Patient on purpose. Losing the temperature stream stops a run, and a gap of a
few seconds is not proof of a broken link: a USB serial port can stall while the
host is busy, and this is the one device here whose readings change slowly enough
that a short gap costs nothing. What it must still catch is a cable pulled out,
which never recovers."""


class SerialLineTransport:
    """Reads CRLF-terminated lines from the device's serial port."""

    def __init__(
        self,
        port: str = DEFAULT_PORT,
        baud: int = DEFAULT_BAUD,
        read_timeout_s: float = READ_TIMEOUT_S,
        silence_timeout_s: float = SILENCE_TIMEOUT_S,
    ) -> None:
        self._port = port
        self._baud = baud
        self._read_timeout_s = read_timeout_s
        self._silence_timeout_s = silence_timeout_s
        self._serial: Optional[object] = None

    @property
    def address(self) -> str:
        return f"{self._port}@{self._baud}"

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    async def open(self) -> None:
        """Open the port.

        pyserial is imported here rather than at module load, so this module -
        and --list-ports - work on a machine without it installed, the same way
        the ODrive backend defers its own SDK import."""
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - the dependency is declared
            raise HardwareError("pyserial is not installed") from exc
        try:
            self._serial = serial.Serial(self._port, self._baud, timeout=self._read_timeout_s)
        except Exception as exc:
            raise HardwareError(f"could not open {self.address}: {exc}") from exc
        logger.info("opened %s", self.address)

    async def close(self) -> None:
        """Close the port, tolerating a port that was never opened or is
        already gone - this runs on the teardown path."""
        port, self._serial = self._serial, None
        if port is None:
            return
        try:
            await asyncio.to_thread(port.close)
        except Exception:
            logger.warning("closing %s failed, continuing", self.address, exc_info=True)

    async def read_line(self) -> str:
        """The next line, without its terminator.

        Retries a timed-out read until SILENCE_TIMEOUT_S has passed with
        nothing arriving, then raises: on a device that only streams, silence is
        the one symptom a dead link has."""
        if self._serial is None:
            raise HardwareError(f"{self.address} is not open")
        waited = 0.0
        while True:
            raw = await asyncio.to_thread(self._serial.readline)
            if raw:
                return raw.decode("ascii", errors="replace").strip()
            waited += self._read_timeout_s
            if waited >= self._silence_timeout_s:
                raise HardwareError(
                    f"{self.address} has been silent for {waited:.0f}s - this device streams "
                    "continuously, so nothing arriving means the link is down"
                )

    async def discard_partial_line(self) -> None:
        """Read and throw away whatever is mid-flight.

        The device is mid-sentence when the port opens, so the first line is
        very likely a fragment - and a fragment parses as the wrong number of
        fields, which would otherwise look exactly like the wrong baud rate."""
        await self.read_line()
