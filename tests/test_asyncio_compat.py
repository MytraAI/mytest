"""Starting a process on a loop that can poll a ZeroMQ socket."""
from __future__ import annotations

import asyncio

from protocol import asyncio_compat


async def _answer() -> int:
    await asyncio.sleep(0)
    return 42


def test_a_coroutine_runs_to_completion_and_returns_its_value():
    assert asyncio_compat.run(_answer()) == 42


def test_the_windows_path_runs_on_a_loop_with_add_reader(monkeypatch):
    """The proactor loop Windows defaults to has no add_reader(), which is what
    pyzmq's asyncio sockets are built on - so every process here would die on
    its first poll. Exercised by forcing the branch, since the suite runs on
    machines where sys.platform is not win32."""
    monkeypatch.setattr(asyncio_compat.sys, "platform", "win32")

    async def loop_capability() -> bool:
        return hasattr(asyncio.get_running_loop(), "add_reader")

    assert asyncio_compat.run(loop_capability()) is True
