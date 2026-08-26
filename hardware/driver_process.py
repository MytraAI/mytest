"""Starting a hardware driver as a subprocess.

A testbed owns its drivers' lifetime: it starts them, commands them, and
terminates them. What this module adds is that a console interrupt must not
take that ownership away.

Ctrl+C reaches every process attached to the console, not just the one being
watched - CTRL_C_EVENT to the console's process list on Windows, SIGINT to the
foreground process group on POSIX. Started plainly, a driver dies at the same
instant the test process begins tearing down, and teardown's commands - disarm
the axis, drop the 48 V bus - reach a socket nobody is serving. The stand is
then left energized by the very keystroke meant to stop it.

Started here, the driver is outside that group and stays up until the testbed
terminates it, which teardown does after the rails are down. Nothing else about
it changes: it still exits on terminate(), and it still has no signal handling
of its own to rely on (see AI/Mytest.md's OS compatibility section).

Lives in hardware/ rather than beside the testbeds because how a driver process
is launched is a property of driver processes, and all three testbeds start
them the same way.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence


CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
"""Windows' own creation flag, carrying its documented value as a fallback:
subprocess only defines the Windows-only constants on Windows, and this module
is read and tested from the machines the stands are developed on."""


def start_driver(args: Sequence[str], console_path: Optional[Path] = None) -> subprocess.Popen:
    """Start a driver process, detached from the console's interrupts.

    `console_path` captures the process's raw stdout and stderr. Without it they are
    INHERITED - not discarded, which is what makes this worth doing: they go to the
    terminal of whoever launched the test, so a vendor library's message, or a
    traceback printed on the way down, exists in a scrollback and nowhere else. That
    is how a 6 h zdrive run's cause came to be missing from its own run directory.

    What it costs is the live view: a person watching the terminal no longer sees each
    driver's INFO lines as they happen. The run's own operator dashboard is the place
    for that, and the file is the place for the record."""
    stdio = {}
    if console_path is not None:
        console_path.parent.mkdir(parents=True, exist_ok=True)
        # Append, matching the driver's own log: a driver restarted mid-run adds to the
        # file rather than erasing what the last attempt recorded.
        handle = open(console_path, "a", encoding="utf-8", buffering=1)
        stdio = {"stdout": handle, "stderr": subprocess.STDOUT}
    if sys.platform == "win32":
        return subprocess.Popen(list(args), creationflags=CREATE_NEW_PROCESS_GROUP, **stdio)
    return subprocess.Popen(list(args), start_new_session=True, **stdio)
