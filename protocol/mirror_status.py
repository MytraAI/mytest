"""What the results mirror last managed, and how a test asks.

The mirror worker (tools/mirror_results.py) copies finished runs to the
results share. It runs on its own schedule, in its own process, and
nothing waits on it - so the only way anybody finds out it has stopped
working is if it says so somewhere a person will look.

It writes this file after every pass, and the run-details prompt reads
it before asking the operator anything: a stand where the share is
unreachable, or where the worker was never installed, says so in a
window while somebody is standing in front of it, rather than being
discovered weeks later by whoever went looking for results that were
never copied.

Read rather than checked live, deliberately. A test process that
stat()ed the share itself would block for the SMB timeout - tens of
seconds - at exactly the point in a run where a person is waiting, and
it still could not detect the failure that matters most after a box is
reimaged: a share that is perfectly reachable and a worker that is not
running. A missing or stale status file is that failure.

A plain file under the system tempdir, beside the engine's heartbeat
(see heartbeat.py) and for the same reasons. Both the worker and the
test process run as the operator, so both resolve the same tempdir.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATUS_FILENAME = "mytest-mirror.json"

DEFAULT_STALE_AFTER_S = 1800.0
"""How old a status file may be before the mirror is treated as not running.

Generous - the worker's own schedule is minutes, not seconds - because the
consequence of calling it stale is a warning in front of an operator, and one
raised at a slow pass would train people to click through it. A worker that has
not completed a pass in half an hour has stopped, not paused."""


@dataclass
class MirrorStatus:
    """One mirror pass, as the worker left it."""

    updated_at: float
    share_root: str
    reachable: bool
    """Whether the share could be written to on that pass. False covers every
    reason at once - unmounted, no credential, read-only, full - because the
    operator's next move is the same for all of them."""
    mirrored: int = 0
    """Runs copied on that pass."""
    outstanding: int = 0
    """Runs finished, eligible, and not yet on the share. Non-zero is normal for
    a moment and a backlog if it stays that way, which is the number worth
    putting in front of a person."""
    error: str = ""
    """What went wrong, if anything, in terms an operator can act on."""

    def age_s(self, now: Optional[float] = None) -> float:
        return (time.time() if now is None else now) - self.updated_at

    def is_fresh(self, stale_after_s: float = DEFAULT_STALE_AFTER_S,
                 now: Optional[float] = None) -> bool:
        return self.age_s(now) < stale_after_s


def status_path() -> Path:
    return Path(tempfile.gettempdir()) / STATUS_FILENAME


def write_status(status: MirrorStatus, path: Optional[Path] = None) -> None:
    """Publish the result of a pass. Atomic, and best-effort: a worker that
    cannot say what it did has still done it, and must not fail the pass over
    the report.

    No replace-retry loop, unlike write_heartbeat next door. That exists because
    the heartbeat's reader opens it constantly and Windows fails os.replace()
    while it is open, so a collision is normal there and a missed beat spends the
    staleness budget of a healthy run. Here the reader opens the file once per
    run, the writer once per pass, and the staleness window is half an hour of
    passes - so a collision is unlikely and one lost report costs nothing."""
    target = status_path() if path is None else path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(asdict(status)))
        os.replace(tmp, target)
    except OSError:
        logger.warning("couldn't write mirror status to %s", target, exc_info=True)


def read_status(path: Optional[Path] = None) -> Optional[MirrorStatus]:
    """The last pass, or None if absent/unreadable/corrupt.

    None is the answer for "no mirror is running here", which is what absent,
    unparseable and written-by-something-else all mean to a caller."""
    target = status_path() if path is None else path
    try:
        data = json.loads(target.read_text())
        return MirrorStatus(
            updated_at=float(data["updated_at"]),
            share_root=str(data["share_root"]),
            reachable=bool(data["reachable"]),
            mirrored=int(data.get("mirrored", 0)),
            outstanding=int(data.get("outstanding", 0)),
            error=str(data.get("error", "")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


REPAIR_COMMAND = (
    r"cd <your mytest checkout>" "\n    "
    r"powershell -ExecutionPolicy Bypass -File provisioning\Setup-StandBox.ps1 -ResultsShareOnly"
)
"""What an operator runs to put the mirror back.

Carries -ExecutionPolicy Bypass and the cd, because without either it fails on a
stand box - the default policy refuses an unsigned script, and the path is
relative to the checkout. A repair instruction that errors is worse than none:
it teaches people the dialog is wrong.

Named rather than offered as a button. The prompt window is deliberate that it
cannot act - "a dialog that could do both invites clicking the wrong one while
standing at live hardware" - and this needs elevation, so a button would raise
a UAC window over a stand's screen and do nothing at all for an operator who is
not an administrator. Nothing is urgent either way: the run is recorded locally
and the mirror backfills, so this can be run during the run or after it."""


def describe_for_operator(
    status: Optional[MirrorStatus], repair_command: str = REPAIR_COMMAND
) -> Optional[str]:
    """What to put in front of the operator, or None if there is nothing wrong.

    Three things can be wrong and they need different words: no mirror running
    at all, a mirror that cannot reach the share, and a mirror that is running
    and behind. All three end with the same reassurance - the run is recorded
    locally either way - because none of them is a reason not to run."""
    safe = ("This run will be recorded on this machine either way, and copied to "
            "the share once it is reachable.")
    if status is None:
        return (
            "The results mirror has never run on this machine.\n\n"
            f"Finished runs are not being copied to the results share. {safe}\n\n"
            f"To fix it, from a console on this machine:\n    {repair_command}"
        )
    if not status.is_fresh():
        # What is known is when it last finished a pass, which is not the same as
        # "it is not running" - a task registered without its repeating trigger
        # runs at logon and no more, and saying it had stopped would be wrong.
        return (
            f"The results mirror last completed a pass {status.age_s() / 60:.0f} min ago.\n\n"
            f"It is not keeping up, or it has stopped. Finished runs are not reliably "
            f"reaching the results share. {safe}\n\n"
            f"To check it, from a console on this machine:\n    {repair_command}"
        )
    if not status.reachable:
        detail = f"\n\n{status.error}" if status.error else ""
        return (
            f"The results share is not reachable from this machine.\n\n"
            f"{status.share_root}{detail}\n\n{safe}\n\n"
            f"If it stays unreachable, from a console on this machine:\n    {repair_command}"
        )
    if status.outstanding:
        if status.error:
            # A run that fails to copy is retried every pass and stays in the count
            # forever, so a backlog that will not clear on its own must say why -
            # otherwise it is a number that grows and nags with nothing to act on.
            return (
                f"{status.outstanding} finished run(s) are waiting to be copied to the "
                f"results share, and the last pass reported errors.\n\n{status.error}\n\n"
                f"{safe}"
            )
        return (
            f"{status.outstanding} finished run(s) are waiting to be copied to the "
            f"results share.\n\nThe mirror is running and reachable, so this should "
            f"clear on its own. {safe}"
        )
    return None
