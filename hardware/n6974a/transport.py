"""The N6974A's raw-socket SCPI line protocol, isolated from the backend.

Commands are ASCII terminated with LF; every query response comes back
terminated with LF too. Keysight standardises port 5025 for SCPI sockets, and
this instrument allows any combination of up to six simultaneous data socket,
control socket and telnet connections - so unlike a single-socket instrument,
a second client is not locked out, and a driver cannot assume it is alone.

Isolated into its own class for two reasons. It is the only part of this driver
that touches a socket, so tests can substitute a fake instrument and still
exercise the real message building and parsing above it (see
tests/test_n6974a.py). And the two properties below are properties of the link
rather than of any particular command.

SEVERAL QUERIES PER MESSAGE, ONE ROUND TRIP. Semicolons separate commands
within a message and a leading colon returns the parser to the root, so
`VOLT?;:CURR:LIM?;:OUTP?` is answered with one LF-terminated line of
semicolon-separated values. This is what makes a whole telemetry frame - 57
queries - cost a single round trip, and what lets a write carry its own error
check and readback without a second exchange. Measured on this instrument:
~0.4 ms for a message of settings queries, ~32 ms once a MEASure is included
(the measurement itself is an acquisition, not I/O).

ONE MESSAGE AT A TIME. `_lock` makes each exchange atomic, so a command's reply
can never be delivered to the telemetry loop's read, or vice versa. Callers
needing several exchanges to be indivisible use `transaction()`.

EVERY FAILURE LEAVES HERE AS A HardwareError. Nothing in this module raises a
bare OSError: a reset, a refused write, an EOF and a timeout are all device
failures from a caller's point of view, and the backend's frame loop and the
command server both dispatch on HardwareError. One escaping as a
ConnectionResetError would bypass both.

A MALFORMED MESSAGE IS DISCARDED WHOLE, AND DESYNCS THE LINK. This is the
failure mode that shapes the error handling here. If any command in a message
is not understood, the instrument abandons the entire message - including
queries that preceded the bad one - and answers with silence. Measured:
`OUTP?;:BOGUS?;:FUNC?` returns nothing at all within any timeout. Worse, the
`OUTP?` answer is left in the output queue *without* its terminating LF, so the
next query's response is appended to that fragment and read as one line: a
following `SYST:ERR?` was read back as `0-113,"Undefined header"`. Every read
after that point would be answered by the wrong query.

So a read that times out, or a reply carrying the wrong number of values, is
treated as a desynchronised link rather than a slow one, and the socket is
closed and reopened before anything else is attempted. Reopening is the only
reliable resynchronisation available: it discards the orphaned fragment, and
because the error queue belongs to the connection rather than the instrument -
measured: a second simultaneous socket client sees none of this one's errors, and
a reopened connection starts empty - the new session also begins with a clean
queue. It costs about 400 ms, which is the right price for a path that
should never be taken.

AN UNAVAILABLE QUERY ANSWERS NOTHING. A command this unit does not implement -
one belonging to an uninstalled option, or to another model - produces no reply
at all, only an error queue entry (`+302,"Option not installed"`,
`+310,"The command is not supported by this model"`, `-113,"Undefined
header"`). So a declared-but-absent channel costs a full read timeout, which is
why N6974aBackend probes every declared query once at connect: inside a
compound frame it would instead shorten the reply by one value and desync the
link on every single frame.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Sequence

from ..backend import HardwareError

logger = logging.getLogger(__name__)

DEFAULT_PORT = 5025
"""Keysight's standardised SCPI socket port. Telnet (5024), VXI-11 and HiSLIP
(4880) are also enabled on this instrument; this driver uses the raw socket."""

DEFAULT_TIMEOUT_S = 5.0
"""Ceiling for one read. Must exceed the longest legitimate exchange that is not
explicitly given more time: a MEASure at the maximum measurement time, since
SENSe:SWEep:NPLCycles accepts up to 100 power line cycles, is ~1.7 s at 60 Hz.
Anything longer than this ceiling is an unimplemented command answering with
silence, not a slow instrument - which is why the few commands that really do
block for longer pass their own timeout instead of this being raised for
everything (see the backend's SLOW_ACTIONS)."""

DEFAULT_CONNECT_TIMEOUT_S = 5.0
"""Ceiling for opening the socket. Generous relative to the ~400 ms this
instrument takes to complete a TCP handshake, so a busy instrument is not
mistaken for an absent one."""

TERMINATOR = b"\n"
"""Response message terminator. Keysight sockets terminate every query response
with a newline; command messages sent to the instrument must be terminated the
same way."""

SEPARATOR = ";:"
"""Joins subsystem commands within one message: the semicolon separates them and
the colon returns the parser to the root of the command tree, which is required
whenever consecutive commands are in different subsystems."""

COMMON_SEPARATOR = ";"
"""Joins an IEEE-488.2 common command (one beginning with `*`) to what precedes
it. A common command takes no root colon: `VOLT 1;:*WAI` is a syntax error
(`-113,"Undefined header"`) and, because a malformed command discards the whole
message, it costs the entire exchange. `join_message` picks between the two."""


def join_message(parts: Sequence[str]) -> str:
    """Join message parts with the right separator for each, per the rule above."""
    message = ""
    for part in parts:
        if not message:
            message = part
        elif part.startswith("*"):
            message += COMMON_SEPARATOR + part
        else:
            message += SEPARATOR + part
    return message

VALUE_SEPARATOR = ";"
"""Separates values within one response line.

A value can itself contain this character: error text does, as in
`+310,"The command is not supported by this model;"`, and `*LRN?` answers with
an entire semicolon-separated command list as a single value. So a reply is
split at most `expected - 1` times, which leaves everything after the final
separator in the last value. The rule that falls out of it, and that callers
must respect: any query whose answer may contain a semicolon goes LAST in the
message. `SYSTem:ERRor?` is the one this driver puts there on every write."""


class KeysightSocketTransport:
    """One raw socket to a Keysight SCPI instrument, with serialized,
    count-checked, multi-query exchanges."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self.resynchronisations = 0
        """How many times the link has been reopened to recover from a desync.
        Published as telemetry, because a link recovering repeatedly is a real
        fault even though each individual recovery succeeded."""

    @property
    def is_open(self) -> bool:
        return self._writer is not None

    @property
    def address(self) -> str:
        return f"{self._host}:{self._port}"

    async def open(self) -> None:
        """Open the socket, or raise HardwareError explaining what to check."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=self._connect_timeout_s
            )
        except asyncio.TimeoutError as exc:
            raise HardwareError(
                f"no answer from {self.address} within {self._connect_timeout_s:.1f}s - check the "
                "instrument is powered and the address is current (it self-assigns a link-local "
                "address when no DHCP server is present, and mDNS advertises it as "
                "A-<model>-<serial>.local)"
            ) from exc
        except OSError as exc:
            raise HardwareError(
                f"could not open {self.address}: {exc} - the instrument allows up to six "
                "simultaneous socket and telnet connections in total, so this can mean that "
                "budget is exhausted by other clients"
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
        indivisible. Everything under one `async with transport.transaction():`
        is serialized against the telemetry poll as a unit."""
        return self._lock

    async def query(self, query: str, timeout_s: Optional[float] = None) -> str:
        """Send one query and return its single value."""
        async with self._lock:
            return (await self._exchange([query], expected=1, timeout_s=timeout_s))[0]

    async def query_in_transaction(self, query: str) -> str:
        """As query(), without taking the lock. Only for callers already
        holding it via transaction()."""
        return (await self._exchange([query], expected=1))[0]

    async def query_all(self, queries: Sequence[str]) -> List[str]:
        """Send every query in one message and return one value per query, in
        order. Raises HardwareError if the count does not match."""
        async with self._lock:
            return await self.query_all_in_transaction(queries)

    async def query_all_in_transaction(self, queries: Sequence[str]) -> List[str]:
        """As query_all(), without taking the lock."""
        if not queries:
            return []
        return await self._exchange(list(queries), expected=len(queries))

    async def command_then_query(
        self, command: str, queries: Sequence[str], timeout_s: Optional[float] = None
    ) -> List[str]:
        """Send one command followed by queries, all in a single message, and
        return the queries' values.

        This is how every write is issued: the command travels with its
        `SYSTem:ERRor?` check, so the error cannot belong to another caller's
        command. Costs one round trip - ~0.5 ms against ~0.4 ms for the bare
        command. The error query goes last because its answer may contain a
        semicolon.

        A setting's readback is deliberately NOT part of that message: on this
        instrument a setting query alongside the write that changes it answers
        with the value from before the write. The backend follows up with a
        second message inside the same transaction - see its docstring."""
        async with self._lock:
            return await self.command_then_query_in_transaction(command, queries, timeout_s)

    async def command_then_query_in_transaction(
        self, command: str, queries: Sequence[str], timeout_s: Optional[float] = None
    ) -> List[str]:
        """As command_then_query(), without taking the lock."""
        return await self._exchange([command, *queries], expected=len(queries), timeout_s=timeout_s)

    async def write_no_reply(self, command: str) -> None:
        """Send one command and do not wait for anything back.

        For the single command that answers nothing because it takes the link
        with it: `SYSTem:REBoot`. Every other write is verified, which requires a
        reply; this one cannot be, so the caller owns what happens next."""
        async with self._lock:
            if self._writer is None:
                raise HardwareError("transport is not open")
            try:
                self._writer.write(command.encode("ascii") + TERMINATOR)
                await self._writer.drain()
            except OSError as exc:
                raise HardwareError(f"could not send {command!r} to {self.address}: {exc}") from exc

    async def _exchange(
        self, parts: Sequence[str], expected: int, timeout_s: Optional[float] = None
    ) -> List[str]:
        """Send one message and return its `expected` values.

        `timeout_s` overrides the read ceiling for commands that legitimately
        block for longer than an unimplemented mnemonic would.

        Both failure modes here mean the same thing - the link is no longer
        aligned - and both are handled the same way: resynchronise, then raise.
        See this module's docstring for why a timeout cannot be treated as mere
        slowness on this instrument."""
        if self._reader is None or self._writer is None:
            raise HardwareError("transport is not open")
        deadline = self._timeout_s if timeout_s is None else timeout_s
        message = join_message(parts)
        try:
            self._writer.write(message.encode("ascii") + TERMINATOR)
            await self._writer.drain()
        except OSError as exc:
            # A reset arriving between exchanges surfaces on the write, not the
            # read. Raised as a HardwareError like every other link failure, so
            # a caller that handles device failure handles this too.
            await self._resynchronise(f"{type(exc).__name__} sending {message!r}: {exc}")
            raise HardwareError(
                f"could not send {message!r} to {self.address}: {exc} - the link has been reopened"
            ) from exc

        try:
            raw = await asyncio.wait_for(self._reader.readuntil(TERMINATOR), timeout=deadline)
        except OSError as exc:
            # The instrument resetting the connection - switched off, rebooted,
            # or its socket budget reclaimed - arrives here as
            # ConnectionResetError rather than as the EOF that IncompleteRead
            # covers. Left unhandled it escapes as a non-HardwareError, which
            # skips every device-failure path this driver has.
            await self._resynchronise(f"{type(exc).__name__} awaiting a response to {message!r}: {exc}")
            raise HardwareError(
                f"the link to {self.address} failed while awaiting a response to {message!r}: "
                f"{exc} - the link has been reopened"
            ) from exc
        except asyncio.TimeoutError as exc:
            await self._resynchronise(f"no response to {message!r} within {deadline:.1f}s")
            raise HardwareError(
                f"no response to {message!r} from {self.address} within {deadline:.1f}s - "
                "either the instrument is unreachable, or one of these commands is not "
                "implemented by this unit (an unavailable command is answered with silence and "
                "discards the whole message). The link has been reopened"
            ) from exc
        except asyncio.IncompleteReadError as exc:
            await self.close()
            raise HardwareError(
                f"connection to {self.address} closed while awaiting a response to {message!r}"
            ) from exc
        except asyncio.LimitOverrunError as exc:
            # A reply longer than the stream reader's buffer with no terminator
            # in it. Nothing this driver asks for is that large, so it means the
            # link is carrying something unexpected.
            await self._resynchronise(f"a reply to {message!r} overran the read buffer")
            raise HardwareError(
                f"the reply to {message!r} from {self.address} overran the read buffer without a "
                f"terminator ({exc}) - the link has been reopened"
            ) from exc

        reply = raw[: -len(TERMINATOR)].decode("ascii", errors="replace").strip()
        # Split at most expected-1 times, so a final value containing semicolons
        # - error text, a *LRN? dump - survives intact. See VALUE_SEPARATOR.
        values = reply.split(VALUE_SEPARATOR, expected - 1) if reply else []
        if len(values) != expected:
            await self._resynchronise(
                f"expected {expected} value(s) for {message!r}, got {len(values)}: {reply!r}"
            )
            raise HardwareError(
                f"{self.address} answered {message!r} with {len(values)} value(s) where {expected} "
                f"were expected: {reply!r}. Every value in a message shifts position when one "
                "command is refused, so the link has been reopened rather than trusted"
            )
        return values

    async def _resynchronise(self, reason: str) -> None:
        """Close and reopen the socket, discarding whatever the link holds.

        The only reliable recovery from a desync: a refused message can leave an
        un-terminated response fragment in the output queue that no timeout will
        clear, and which would otherwise be read as the beginning of the next
        reply. Reopening discards it, and since the error queue belongs to the
        I/O session rather than the instrument, the new session also starts with
        an empty one.

        Failure to reopen is left to the caller: this is already an error path,
        and the exception the caller is about to raise describes the original
        problem more usefully than a second one about the recovery."""
        self.resynchronisations += 1
        logger.warning(
            "resynchronising %s (%s) - reopening the socket to discard any partial reply "
            "(recovery %d)", self.address, reason, self.resynchronisations,
        )
        await self.close()
        try:
            await self.open()
        except HardwareError as exc:
            logger.error("could not reopen %s after a desync: %s", self.address, exc)
