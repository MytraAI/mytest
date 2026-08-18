"""Starting an asyncio program that polls ZeroMQ sockets, on every OS this
runs on.

Windows defaults to the proactor event loop, which implements no add_reader() -
the call pyzmq's asyncio sockets are built on. Every long-lived process here
polls ZeroMQ from asyncio (the engine's aggregator, and both servers inside
every hardware driver), so on Windows each one raises "Proactor event loop does
not implement add_reader family of methods required for zmq" on its first poll
and never recovers. run() below starts them on a selector loop instead, which
does implement it.

Lives in protocol/ because this is about being able to speak the shared ZeroMQ
transport at all, and protocol/ is what every process on that transport already
imports - see wire.py.

The only difference from asyncio.run() is which loop it picks. What the
proactor loop offers that the selector loop doesn't is asyncio subprocesses,
which nothing here uses: driver processes are started with subprocess.Popen by
a testbed, not from an event loop.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T]) -> T:
    """asyncio.run(), on an event loop that can poll a ZeroMQ socket.

    Every process entry point that serves or subscribes over ZeroMQ starts
    here rather than at asyncio.run(). The loop has to be chosen as it is
    created, so this cannot be done from inside the coroutine it runs."""
    if sys.platform != "win32":
        return asyncio.run(coro)
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coro)
