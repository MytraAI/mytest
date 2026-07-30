"""Engine-side run tracking and per-run bookkeeping: decide which run (if
any) a device's frames belong to, account those frames, stamp completeness
onto each run's verdict, and record a verdict for a run whose test process
died without writing one.

**How the engine knows a run is open.** The testcase process publishes a
small run-state stream (protocol/wire.py's RunStateFrame) for the whole life
of a run, from before PreTestSetup until after PostTestTeardown. Its existence
is the signal:

  - a state frame arrives      -> that run is open; its `devices` are the ones
                                  whose frames belong to it, and its `state` is
                                  merged into the rows written for them
  - the stream goes quiet      -> the run is over, and is finalized
  - a *different* test_id      -> the previous run is finalized immediately

There is deliberately no start or end marker on the stream. A start marker
would protect nothing, because the test publishes state before any device
driver exists, so there are no frames to misroute. An end marker would be
worse than useless: the verdict file already distinguishes a clean end from a
crash - more reliably, since it is written atomically before teardown - and
finalizing the instant a marker arrived would truncate frames still sitting in
the engine's write queue. Quiet-then-check is what makes trailing frames
counted by construction.

**Routing has no gaps and no duplication.** Exactly one destination per frame:

  | frame from device D        | destination                                |
  |----------------------------|--------------------------------------------|
  | run open, D declared by it | runs/<test_id>/D/telemetry.csv, state merged|
  | run open, D not declared   | raw/D/telemetry_<session>.csv               |
  | no run open                | raw/D/telemetry_<session>.csv               |

So when a test process dies, the state stream stops, every device reverts to
the per-session record, and frames keep landing without interruption - the
continuity the old dual-write was trying to buy, without writing anything
twice.

On finalization a run's verdict.json - written by the test process itself,
before its own teardown - is amended in place with the completeness stats only
the engine can produce. If no verdict.json exists, the test process died
without writing one (taskkill /F, SIGKILL, a hard crash), and a
lifecycle=CRASHED verdict is synthesized so the run leaves a record instead of
vanishing.

flush() finalizes everything at engine shutdown but never synthesizes CRASHED:
a run still in progress isn't crashed. That window is narrow anyway, since a
stopping engine is itself something a running test notices and aborts on (see
protocol/heartbeat.py).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from protocol.paths import verdict_path
from protocol.verdict import (
    BoundsResult,
    Lifecycle,
    Verdict,
    amend_completeness,
    write_verdict,
)
from protocol.wire import RunStateFrame, TelemetryFrame

from .wide_csv_storage import WideCsvTelemetryStorage

logger = logging.getLogger(__name__)

DEFAULT_STALENESS_S = 5.0
"""How long the run-state stream must be quiet before its run is finalized.

The state stream publishes unconditionally every STATE_PUBLISH_INTERVAL_S, so
this is a hundred missed ticks - prompt enough that a finished run's files
close within seconds of it ending, generous enough that finalizing early would
require the publisher thread to be starved for five continuous seconds. Early
finalization is the failure that matters, because a later frame for a
finalized run is ignored rather than recorded.

One window serves both routing and finalization deliberately. Frames arriving
in the tail - after a crash, before finalization - are still attributed to the
run, so its own record contains the first seconds of whatever the hardware did
once nothing was supervising it. After finalization the same device's frames
land in the per-session file instead. Continuous either way, never twice."""


@dataclass
class _DeviceTrack:
    """Per-device accounting. Kept per device because `seq` is assigned by
    each driver independently - tracking one counter across devices would
    invent gaps every time two devices' frames interleave."""

    frame_count: int = 0
    last_seq: Optional[int] = None
    seq_gap_count: int = 0
    first_t: Optional[float] = None
    last_t: Optional[float] = None

    def observe(self, seq: int, t: float) -> None:
        self.frame_count += 1
        if self.first_t is None:
            self.first_t = t
        self.last_t = t
        if self.last_seq is None:
            self.last_seq = seq
            return
        if seq > self.last_seq + 1:
            self.seq_gap_count += seq - self.last_seq - 1
        if seq > self.last_seq:
            self.last_seq = seq

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frame_count": self.frame_count,
            "seq_gap_count": self.seq_gap_count,
            "first_t": self.first_t,
            "last_t": self.last_t,
        }


