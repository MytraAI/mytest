"""CSV implementation of TelemetryStorage. Local, durable, no external
dependencies. One timestamped file per process run; files are never
appended across restarts, so unrelated runs' data can't mix together.

Uses a long/tidy format: one row per channel value, not per frame
(`seq, t, test_id, channel, value`). This matches the shape a real
time-series database write wants - one point per tag/field/timestamp
combination - so a future TSDB-backed TelemetryStorage implementation
will need to make the same decomposition, not a different one.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import TextIO

from hardware.protocol import TaggedTelemetryFrame

from .storage import MergedItem, TelemetryStorage

logger = logging.getLogger(__name__)

CSV_HEADER = ["seq", "t", "test_id", "channel", "value"]


class CsvStorage(TelemetryStorage):
    """Appends merged telemetry points to a local CSV file, one row per channel value."""

    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file: TextIO = open(self._path, "w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(CSV_HEADER)
        logger.info("writing telemetry to %s", self._path)

    async def write(self, item: MergedItem) -> None:
        test_id = item.test_id if isinstance(item, TaggedTelemetryFrame) else ""
        for channel, value in item.channels.items():
            self._writer.writerow([item.seq, item.t, test_id, channel, value])

    async def close(self) -> None:
        self._file.close()
