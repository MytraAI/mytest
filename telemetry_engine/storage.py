"""Abstract storage interface for the telemetry engine, and the one item
type it persists.

Mirrors the HardwareBackend pattern used on the hardware side: a
minimal interface that the rest of the system depends on instead of a
concrete implementation. This lets a real time-series database (e.g.
InfluxDB, TimescaleDB - see the architecture doc's open decisions) get
plugged in later without touching main.py or the aggregator.
WideCsvTelemetryStorage is the only concrete implementation today.

`write()` takes one WriteItem and is free to persist it however suits the
store. A TSDB implementation would decompose it into one point per
tag/field/timestamp combination (e.g. InfluxDB's line protocol); the CSV
implementation writes one row per frame with a column per channel. That
decomposition is a detail of each implementation, not a property of this
interface - which is why the interim files aren't stored in point form
just because a future database will want it that way.

**Routing is decided before storage sees an item, not by storage.** A
WriteItem carries `test_id` set when the engine has attributed the frame to
an open run, and None when it hasn't; storage just honours it. Previously
this was inferred from the item's *type* - a tagged frame meant "belongs to a
run" - which put a policy question inside the layer that is supposed to be
swappable. The engine owns that decision now (see main.py and
run_recorder.py) and storage stays a dumb writer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

@dataclass
class WriteItem:
    """One row to persist, with its destination already decided.

    `channels` is what actually gets written, so for a run-attributed frame it
    is the device's channels *plus* the open run's published state merged in -
    the engine does that merge, which is why the recorded file still shows what
    the test was doing on every frame without telemetry ever passing through
    the test process.

    `seq` and `t` are the publishing driver's own, carried through untouched, so
    a rule transition recorded in a verdict points at an identifiable row here
    (see protocol/wire.py and AI/mytest-vs-forge.md §2.5)."""

    device: str
    seq: int
    t: float
    channels: Dict[str, Any] = field(default_factory=dict)
    test_id: Optional[str] = None
    """The run this row belongs to, or None for the continuous per-session
    record. Exactly one of the two destinations, never both - a frame is
    written once."""


class TelemetryStorage(ABC):
    """Interface the telemetry engine depends on instead of a concrete storage backend."""

    @abstractmethod
    async def write(self, item: WriteItem) -> None:
        """Persist one row at the destination the item names."""

    @abstractmethod
    async def close(self) -> None:
        """Flush and release any resources (file handles, connections, ...)."""
