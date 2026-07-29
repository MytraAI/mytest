"""Command server: request/reply endpoint for the hardware driver.

Runs a ZeroMQ REP socket. A CommandClient (or a device-specific
subclass, e.g. DaqCommandClient/PowerSupplyCommandClient) connects
here to issue discrete commands.

Only connect/disconnect/get_status/list_actions are dispatched
directly against the backend's universal core. Everything else is
device-specific and routed to `backend.execute()` unchanged, so this
server never needs to know what commands any given device supports.

It also does not implement a test sequence state machine itself -
that logic belongs to the testcase execution process, which decides
what commands to send and when.

Deliberately no server-side timeouts on the backend calls in _handle():
device operations have no sane fixed ceiling (motor calibration and
homing legitimately run for many seconds), so a naive asyncio.wait_for
here would kill real work. The actual hang protection lives on the
client instead (CommandClient's RCVTIMEO/SNDTIMEO): there is exactly one
client per driver and one test at a time, so a wedged backend call is
already backstopped by the client timing out, the test aborting and
tearing down, and the whole driver process being terminated - at which
point this stalled loop dies with it. See hardware/clients/command_client.py.
"""
from __future__ import annotations

import logging

import zmq
import zmq.asyncio

from .backend import HardwareBackend, HardwareError
from protocol.wire import DEFAULT_COMMAND_ENDPOINT, CommandReply, CommandRequest

logger = logging.getLogger(__name__)


class CommandServer:
    """Request/reply server dispatching commands to a HardwareBackend."""

    def __init__(self, backend: HardwareBackend, endpoint: str = DEFAULT_COMMAND_ENDPOINT):
        self._backend = backend
        self._endpoint = endpoint
        self._ctx = zmq.asyncio.Context.instance()
        self._socket = self._ctx.socket(zmq.REP)

    async def run(self) -> None:
        """Bind the socket and serve command requests until cancelled."""
        self._socket.bind(self._endpoint)
        logger.info("command server listening on %s", self._endpoint)
        try:
            while True:
                raw = await self._socket.recv()
                reply = await self._handle(raw)
                await self._socket.send(reply.to_bytes())
        finally:
            self._socket.close(linger=0)

    async def _handle(self, raw: bytes) -> CommandReply:
        try:
            req = CommandRequest.from_bytes(raw)
        except Exception as exc:  # malformed request, can't even get an id
            logger.exception("malformed command request")
            return CommandReply(id="unknown", ok=False, error=f"malformed request: {exc}")

        try:
            if req.cmd == "connect":
                result = await self._backend.connect()
            elif req.cmd == "disconnect":
                result = await self._backend.disconnect()
            elif req.cmd == "get_status":
                result = await self._backend.get_status()
            elif req.cmd == "list_actions":
                result = await self._backend.list_actions()
            else:
                result = await self._backend.execute(req.cmd, **req.args)
            return CommandReply(id=req.id, ok=True, result=result)
        except HardwareError as exc:
            return CommandReply(id=req.id, ok=False, error=str(exc))
        except Exception as exc:  # unexpected bug in backend/handler
            logger.exception("command %s failed", req.cmd)
            return CommandReply(id=req.id, ok=False, error=f"internal error: {exc}")
