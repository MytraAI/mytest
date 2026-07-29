"""Abstract storage interface for the telemetry engine's merged stream.

Mirrors the HardwareBackend pattern used on the hardware side: a
minimal interface that the rest of the system depends on instead of a
concrete implementation. This lets a real time-series database (e.g.
InfluxDB, TimescaleDB - see the architecture doc's open decisions) get
plugged in later without touching main.py or the aggregator.
WideCsvTelemetryStorage is the only concrete implementation today.

`write()` takes one merged item (a raw TelemetryFrame or a
TaggedTelemetryFrame) and is free to persist it however suits the store.
A TSDB implementation would decompose it into one point per
tag/field/timestamp combination (e.g. InfluxDB's line protocol); the CSV
implementation writes one row per frame with a column per channel. That
decomposition is a detail of each implementation, not a property of this
interface - which is why the interim files aren't stored in point form
just because a future database will want it that way.

Note the routing an implementation is expected to handle: a
TaggedTelemetryFrame belongs to a specific test run (test_id) while a
raw TelemetryFrame does not, and every frame carries the `device` that
produced it. See wide_csv_storage.py and protocol/paths.py.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Union

from protocol.wire import TaggedTelemetryFrame, TelemetryFrame

MergedItem = Union[TelemetryFrame, TaggedTelemetryFrame]


class TelemetryStorage(ABC):
    """Interface the telemetry engine depends on instead of a concrete storage backend."""

    @abstractmethod
    async def write(self, item: MergedItem) -> None:
        """Persist one merged item, decomposed into one point per channel."""

    @abstractmethod
    async def close(self) -> None:
        """Flush and release any resources (file handles, connections, ...)."""
