"""The telemetry engine's liveness heartbeat, and how a test reads it.

A run's whole product is its record, so a test that keeps driving hardware
after recording has stopped spends wear producing nothing. The engine
publishes a heartbeat while recording; TestCase refuses to start without one
and aborts if it goes stale (see TestCase.check_recording_alive).

A plain file under the system tempdir, for the same reason stop markers are:
exists()/write_text()/unlink() behave identically on Windows, CentOS and
macOS, unlike signals. It carries the engine's actual output_dir, so the test
finds the run directory to write into without a shared config or a flag that
could disagree - the engine is the authority on where it writes.

This is deliberately not the "no feedback loop from the telemetry engine"
that AI/Mytest.md forbids: that rule keeps *evaluation results* from
influencing a running test. A liveness check carries no result - it's an
infrastructure precondition, pulled from the filesystem exactly as stop
requests already are.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HEARTBEAT_FILENAME = "mytest-engine.json"

DEFAULT_REFRESH_S = 1.0
"""How often the engine rewrites the file (its reconcile tick)."""

DEFAULT_STALE_AFTER_S = 10.0
"""How old a heartbeat may be before a test treats recording as lost -
ten missed refreshes, so a single slow tick or a GC pause can't abort a
test, but a dead/wedged engine is noticed within seconds."""


def heartbeat_path() -> Path:
    return Path(tempfile.gettempdir()) / HEARTBEAT_FILENAME


@dataclass
class EngineHeartbeat:
    pid: int
    output_dir: str
    updated_at: float

    def age_s(self, now: Optional[float] = None) -> float:
        return (time.time() if now is None else now) - self.updated_at

    def is_fresh(self, stale_after_s: float = DEFAULT_STALE_AFTER_S, now: Optional[float] = None) -> bool:
        return self.age_s(now) < stale_after_s


def write_heartbeat(output_dir: Path, path: Optional[Path] = None) -> None:
    """Publish/refresh the heartbeat. Atomic (write-temp-then-replace) so
    a reader never sees a half-written file. Best-effort: logs rather than
    raises, since failing to advertise liveness must not take down an
    otherwise healthy engine."""
    target = heartbeat_path() if path is None else path
    payload = EngineHeartbeat(pid=os.getpid(), output_dir=str(output_dir), updated_at=time.time())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload.__dict__))
        os.replace(tmp, target)
    except OSError:
        logger.warning("couldn't write engine heartbeat to %s", target, exc_info=True)


def read_heartbeat(path: Optional[Path] = None) -> Optional[EngineHeartbeat]:
    """The current heartbeat, or None if absent/unreadable/corrupt.
    Absent and corrupt are the same answer to the only question a caller
    asks - is something recording right now - so they're not
    distinguished."""
    target = heartbeat_path() if path is None else path
    try:
        data = json.loads(target.read_text())
        return EngineHeartbeat(
            pid=int(data["pid"]), output_dir=str(data["output_dir"]), updated_at=float(data["updated_at"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear_heartbeat(path: Optional[Path] = None) -> None:
    """Remove the heartbeat on clean engine shutdown, so a test starting
    afterwards fails fast rather than waiting out the staleness window."""
    target = heartbeat_path() if path is None else path
    try:
        target.unlink(missing_ok=True)
    except OSError:
        logger.warning("couldn't remove engine heartbeat at %s", target, exc_info=True)
