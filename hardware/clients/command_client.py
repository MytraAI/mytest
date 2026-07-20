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
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

import zmq

from ..backend import MissingChannelError
from ..protocol import DEFAULT_COMMAND_ENDPOINT, CommandReply, CommandRequest


class CommandClientError(Exception):
    """Raised when the command server reports a failure."""


class CommandClient:
    """Synchronous request/reply client for a hardware driver's command server."""

    def __init__(self, endpoint: str = DEFAULT_COMMAND_ENDPOINT, timeout_ms: int = 5000):
        self._ctx = zmq.Context.instance()
        self._socket = self._ctx.socket(zmq.REQ)
        self._socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.connect(endpoint)

    def execute(self, action: str, **params: Any) -> Any:
        """Send a device-specific command and return its result."""
        req = CommandRequest(cmd=action, args=params)
        self._socket.send(req.to_bytes())
        raw = self._socket.recv()
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
