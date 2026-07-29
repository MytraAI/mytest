"""Engine-side per-run bookkeeping: stamp completeness onto each run's
verdict, and record a verdict for a run whose test process died without
writing one.

The engine feeds every tagged frame through observe(), which accounts it
against that test_id (per device, since seq is only meaningful per
device), and calls reconcile() on a periodic tick. When a run's tagged
stream has been quiet for staleness_s, that run is finalized:

  1. Its verdict.json - written by the test process itself, before its own
     teardown - is amended in place with the completeness stats only the
     engine can produce.
  2. If no verdict.json exists, the test process died without writing one
     (taskkill /F, SIGKILL, a hard crash), and a lifecycle=CRASHED verdict
     is synthesized so the run leaves a record instead of vanishing.
  3. That run's telemetry files are closed, so a finished run's record is
     complete on disk without waiting for engine shutdown.

Why there's no spool directory or settle window any more. The verdict used
to be relayed through a spool dir in the system tempdir, drained here,
held for a couple of seconds so trailing frames could be counted, then
written to a store this module owned. All of that machinery existed to
decouple the two processes' lifetimes and to guess when a run's frames had
finished arriving. Neither problem remains: the test now writes straight
into its own run directory (a location both sides derive from
protocol/paths.py), and completeness is only stamped once the stream has
*already* gone quiet, so trailing frames are counted by construction
rather than by a timing heuristic. It also removes a real bug the old
three-set (claimed/pending/done) bookkeeping had: a teardown slower than
the staleness window would get an INCOMPLETE verdict synthesized *and*
then the real verdict written too, as two contradictory records. Here a
late verdict simply exists, and existence is the whole check.

flush() finalizes everything immediately at engine shutdown, but never
synthesizes CRASHED for a still-active run: a run in progress when the
engine stops isn't crashed. Note the engine stopping is itself something a
test notices and aborts on (see protocol/heartbeat.py), so this is a
narrow window by design.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from protocol.paths import verdict_path
from protocol.verdict import (
    BoundsResult,
    Lifecycle,
    Verdict,
    amend_completeness,
    write_verdict,
)
from protocol.wire import TaggedTelemetryFrame

from .wide_csv_storage import WideCsvTelemetryStorage

logger = logging.getLogger(__name__)

DEFAULT_STALENESS_S = 15.0
"""How long a run's tagged stream must be quiet before it's finalized -
~3x the telemetry client's own 5 s staleness deadline, so a live stream
never reaches it. A test's publisher stops during post_test_teardown(),
which is also when its verdict is already on disk (the test writes it
before tearing down), so in the normal case the verdict is waiting well
before this fires."""


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
    last_seen_mono: float = 0.0

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
        firsts = [d.first_t for d in self.devices.values() if d.first_t is not None]
        lasts = [d.last_t for d in self.devices.values() if d.last_t is not None]
        return (min(firsts) if firsts else 0.0, max(lasts) if lasts else 0.0)


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

    def observe(self, frame: TaggedTelemetryFrame, now: Optional[float] = None) -> None:
        """Account one tagged frame against its run's telemetry stats.

        Frames for an already-finalized run are ignored. Finalizing pops the
        track, so without this a straggler frame arriving afterwards would
        start a fresh track holding just that frame, and the next tick would
        finalize it again - overwriting a correct completeness record with a
        stragglers-only count of 1.
        """
        if frame.test_id in self._finalized:
            return
        now = time.monotonic() if now is None else now
        track = self._tracks.setdefault(frame.test_id, _Track())
        track.test_name = frame.test_name
        track.devices.setdefault(frame.device, _DeviceTrack()).observe(frame.seq, frame.t)
        track.last_seen_mono = now

    def note_dropped(self, frame: TaggedTelemetryFrame) -> None:
        """Record that a received frame couldn't be written (writer queue
        full). Counted per run so completeness can report it separately
        from frames lost in transit."""
        if frame.test_id in self._finalized:
            return
        track = self._tracks.setdefault(frame.test_id, _Track())
        track.dropped_frames += 1

    async def reconcile(self, now: Optional[float] = None) -> None:
        """One tick: finalize any run whose stream has gone quiet."""
        now = time.monotonic() if now is None else now
        for test_id, track in list(self._tracks.items()):
            if now - track.last_seen_mono < self._staleness_s:
                continue
            await self._finalize(test_id, track)

    async def flush(self) -> None:
        """Finalize every run we've seen (engine shutdown). Still writes
        completeness onto verdicts already on disk, but does not synthesize
        CRASHED for a run with no verdict - it may simply still be
        running."""
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
                "test %s (%s): stream quiet for %.0fs with no verdict - recording CRASHED",
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
                        f"telemetry stream went quiet for >{self._staleness_s:.0f}s and the test process "
                        "never wrote a verdict - it was killed or crashed outright"
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
