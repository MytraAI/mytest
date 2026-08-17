"""The CPX400DP's raw-socket line protocol, isolated from the backend.

Commands are ASCII, terminated with LF (0AH); responses come back terminated
with CR LF (0DH 0AH). That is the whole wire format - see the instruction
manual's "Remote Commands" section.

Isolated into its own class for two reasons. It is the only part of this
driver that touches a socket, so tests can substitute a fake instrument and
still exercise the real parsing and command-formatting code above it (see
tests/test_cpx400dp.py). And the serialization requirement below is a property
of the link, not of any particular command.

ONE CONNECTION, ONE COMMAND AT A TIME. The instrument accepts a single raw
socket on port 9221 - a second connection while one is open is refused outright.
Its parser "will not start a new command until any previous command or query is
complete", so the telemetry poll and every operator command share one strictly
serial link. `_lock` is what makes that safe: it makes each
transaction atomic, so a command's reply can never be delivered to the
telemetry loop's read, or vice versa. Callers that need several exchanges to be
indivisible - a write followed by its error check - use `transaction()`.

THE TIMEOUT IS LARGE ON PURPOSE. The `with verify` command family (`V<n>V`,
`INCV<n>V`, `DECV<n>V`) blocks the instrument's parser for up to 5 seconds while
the output settles, so the read timeout has to exceed 5 s or an ordinary verify
command looks like a dead instrument. The cost: a genuinely unreachable device
also takes that long to detect.

AN UNKNOWN COMMAND ANSWERS NOTHING. A mnemonic this firmware does not
implement produces no reply at all - not an error, silence - so the read simply
runs to timeout. That is why Cpx400dpBackend probes every declared query once
at connect(): a declared-but-unsupported channel would otherwise stall the
telemetry stream by the full timeout on every single frame, for the whole run.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..backend import HardwareError

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9221
"""TTi's raw-socket SCPI port. Confirmed on this instrument; telnet (5024) and
the 5025 SCPI-RAW convention are both refused by this firmware."""

DEFAULT_TIMEOUT_S = 8.0
"""Ceiling for one read. Must exceed the 5 s `with verify` block - see above."""

DEFAULT_CONNECT_TIMEOUT_S = 5.0
"""Ceiling for opening the socket. Short, because a missing instrument should
fail setup promptly, and connecting to a present one takes tens of
milliseconds."""

DRAIN_TIMEOUT_S = 0.5
"""How long to wait for a late reply after a read has already timed out, so it
can be discarded rather than mistaken for the answer to the *next* query - see
_discard_late_reply(). Generous, because that case means the instrument is
already behaving slowly and the read it belongs to has been given up on."""

CONNECT_DRAIN_TIMEOUT_S = 0.1
"""How long to wait when draining at connect. Much shorter than the above,
because a reply a dead client left behind arrives as soon as the link is open -
within one round-trip, around 2.4 ms - and this wait is paid on every
connect."""

TERMINATOR = b"\r\n"
"""<RESPONSE MESSAGE TERMINATOR>: CR LF, per the manual."""


class TtiSocketTransport:
    """One raw socket to a TTi instrument, with serialized transactions."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        drain_timeout_s: float = DRAIN_TIMEOUT_S,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._drain_timeout_s = drain_timeout_s
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._writer is not None

    @property
    def address(self) -> str:
        return f"{self._host}:{self._port}"

    async def open(self) -> None:
        """Open the socket, or raise HardwareError explaining what to check.

        A refused connection most often means something else already holds the
        instrument's single raw-socket slot, so the error says so - that is the
        failure a second driver process on the same stand would hit."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=self._connect_timeout_s
            )
        except asyncio.TimeoutError as exc:
            raise HardwareError(
                f"no answer from {self.address} within {self._connect_timeout_s:.1f}s - "
                "check the instrument is powered and the address is current "
                "(it self-assigns a link-local address when no DHCP server is present)"
            ) from exc
        except OSError as exc:
            raise HardwareError(
                f"could not open {self.address}: {exc} - the instrument accepts only one "
                "raw-socket connection at a time, so this usually means another client holds it"
            ) from exc
        logger.info("opened %s", self.address)

    async def close(self) -> None:
        """Close the socket, tolerating a link that is already gone.

        Runs on the teardown path, where raising would mask whatever failure is
        already propagating."""
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except OSError as exc:
            logger.warning("error closing %s, ignoring: %s", self.address, exc)

    def transaction(self) -> asyncio.Lock:
        """The link lock, for callers needing several exchanges to be
        indivisible - a write and the error check that follows it. Everything
        under one `async with transport.transaction():` is serialized against
        the telemetry poll as a unit."""
        return self._lock

    async def query(self, command: str) -> str:
        """Send a command and return its single-line response, without the
        terminator. Raises HardwareError on timeout, which - given an unknown
        mnemonic answers nothing at all - is also how "this firmware does not
        implement that command" presents itself."""
        async with self._lock:
            return await self.query_in_transaction(command)

    async def write(self, command: str) -> None:
        """Send a command that produces no response."""
        async with self._lock:
            await self.write_in_transaction(command)

    async def write_in_transaction(self, command: str) -> None:
        """Send without taking the lock. Only for callers already holding it
        via `transaction()`; `write()` is the one to use otherwise."""
        if self._writer is None:
            raise HardwareError("transport is not open")
        self._writer.write(command.encode("ascii") + b"\n")
        await self._writer.drain()

    async def query_in_transaction(self, command: str) -> str:
        """Send and read one line without taking the lock. Only for callers
        already holding it via `transaction()`; `query()` otherwise."""
        if self._reader is None:
            raise HardwareError("transport is not open")
        await self.write_in_transaction(command)
        try:
            raw = await asyncio.wait_for(self._reader.readuntil(TERMINATOR), timeout=self._timeout_s)
        except asyncio.TimeoutError as exc:
            await self._discard_late_reply(command)
            raise HardwareError(
                f"no response to {command!r} from {self.address} within {self._timeout_s:.1f}s - "
                "either the instrument is unreachable, or this firmware does not implement "
                "that command (an unknown mnemonic is answered with silence, not an error)"
            ) from exc
        except asyncio.IncompleteReadError as exc:
            raise HardwareError(
                f"connection to {self.address} closed while awaiting a response to {command!r}"
            ) from exc
        return raw[: -len(TERMINATOR)].decode("ascii", errors="replace").strip()

    async def drain(self, reason: str, timeout_s: Optional[float] = None) -> int:
        """Read and discard anything already waiting on the link. Returns how
        many replies were thrown away.

        Needed at connect, because a stale reply survives the *connection*, not
        just the read that abandoned it: a client killed mid-transaction leaves
        one unread reply on the instrument, which the next client's first query
        collects - so a fresh driver asks `*IDN?` and is told `0`, with every read
        after that one behind. `_confirm_identity()` catches that and refuses to
        run, so draining first is what keeps an abrupt kill from costing the next
        startup.

        Anything discarded is logged at warning level: it is evidence a previous
        process died without finishing a transaction, worth knowing even though
        this recovers from it."""
        deadline = self._drain_timeout_s if timeout_s is None else timeout_s
        discarded = 0
        while True:
            try:
                stale = await asyncio.wait_for(self._reader.readuntil(TERMINATOR), timeout=deadline)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
                if discarded:
                    logger.warning(
                        "discarded %d stale repl%s on %s (%s) - a previous client left the link "
                        "mid-transaction; the link is now aligned",
                        discarded, "y" if discarded == 1 else "ies", self.address, reason,
                    )
                return discarded
            discarded += 1
            logger.warning("discarding stale reply on %s (%s): %r", self.address, reason, stale)

    async def _discard_late_reply(self, command: str) -> None:
        """After a read has timed out, throw away anything that arrives late.

        A timeout has two possible causes and they are indistinguishable at the
        moment it happens: the instrument will never answer (an unimplemented
        mnemonic, which is answered with silence), or it is merely slow and the
        reply is still coming. The second case is the dangerous one. A reply
        that lands after its read gave up would be handed to the *next* query,
        and every read after that would be answered by the previous one - a
        permanent off-by-one in which every channel reports its neighbour's
        value. Some of that would be caught downstream, since the parsers check
        the echoed mnemonic and the V/A unit suffixes, but `OP<n>?` and
        `LSR<n>?` both answer bare integers and would swap silently.

        Discarding is right rather than merely safe: the caller has already
        been told this query failed, so a late answer to it has no owner."""
        while True:
            try:
                late = await asyncio.wait_for(self._reader.readuntil(TERMINATOR), timeout=self._drain_timeout_s)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
                return
            logger.warning(
                "discarded a late reply to %r on %s: %r - it arrived after the read gave up, and "
                "would otherwise have been read as the answer to the next query",
                command, self.address, late,
            )
