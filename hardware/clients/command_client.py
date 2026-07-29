"""Generic HW Command Client used by the testcase execution process
to talk to a hardware driver's command server. It's a synchronous
request/reply client over ZeroMQ REQ, so test cases can call these
methods directly from their own logic.

Exposes only the universal core (connect/disconnect/get_status/
list_actions) plus a generic `execute()` for anything device-specific.
Device-specific convenience methods live on subclasses - see
DaqCommandClient and PowerSupplyCommandClient - so call sites read as
e.g. `daq.start_acquisition(test_id=...)` rather than
`daq.execute("start_acquisition", test_id=...)` everywhere.

verify_actions() is the positive-confirmation half of channel
declaration: it calls list_actions() (a real, live answer from the
running backend) and raises MissingChannelError if any expected action
isn't actually supported, rather than trusting a hand-maintained list
that could drift from what the backend actually implements.

Timeout/watchdog behavior: both halves of execute() are bounded -
RCVTIMEO and SNDTIMEO are both set to timeout_ms, so a dead or hung
command server (a crashed driver process, a wedged real-device call)
can't block a test forever. On a timeout, execute() raises
CommandTimeout. It deliberately does NOT rebuild the REQ socket: a REQ
socket enforces strict send->recv alternation, so a timed-out socket is
left in a broken state, and the only "recovery" would be to recreate it
and resend - but silently resending a command with side effects (a
relative move_incremental, a set_position/set_axis_state on a real
motor) could double-apply it. Instead the client marks itself _broken;
every subsequent execute() raises a clear CommandClientError telling the
caller to reconstruct the client, rather than leaking the cryptic zmq
"operation cannot be accomplished in current state" (EFSM) error the
broken socket would otherwise raise on its next send. A command timeout
is effectively fatal to this client instance by design - the caller
(the test) is expected to abort and tear down, not keep issuing commands.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

import zmq

from ..backend import MissingChannelError
from protocol.wire import DEFAULT_COMMAND_ENDPOINT, CommandReply, CommandRequest


class CommandClientError(Exception):
    """Raised when the command server reports a failure."""


class CommandTimeout(CommandClientError):
    """Raised by CommandClient.execute() when a send or receive exceeds
    the client's timeout_ms - the command server didn't answer in time
    (or couldn't even be sent to). A subclass of CommandClientError so
    existing `except CommandClientError` handlers (tools/manual_gui.py's
    send worker, TestCase.teardown_step) still catch it, while callers
    that care can tell a timeout apart from a backend-reported failure.
    Marks the client broken - see this module's docstring."""


class CommandClient:
    """Synchronous request/reply client for a hardware driver's command server."""

    def __init__(self, endpoint: str = DEFAULT_COMMAND_ENDPOINT, timeout_ms: int = 5000):
        self._ctx = zmq.Context.instance()
        self._socket = self._ctx.socket(zmq.REQ)
        self._socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self._socket.setsockopt(zmq.SNDTIMEO, timeout_ms)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.connect(endpoint)
        self._timeout_ms = timeout_ms
        self._broken = False

    def execute(self, action: str, **params: Any) -> Any:
        """Send a device-specific command and return its result.

        Bounded by timeout_ms on both send and receive; a timeout raises
        CommandTimeout and marks this client broken (see module
        docstring). Raises CommandClientError immediately, without
        touching the socket, if the client was already broken by an
        earlier timeout."""
        if self._broken:
            raise CommandClientError(
                f"command client is broken after an earlier timeout "
                f"({self._timeout_ms}ms) - reconstruct it before reuse"
            )
        req = CommandRequest(cmd=action, args=params)
        try:
            self._socket.send(req.to_bytes())
            raw = self._socket.recv()
        except zmq.error.Again as exc:
            # RCVTIMEO/SNDTIMEO expired. The REQ socket is now stuck
            # mid-alternation and unusable; mark broken rather than
            # recreate-and-resend, which could double-apply a stateful
            # command on real hardware - see this module's docstring.
            self._broken = True
            raise CommandTimeout(
                f"command {action!r} timed out after {self._timeout_ms}ms - "
                "command server not responding; client must be reconstructed"
            ) from exc
        reply = CommandReply.from_bytes(raw)
        if not reply.ok:
            raise CommandClientError(reply.error)
        return reply.result

    def connect_backend(self) -> None:
        self.execute("connect")

    def disconnect_backend(self) -> None:
        self.execute("disconnect")

    def get_status(self) -> Dict[str, Any]:
        return self.execute("get_status")

    def list_actions(self) -> List[str]:
        return self.execute("list_actions")

    def verify_actions(self, expected: Iterable[str]) -> None:
        """Raise MissingChannelError if any of `expected` isn't among
        what list_actions() reports the backend actually supports."""
        missing = set(expected) - set(self.list_actions())
        if missing:
            raise MissingChannelError(f"missing command channels: {sorted(missing)}")

    def close(self) -> None:
        self._socket.close(linger=0)
