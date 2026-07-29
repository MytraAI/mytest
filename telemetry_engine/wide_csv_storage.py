"""Wide CSV telemetry storage: one row per frame, one column per channel,
one file per device per run.

Replaces the earlier long/tidy layout (one row per channel *value*, with
seq/t/test_id repeated on every row). The long form was justified as
"the shape a real time-series database write wants", but that
decomposition is a four-line loop inside a TelemetryStorage.write()
implementation, not a property of the interim file - a future
InfluxStorage receives the same MergedItem and decomposes it itself
regardless of what this class chose to write. Meanwhile the long form
repeated a 32-char test_id and a ~35-char channel name for every single
value, on a test case that runs indefinitely, and needed a pivot before a
human could read it. Wide opens in a spreadsheet and loads straight into
a dataframe.

Measured against real hardware, the saving is about 15x rather than the
~4x first estimated: a 117-column ODrive frame is 762 bytes wide, versus
~116 rows x ~100 bytes long. At the real device's achieved ~12.6 Hz that
is ~35 MB/hour.

Routing. One instance fans out across many files: tagged frames go to
<output>/runs/<test_id>/<device>/telemetry.csv, raw (untagged) frames to
<output>/raw/<device>/telemetry_<session>.csv. Files are opened lazily on
the first frame that needs one, and in append mode, so an engine restart
mid-run keeps adding to the same run's file instead of truncating it or
silently starting a second one.

The header problem, and how it's handled. A wide file's columns must be
fixed when the header line is written, but the full channel set isn't
knowable from frame one: the tagged stream merges in test-published state
channels (test_status, {bound_label}_status, current_step) that only
appear once something publishes them. So each writer buffers the first
HEADER_SAMPLE_FRAMES frames, takes the union of their channel names as
the header, then writes the header and the buffered rows. After that the
schema is fixed: a channel that shows up later is logged once and
dropped, and a channel missing from a frame is an empty cell. Both are
visible in the file rather than silent - an empty cell in a known column
says "this frame didn't carry it", which is exactly what you want for the
~10 channels a real ODrive reports as absent depending on board config.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TextIO, Tuple

from protocol.paths import raw_telemetry_path, run_telemetry_path
from protocol.wire import UNKNOWN_DEVICE, TaggedTelemetryFrame

from .storage import MergedItem, TelemetryStorage

logger = logging.getLogger(__name__)

HEADER_SAMPLE_FRAMES = 50
"""Frames buffered before the header is fixed - 1 s at 50 Hz, 2.5 s at
the real ODrive's 20 Hz. Long enough for the test's published state
channels to appear, short enough that nothing meaningful is held in
memory or delayed on disk."""

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
        117-element set construction on every frame in the hot path."""
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

    async def write(self, item: MergedItem) -> None:
        device = getattr(item, "device", UNKNOWN_DEVICE) or UNKNOWN_DEVICE
        test_id = item.test_id if isinstance(item, TaggedTelemetryFrame) else None
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
        the engine itself to shut down."""
        for key in [k for k in self._writers if k[0] == test_id]:
            writer = self._writers.pop(key)
            writer.close()
            self._record(writer)

    async def close(self) -> None:
        for writer in list(self._writers.values()):
            writer.close()
            self._record(writer)
        self._writers.clear()