@dataclass
class _Track:
    test_name: str = "unknown"
    devices: Dict[str, _DeviceTrack] = field(default_factory=dict)
    dropped_frames: int = 0
    state_first_t: Optional[float] = None
    state_last_t: Optional[float] = None
    """The wall-clock window the run-state stream itself covered. Used as the
    span for a run that produced no device frames at all - a test can declare
    devices that never stream (an acquisition device idles until acquisition is
    started), and a synthesized CRASHED verdict for such a run would otherwise
    claim it ran at the epoch."""

    def completeness(self) -> Dict[str, Any]:
        """The honest account of what the best-effort transport delivered.

        Two loss counters, deliberately separate: seq_gap_count is frames
        lost *in transit* (a PUB/SUB drop), dropped_frames is frames the
        engine received but couldn't write fast enough (a full writer
        queue). They have different fixes, so conflating them would hide
        which one is actually happening."""
        return {
            "frame_count": sum(d.frame_count for d in self.devices.values()),
            "seq_gap_count": sum(d.seq_gap_count for d in self.devices.values()),
            "dropped_frames": self.dropped_frames,
            "devices": {name: track.to_dict() for name, track in self.devices.items()},
        }

    def span(self) -> tuple[float, float]:
        """The run's observed window, preferring the frames it actually
        recorded and falling back to how long it announced itself for."""
        firsts = [d.first_t for d in self.devices.values() if d.first_t is not None]
        lasts = [d.last_t for d in self.devices.values() if d.last_t is not None]
        if firsts and lasts:
            return (min(firsts), max(lasts))
        return (self.state_first_t or 0.0, self.state_last_t or 0.0)


@dataclass
class _OpenRun:
    """The run the state stream currently says is open."""

    test_id: str
    test_name: str
    devices: Set[str]
    state: Dict[str, Any]
    last_seen_mono: float

    def claims(self, device: str) -> bool:
        return device in self.devices


