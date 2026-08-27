"""Copy finished runs to the results share, filed by DUT, ticket and unit.

    python -m tools.mirror_results
    python -m tools.mirror_results --share-root /Volumes/SEIT/TestResults/MytestResults
    python -m tools.mirror_results --dry-run

One pass per invocation: find every finished run that is not on the share
yet, copy it, say what happened, exit. A scheduled task runs it every few
minutes (see provisioning/Setup-StandBox.ps1), so a pass that dies takes
nothing with it and the next one picks up where it stopped - there is no
long-lived process to wedge, and no state to lose.

WHY THIS IS ITS OWN PROCESS. Neither the telemetry engine nor the test
process can do this. The engine is the participant whose liveness gates
every run - a test aborts when its heartbeat goes stale - so an SMB call
that blocks for half a minute inside it would end runs. The test process
cannot either: these tests are routinely ended with Ctrl+C, and anything
in teardown is the part an impatient second Ctrl+C skips. A separate
process also means a run whose test process died hard is still copied,
because the engine synthesises its verdict and this reads verdicts.

WHAT IT KEYS OFF. A run is finished when its verdict.json carries
`completeness`, which only the engine writes and only once it has seen
the run's stream go quiet. That is an existing signal, not one invented
here, and it means no handshake with anything.

WHAT STOPS TWO PASSES COLLIDING. Nothing in here does - the scheduled
task is registered with MultipleInstances IgnoreNew, so a pass that is
still copying when the next tick fires keeps the slot. Two passes started
by hand against the same output directory can fight over one `_partial_`;
the loser's rename fails and is logged, and the size check means neither
publishes a short copy, but the wasted work is real. One at a time.

WHAT MAKES IT SAFE TO RUN AGAIN. It never deletes or overwrites a run, on
either side - the only things it removes are its own leftovers: a
`_partial_` directory from an interrupted pass, and the probe file it
writes to find out whether the share can be written to at all.
The share is the state: a run is already mirrored if its destination
directory exists, so there is no ledger to lose when a box is reimaged,
and "is this run copied?" is answerable by looking, from any machine. A
copy lands in a `_partial_` sibling and is renamed into place, so the
destination directory exists only once the whole run is there - a reader
never opens a truncated telemetry.csv and analyses it.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from protocol import heartbeat
from protocol.mirror_status import MirrorStatus, write_status
from protocol.paths import (
    DEFAULT_OUTPUT_DIR,
    VERDICT_FILENAME,
    runs_dir,
    safe_path_component,
)

logger = logging.getLogger(__name__)

RESULTS_SHARE_ROOT = Path(r"\\nas.mytra.co\SEIT\TestResults\MytestResults")
"""The root of the filed tree on the results share.

A UNC path, not a drive letter: a mapped drive is per-user and per-session,
and this runs from a scheduled task. Windows needs no mount for a UNC path -
only a credential for the server, which Setup-StandBox.ps1 -ResultsShareOnly
establishes machine-wide.

Overridable per invocation (--share-root), the way an instrument's host is
(see testbeds/zdrive_testbed.py CPX400DP_HOST), which is how this gets
developed against a Mac's mount of the same share."""

NO_DUT = "_NO-DUT"
NO_ER_TICKET = "_NO-ER-TICKET"
NO_DUT_SERIAL = "_NO-DUT-SN"
"""Where a run goes when it cannot be filed.

A run ended before its operator prompt - Ctrl+C during setup - has a perfectly
good verdict and telemetry and no idea which unit or ticket it belongs to. It is
still worth keeping: somebody killed it for a reason, and the telemetry usually
says what it was. Underscore-led so they sort away from the ER-nnnn block and
cannot be mistaken for a ticket. NO_DUT is the rare one - the DUT is a class
attribute, so only an engine-synthesised verdict lacks it."""

NO_TEST_ID = "_UNNAMED-RUN"
"""A run directory whose name is not usable as one on the far side. Only
reachable for a directory nothing in this project named."""

PARTIAL_PREFIX = "_partial_"
"""Prefix for a copy in progress. Its own name until the copy is complete and
verified, so the destination directory's existence means the whole run is there
and nothing else."""

STALE_VERDICT_S = 600.0
"""How long a verdict with no `completeness` waits before it is copied anyway.

Completeness is the engine's signal that a run is finished, so normally this
never fires. It exists for the case that produces a verdict nothing will ever
finalise: the engine killed outright, rather than shut down (its own shutdown
stamps completeness on the way out). Without this those runs would sit here
forever, which is the failure mode of keying off a signal one process owns."""

SKIP_DUTS = frozenset({"example_dut"})
"""DUT packages whose runs are not mirrored. example_dut is scaffolding for
writing a new DUT package - it measures nothing, and a top-level directory for
it on the share is a permanent invitation to wonder what is in it."""


