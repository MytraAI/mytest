"""CSV implementation of ResultStorage. Local, durable, no external
dependencies. One timestamped file per process run, same convention as
CsvStorage (see csv_storage.py).
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import TextIO

from .evaluation import ViolationEvent
from .result_storage import ResultStorage

logger = logging.getLogger(__name__)

CSV_HEADER = ["test_id", "test_name", "rulebook_name", "bound_label", "channel", "value", "seq", "t", "transition"]


class CsvResultStorage(ResultStorage):
    """Appends violation/clear transition events to a local CSV file, one row per event."""

    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file: TextIO = open(self._path, "w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(CSV_HEADER)
        logger.info("writing evaluation results to %s", self._path)

    async def write(self, event: ViolationEvent) -> None:
        self._writer.writerow(
            [
                event.test_id,
                event.test_name,
                event.rulebook_name,
                event.bound_label,
                event.channel,
                event.value,
                event.seq,
                event.t,
                event.transition,
            ]
        )
        self._file.flush()

    async def close(self) -> None:
        self._file.close()
