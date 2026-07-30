"""Wide CSV telemetry storage: one row per frame, one column per channel,
one file per device per run.

Wide rather than long/tidy (one row per channel *value*): the "one point per
tag/field/timestamp" shape a TSDB wants is a loop inside a
TelemetryStorage.write() implementation, not a property of the interim file,
and the long form repeated the test_id and channel name for every value.
Measured ~15x smaller on a ~100-channel device, and it opens in a
spreadsheet without a pivot.

Routing. One instance fans out on the destination each WriteItem already
names: items carrying a test_id go to
<output>/runs/<test_id>/<device>/telemetry.csv, items without one go to
<output>/raw/<device>/telemetry_<session>.csv (see protocol/paths.py). This
layer does not decide which - the engine does, from the open run's declared
devices (see run_recorder.py). Files open lazily and in append mode, so an
engine restart mid-run keeps adding to the same file rather than truncating it
or starting a second one.

The header problem. A wide file's columns are fixed when the header is
written, but the full channel set isn't knowable from frame one - a
run-attributed row carries test-published state channels that appear only once
something sets them. So each writer buffers HEADER_SAMPLE_FRAMES frames,
takes the union of their channel names, then writes header and buffer. After
that the schema is fixed: a later channel is logged once and dropped, and a
channel missing from a frame is an empty cell. Both stay visible in the file
rather than silent.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TextIO, Tuple

from protocol.paths import raw_telemetry_path, run_telemetry_path
from protocol.wire import UNKNOWN_DEVICE

from .storage import TelemetryStorage, WriteItem

logger = logging.getLogger(__name__)

HEADER_SAMPLE_FRAMES = 50
"""Frames buffered before the header is fixed - a second or two at the rates
devices here run. Long enough for the test's published state channels to
appear, short enough that little is held in memory or delayed on disk."""

_FIXED_COLUMNS = ["seq", "t"]


class _WideCsvWriter:
    """One wide CSV file: buffers to establish its header, then appends."""

    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file: Optional[TextIO] = None
        self._writer: Optional[Any] = None
        self._columns: List[str] = []
        self._known_columns: Set[str] = set()
        """The header as a set, and the channel columns as a list, both built
        once when the header is fixed. Rebuilding them per row cost a
        set construction per column on every frame, in the hot path."""
        self._channel_columns: List[str] = []
        self._buffer: List[Tuple[int, float, Dict[str, Any]]] = []
        self._pending_channels: Set[str] = set()
        self._unknown_logged: Set[str] = set()
        self._rows = 0

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def path(self) -> Path:
        return self._path

    def write(self, seq: int, t: float, channels: Dict[str, Any]) -> None:
        if self._writer is None:
            self._buffer.append((seq, t, dict(channels)))
            self._pending_channels.update(channels)
            if len(self._buffer) >= HEADER_SAMPLE_FRAMES:
                self._open()
            return
        self._write_row(seq, t, channels)

    def _open(self) -> None:
        """Fix the header from the sampled channel union and flush the
        buffer. Appends rather than truncates: if the file already exists
        (an engine restart mid-run) its header is authoritative, so it's
        reused and this session's extra channels, if any, are dropped the
        same way any late channel is."""
        existing_header: Optional[List[str]] = None
        if self._path.exists() and self._path.stat().st_size > 0:
            try:
                with open(self._path, "r", newline="") as handle:
                    first = handle.readline().strip()
                if first:
                    existing_header = next(csv.reader([first]))
            except OSError:
                logger.warning("couldn't read existing header from %s; rewriting", self._path, exc_info=True)

        if existing_header:
            self._columns = existing_header
            logger.info("appending to existing wide telemetry file %s (%d columns)", self._path, len(self._columns))
        else:
            self._columns = _FIXED_COLUMNS + sorted(self._pending_channels)
            logger.info("writing wide telemetry to %s (%d columns)", self._path, len(self._columns))

        self._known_columns = set(self._columns)
        self._channel_columns = self._columns[len(_FIXED_COLUMNS):]
        self._file = open(self._path, "a", newline="")
        self._writer = csv.writer(self._file)
        if not existing_header:
            self._writer.writerow(self._columns)

        buffered, self._buffer = self._buffer, []
        for seq, t, channels in buffered:
            self._write_row(seq, t, channels)

    def _write_row(self, seq: int, t: float, channels: Dict[str, Any]) -> None:
        assert self._writer is not None
        for name in channels:
            if name not in self._known_columns and name not in self._unknown_logged:
                self._unknown_logged.add(name)
                logger.warning(
                    "%s: channel %r appeared after the header was fixed - dropping it from this file",
                    self._path.name, name,
                )
        row = [seq, t] + [channels.get(name, "") for name in self._channel_columns]
        self._writer.writerow(row)
        self._rows += 1

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush()

    def close(self) -> None:
        """Flush and close. A file that never reached HEADER_SAMPLE_FRAMES
        is opened here so a short run still produces a real file rather
        than losing its handful of frames."""
        if self._writer is None and self._buffer:
            self._open()
        if self._file is not None:
            self._file.close()
            self._file = None
            self._writer = None


class WideCsvTelemetryStorage(TelemetryStorage):
    """Fans merged frames out to one wide CSV per device per run."""

    def __init__(self, output_dir: Path, session: str):
        self._output_dir = Path(output_dir)
        self._session = session
        self._writers: Dict[Tuple[Optional[str], str], _WideCsvWriter] = {}
        self._written: Dict[Path, int] = {}
        """Every file this instance has written to, and its row count -
        kept separately from _writers because that dict is emptied by
        close()/close_run(), and a caller reporting on what was produced
        (a demo, a summary) naturally asks *after* closing. Reading it off
        the open-writer dict silently returned nothing."""

    def _record(self, writer: _WideCsvWriter) -> None:
        self._written[writer.path] = writer.rows

    def paths(self) -> List[Path]:
        """Every file written to, whether still open or already closed."""
        for writer in self._writers.values():
            self._record(writer)
        return sorted(self._written)

    def row_counts(self) -> Dict[str, int]:
        for writer in self._writers.values():
            self._record(writer)
        return {str(path): rows for path, rows in sorted(self._written.items())}

    async def write(self, item: WriteItem) -> None:
        device = item.device or UNKNOWN_DEVICE
        test_id = item.test_id
        writer = self._writers.get((test_id, device))
        if writer is None:
            path = (
                run_telemetry_path(self._output_dir, test_id, device)
                if test_id is not None
                else raw_telemetry_path(self._output_dir, device, self._session)
            )
            writer = _WideCsvWriter(path)
            self._writers[(test_id, device)] = writer
        writer.write(item.seq, item.t, item.channels)

    def flush(self) -> None:
        for writer in self._writers.values():
            writer.flush()

    def close_run(self, test_id: str) -> None:
        """Close just this run's files, once its stream has gone quiet, so
        a finished run's record is complete on disk without waiting for
        the engine itself to shut down.

        A frame for this run still sitting in the engine's write queue when this
        runs will recreate its writer and append to the same file - correct, not
        a leak of data, but that writer then stays open until engine shutdown
        and row_counts() will report only the rows written after the reopen.
        Harmless today; worth knowing before anyone treats close_run as final."""
        for key in [k for k in self._writers if k[0] == test_id]:
            writer = self._writers.pop(key)
            writer.close()
            self._record(writer)

    async def close(self) -> None:
        for writer in list(self._writers.values()):
            writer.close()
            self._record(writer)
        self._writers.clear()
