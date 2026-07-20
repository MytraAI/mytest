"""Abstract storage interface for evaluation results (ViolationEvents).

Kept separate from TelemetryStorage (storage.py) because the
architecture doc distinguishes raw/derived time-series data from
pass/fail summaries; the two get persisted differently (a
report/relational store, not a time-series one). Otherwise this
follows the same pattern as TelemetryStorage: a minimal interface,
with CsvResultStorage the only implementation today.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .evaluation import ViolationEvent


class ResultStorage(ABC):
    """Interface the telemetry engine depends on instead of a concrete results store."""

    @abstractmethod
    async def write(self, event: ViolationEvent) -> None:
        """Persist one violation/clear transition event."""

    @abstractmethod
    async def close(self) -> None:
        """Flush and release any resources (file handles, connections, ...)."""