def read_verdict_data(path: Path) -> Optional[Dict]:
    """A verdict as it is on disk, or None if it cannot be read.

    Raw JSON rather than protocol.verdict.Verdict, because the one question
    below that cannot be asked of a parsed verdict is whether a key is present
    at all - a `dut` field defaulted to "" and a `dut` field never written are
    different runs (see skip_reason)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def skip_reason(verdict: Dict) -> str:
    """Why this run is not mirrored, or "" if it should be.

    A verdict with no `dut` key at all was written before runs recorded which
    stand produced them. Those predate the share, cannot be filed by DUT, and
    are deliberately left where they are rather than piled into a sentinel -
    an empty `dut` on a verdict that has the key is a different thing, and
    that one is filed."""
    if "dut" not in verdict:
        return "predates the DUT identifier"
    if verdict.get("used_mock"):
        return "drove a mock backend"
    if verdict.get("dut") in SKIP_DUTS:
        return f"{verdict['dut']} runs are not mirrored"
    return ""


def is_finished(verdict: Dict, path: Path, now: Optional[float] = None) -> bool:
    """Whether the engine is done writing this run.

    `completeness` is the engine's own finish marker. The age fallback covers an
    engine that was killed before it could stamp one: the file has not been
    touched in STALE_VERDICT_S, so nothing is coming."""
    if verdict.get("completeness") is not None:
        return True
    try:
        untouched_s = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return untouched_s > STALE_VERDICT_S


def destination(share_root: Path, verdict: Dict, test_id: str) -> Path:
    """Where this run is filed: <dut>/<ticket>/<serial>/runs/<test_id>.

    Every component is reduced to something that is one path component and legal
    on Windows, since two of them are what an operator typed. The unreduced
    answers stay in verdict.json, so nothing is lost by filing under a tidied
    name.

    The run's own directory name is reduced too, even though new_test_id()
    already produced a safe one: a run directory named by hand, or by a caller
    passing --test-id, can hold anything the local filesystem allowed, and a
    name Windows refuses is a run that fails to copy on every pass forever -
    which quietly poisons the backlog count the operator prompt reports."""
    metadata = verdict.get("metadata") or {}
    return (
        Path(share_root)
        / safe_path_component(str(verdict.get("dut") or ""), NO_DUT)
        / safe_path_component(str(metadata.get("er_ticket") or ""), NO_ER_TICKET)
        / safe_path_component(str(metadata.get("dut_serial_number") or ""), NO_DUT_SERIAL)
        / "runs"
        / safe_path_component(test_id, NO_TEST_ID)
    )


def _file_sizes(root: Path) -> Dict[str, int]:
    """Every file under `root`, by path relative to it, with its size."""
    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in root.rglob("*")
        if path.is_file()
    }


def copy_run(source: Path, dest: Path) -> None:
    """Copy one finished run directory to `dest`, atomically as far as a reader
    is concerned.

    Into a `_partial_` sibling first, then renamed. A rename within one share is
    the server's own operation, so the destination appears whole or not at all;
    a copy straight into place would leave a half-written telemetry.csv sitting
    where a reader expects a complete one, and a truncated CSV is a quiet way to
    be wrong.

    Sizes are compared afterwards rather than hashes. SMB checksums the
    transport, so what this catches is a copy that stopped early - a full share,
    a dropped connection - and comparing byte counts catches that for the cost
    of a stat. Hashing gigabytes on both ends to catch what the transport
    already checks is effort better spent elsewhere.

    A `_partial_` left by an interrupted pass is removed and redone, not
    resumed: it cannot be told apart from one that stopped mid-file."""
    partial = dest.parent / f"{PARTIAL_PREFIX}{dest.name}"
    if partial.exists():
        shutil.rmtree(partial)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, partial)

    expected, copied = _file_sizes(source), _file_sizes(partial)
    short = {name: size for name, size in expected.items() if copied.get(name) != size}
    if short:
        shutil.rmtree(partial, ignore_errors=True)
        raise OSError(
            f"{dest.name}: {len(short)} file(s) did not copy whole "
            f"(first: {sorted(short)[0]}) - nothing was left on the share"
        )
    # Never onto an existing directory: Windows refuses that outright, and it
    # would mean overwriting a run somebody may already have read.
    os.rename(partial, dest)


def check_reachable(share_root: Path) -> str:
    """"" if the share can be written to, else what is wrong with it in one line.

    A write, not a stat: a share can be present and unwritable - a read-only
    mount, an expired credential, a full volume - and each of those looks fine
    to anything that only looks."""
    probe = Path(share_root) / f".mirror-probe-{os.getpid()}"
    try:
        Path(share_root).mkdir(parents=True, exist_ok=True)
        probe.write_text("")
        return ""
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def pending_runs(
    output_dir: Path, share_root: Path, check_destination: bool = True
) -> List[Tuple[Path, Path]]:
    """Every finished, mirrorable run not yet on the share, oldest first.

    Oldest first so a backlog drains in the order the runs happened, which is
    the order somebody looking for them expects them to appear.

    `check_destination=False` skips asking the share what it already has, and
    reports every finished mirrorable run instead - for a share already known to
    be down.

    Measured on a stand box: first contact with an unreachable server costs ~21 s
    before it gives up. Windows then negative-caches that, so the runs behind the
    first are free, and the cost is per pass rather than per run. Skipped anyway,
    because relying on a cache to make a pointless question cheap is not the same
    as not asking it - and check_reachable has already answered it."""
    found: List[Tuple[float, Path, Path]] = []
    root = runs_dir(output_dir)
    if not root.is_dir():
        return []
    for run in root.iterdir():
        verdict_file = run / VERDICT_FILENAME
        if not verdict_file.is_file():
            continue  # a run still going, or one whose process died before writing
        verdict = read_verdict_data(verdict_file)
        if verdict is None:
            logger.warning("%s: unreadable verdict, skipped", run.name)
            continue
        reason = skip_reason(verdict)
        if reason:
            logger.debug("%s: %s", run.name, reason)
            continue
        if not is_finished(verdict, verdict_file):
            continue
        dest = destination(share_root, verdict, run.name)
        if check_destination and dest.exists():
            continue
        found.append((verdict_file.stat().st_mtime, run, dest))
    return [(run, dest) for _, run, dest in sorted(found, key=lambda item: item[0])]


def run_once(output_dir: Path, share_root: Path, dry_run: bool = False) -> MirrorStatus:
    """One pass. Returns what to publish about it - and publishing happens even
    when the share is unreachable, because "unreachable" is the single most
    useful thing this can tell the operator prompt."""
    unreachable = check_reachable(share_root)
    if unreachable:
        logger.warning("results share unreachable (%s): %s", share_root, unreachable)
        return MirrorStatus(
            updated_at=time.time(), share_root=str(share_root), reachable=False,
            outstanding=len(_countable(output_dir, share_root)), error=unreachable,
        )

    pending = pending_runs(output_dir, share_root)
    mirrored, errors = 0, []
    for run, dest in pending:
        if dry_run:
            logger.info("would copy %s -> %s", run.name, dest)
            continue
        try:
            copy_run(run, dest)
        except OSError as exc:
            logger.warning("%s: %s", run.name, exc)
            errors.append(str(exc))
            continue
        mirrored += 1
        logger.info("copied %s -> %s", run.name, dest)

    return MirrorStatus(
        updated_at=time.time(), share_root=str(share_root), reachable=True,
        mirrored=mirrored, outstanding=len(pending) - mirrored,
        # Capped: this ends up in a dialog, and every one of them was logged in
        # full on the way past.
        error=_summarise(errors),
    )


def _summarise(errors: List[str], keep: int = 3) -> str:
    """The first few failures, and how many more there were."""
    if len(errors) <= keep:
        return "; ".join(errors)
    return "; ".join(errors[:keep]) + f"; and {len(errors) - keep} more"


def _countable(output_dir: Path, share_root: Path) -> List:
    """Runs waiting, when the share cannot be reached to check what is on it.

    Every finished mirrorable run counts as outstanding: with the share down
    there is no way to know which are already there, and over-reporting a
    backlog during an outage is the harmless direction to be wrong in. The
    share is not touched at all - see pending_runs on why asking a dead server
    is worse than not knowing."""
    try:
        return pending_runs(output_dir, share_root, check_destination=False)
    except OSError:
        return []


def resolve_output_dir(explicit: Optional[Path]) -> Path:
    """Where the engine is writing, from its heartbeat.

    The same source the test process uses, and for the same reason: the engine
    is the authority on where it writes, so nothing else records it and the two
    cannot disagree. Falls back to the default when no engine is running, which
    is the normal case for a pass between runs."""
    if explicit is not None:
        return Path(explicit)
    beat = heartbeat.read_heartbeat()
    return Path(beat.output_dir) if beat is not None else DEFAULT_OUTPUT_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--share-root", type=Path, default=RESULTS_SHARE_ROOT,
        help="root of the filed tree on the results share",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="where runs are, if not where the engine's heartbeat says",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="say what would be copied, copy nothing, publish no status. The share is\n             still probed, since whether it can be written to is most of the answer",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    output_dir = resolve_output_dir(args.output_dir)
    logger.info("mirroring %s -> %s", output_dir, args.share_root)
    status = run_once(output_dir, args.share_root, args.dry_run)
    if not args.dry_run:
        write_status(status)
    logger.info(
        "pass done: %d copied, %d outstanding, share %s",
        status.mirrored, status.outstanding,
        "reachable" if status.reachable else "UNREACHABLE",
    )
    return 0 if status.reachable and not status.error else 1


if __name__ == "__main__":
    raise SystemExit(main())
