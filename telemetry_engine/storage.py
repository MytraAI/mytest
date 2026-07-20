"""Abstract storage interface for the telemetry engine's merged stream.

Mirrors the HardwareBackend pattern used on the hardware side: a
minimal interface that the rest of the system depends on instead of a
concrete implementation. This lets a real time-series database (e.g.
InfluxDB, TimescaleDB - see the architecture doc's open decisions) get
plugged in later without touching main.py or the aggregator.
CsvStorage is the only concrete implementation today.

`write()` takes one merged item (a raw TelemetryFrame or a
TaggedTelemetryFrame) and decomposes it into one point per channel,
not a single row per frame. This is the shape a real time-series
database wants: one write per tag/field/timestamp combination (e.g.
InfluxDB's line protocol). Each channel value in a frame shares the
same seq/t/test_id.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Union

from hardware.protocol import TaggedTelemetryFrame, TelemetryFrame

MergedItem = Union[TelemetryFrame, TaggedTelemetryFrame]


class TelemetryStorage(ABC):
    """Interface the telemetry engine depends on instead of a concrete storage backend."""

    @abstractmethod
    async def write(self, item: MergedItem) -> None:
        """Persist one merged item, decomposed into one point per channel."""

    @abstractmethod
    async def close(self) -> None:
        """Flush and release any resources (file handles, connections, ...)."""