class RunRecorder:
    def __init__(
        self,
        output_dir,
        storage: WideCsvTelemetryStorage,
        staleness_s: float = DEFAULT_STALENESS_S,
    ):
        self._output_dir = output_dir
        self._storage = storage
        self._staleness_s = staleness_s
        self._tracks: Dict[str, _Track] = {}
        self._finalized: set[str] = set()
        self._open: Optional[_OpenRun] = None
        self._superseded: List[tuple[str, str]] = []
        """Runs displaced by a new test_id, awaiting finalization on the next
        reconcile tick - see observe_state."""

    # ---- run tracking, from the run-state stream ----

    def observe_state(self, frame: RunStateFrame, now: Optional[float] = None) -> None:
        """Note that a run is open (or still open), and adopt its declared
        devices and current state.

        A different test_id finalizes the previous run immediately rather than
        waiting out staleness: two runs never overlap on one stand, so a new
        run announcing itself is proof the old one is done."""
        now = time.monotonic() if now is None else now
        if frame.test_id in self._finalized:
            return
        if self._open is not None and self._open.test_id != frame.test_id:
            # Queue the superseded run for the next reconcile tick rather than
            # finalizing inline: finalization is async (it writes files) and
            # this is called from the hot consume path, which should not block
            # on disk. A tick's delay costs nothing - nobody is waiting on a
            # finished run's files closing.
            logger.info("test %s: superseded by %s - finalizing", self._open.test_id, frame.test_id)
            self._superseded.append((self._open.test_id, self._open.test_name))
        self._open = _OpenRun(
            test_id=frame.test_id,
            test_name=frame.test_name,
            devices=set(frame.devices),
            state=dict(frame.state),
            last_seen_mono=now,
        )
        # The track exists from the moment a run announces itself, not from its
        # first frame - so a run that never produces one still has an identity
        # and a window to report.
        track = self._tracks.setdefault(frame.test_id, _Track())
        track.test_name = frame.test_name
        if track.state_first_t is None:
            track.state_first_t = frame.t
        track.state_last_t = frame.t

    def route(self, device: str, now: Optional[float] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Where this device's frames belong: the open run's test_id and the
        state to merge into its rows, or None for the per-session record.

        One call rather than "which run?" followed by "and its state?" - the
        caller always wants both, and asking twice meant two lookups per frame
        on the engine's hot path."""
        run = self._open_run(now)
        if run is None or not run.claims(device):
            return None
        return run.test_id, run.state

    def _open_run(self, now: Optional[float] = None) -> Optional[_OpenRun]:
        """The run the state stream says is open, or None if it has gone quiet."""
        if self._open is None:
            return None
        now = time.monotonic() if now is None else now
        if now - self._open.last_seen_mono >= self._staleness_s:
            return None
        return self._open

    # ---- per-run frame accounting ----

    def observe(self, frame: TelemetryFrame, test_id: str, now: Optional[float] = None) -> None:
        """Account one frame against the run it was attributed to.

        Frames for an already-finalized run are ignored. Finalizing pops the
        track, so without this a straggler would start a fresh track holding
        just that frame, and the next tick would finalize it again -
        overwriting a correct completeness record with a count of 1."""
        if test_id in self._finalized:
            return
        now = time.monotonic() if now is None else now
        track = self._tracks.setdefault(test_id, _Track())
        track.devices.setdefault(frame.device, _DeviceTrack()).observe(frame.seq, frame.t)

    def note_dropped(self, test_id: str) -> None:
        """Record that a received frame couldn't be written (writer queue
        full). Counted per run so completeness can report it separately
        from frames lost in transit."""
        if test_id in self._finalized:
            return
        self._tracks.setdefault(test_id, _Track()).dropped_frames += 1

    # ---- finalization ----

    async def reconcile(self, now: Optional[float] = None) -> None:
        """One tick: finalize any superseded run, and the open run if its
        state stream has gone quiet."""
        now = time.monotonic() if now is None else now

        superseded, self._superseded = self._superseded, []
        for test_id, test_name in superseded:
            await self._finalize_id(test_id, test_name)

        if self._open is None:
            return
        if now - self._open.last_seen_mono < self._staleness_s:
            return
        test_id, test_name = self._open.test_id, self._open.test_name
        self._open = None
        await self._finalize_id(test_id, test_name)

    async def _finalize_id(self, test_id: str, test_name: str) -> None:
        """Finalize a run by id, whether it went quiet or was superseded."""
        await self._finalize(test_id, self._tracks.get(test_id) or _Track(test_name=test_name))

    async def flush(self) -> None:
        """Finalize every run we've seen (engine shutdown). Still writes
        completeness onto verdicts already on disk, but does not synthesize
        CRASHED for a run with no verdict - it may simply still be
        running."""
        self._open = None
        for test_id, track in list(self._tracks.items()):
            await self._finalize(test_id, track, synthesize=False)

    async def _finalize(self, test_id: str, track: _Track, synthesize: bool = True) -> None:
        path = verdict_path(self._output_dir, test_id)
        completeness = track.completeness()

        if path.exists():
            if amend_completeness(path, completeness):
                logger.info(
                    "test %s (%s): recorded - %d frames, %d seq gaps, %d dropped",
                    test_id, track.test_name, completeness["frame_count"],
                    completeness["seq_gap_count"], completeness["dropped_frames"],
                )
            else:
                # Present but unreadable. Do NOT overwrite it with a
                # synthesized verdict - the test's own record is the
                # authority, and a corrupt one is a thing to investigate,
                # not to silently replace.
                logger.error("test %s: verdict at %s exists but couldn't be parsed - leaving it alone", test_id, path)
        elif synthesize:
            started_at, ended_at = track.span()
            logger.warning(
                "test %s (%s): state stream quiet for %.0fs with no verdict - recording CRASHED",
                test_id, track.test_name, self._staleness_s,
            )
            write_verdict(
                Verdict(
                    test_id=test_id,
                    test_name=track.test_name,
                    lifecycle=Lifecycle.CRASHED,
                    bounds_result=BoundsResult.NOT_EVALUATED,
                    started_at=started_at,
                    ended_at=ended_at,
                    reason=(
                        f"the run-state stream went quiet for >{self._staleness_s:.0f}s and the test "
                        "process never wrote a verdict - it was killed or crashed outright"
                    ),
                    completeness=completeness,
                ),
                self._output_dir,
            )
        else:
            return  # still running; leave it tracked for the next engine

        self._storage.close_run(test_id)
        self._tracks.pop(test_id, None)
        self._finalized.add(test_id)
