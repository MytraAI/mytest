"""The per-test verdict record: one authoritative outcome per test run.

How the run ended, whether the DUT stayed inside its bounds, every bound
transition, and how much telemetry backs the record. The time-series lives
beside it as per-device CSVs (see paths.py), joined by sharing a directory.

The test process authors it - it alone knows how the run ended, and it holds
the evaluator that actually gated the run - and writes it into the run
directory before its own teardown. The telemetry engine only adds
`completeness`, the one field it alone can produce, and synthesizes a
CRASHED record for a run whose process died without writing one.

`lifecycle` and `bounds_result` are separate fields because they're
independent. A test that runs until an operator stops it ends deliberately
and may still be a success (STOPPED/PASS); one that completes normally may
still have violated a bound. A single enum makes the first case
inexpressible - it has to file a stopped run as either not-a-failure (hiding
any violations from a query) or a failure (condemning a good run). Two small
closed enums also map to two indexed database columns.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import verdict_path

logger = logging.getLogger(__name__)


class Lifecycle:
    """How the run ended. All but CRASHED are authored by the test
    process; CRASHED is only ever synthesized by the engine."""

    COMPLETED = "COMPLETED"  # main_execution() returned normally
    STOPPED = "STOPPED"  # deliberate operator stop (StopRequested / SIGTERM / Ctrl+C)
    ERRORED = "ERRORED"  # an exception propagated out - including a fatal bound abort
    CRASHED = "CRASHED"  # engine-synthesized: stream went stale, no verdict was ever written


class BoundsResult:
    """Whether the DUT stayed inside its Rulebook's bounds."""

    PASS = "PASS"  # evaluation ran and no bound ever violated
    FAIL = "FAIL"  # at least one bound violated at some point, fatal or not
    NOT_EVALUATED = "NOT_EVALUATED"  # the runner never evaluated a single frame


@dataclass
class Violation:
    """One bound transition - a violate or a clear - as the live
    evaluator saw it.

    Carries the frame's own seq/t (not wall-clock at record time) so the
    timeline can be lined up against the stored telemetry, and replayed
    against it (see telemetry_engine/replay.py).
    """

    bound_label: str
    rulebook_name: str
    channel: str
    value: Any
    fatal: bool
    transition: str  # "violated" | "cleared"
    seq: int
    t: float


@dataclass
class Verdict:
    """One test run's authoritative record, keyed by test_id.

    `violations` is the *full* transition timeline, not a summary: every
    violate and every clear, in order. It lives here rather than in a
    separate store because a run's result is one record - splitting the
    timeline into a second file written by a different process on a
    different lifecycle is how a verdict ends up referencing violations
    nobody recorded. Ports to a database as one row plus one child table.

    `metadata` is a freeform bag the test attaches (tuning profile,
    setpoints, DUT serial, operator, git SHA); `completeness` is added by
    the engine at record time and is the honest account of what the
    best-effort PUB/SUB transport actually delivered. `dut` is a real
    field rather than part of that bag: which stand produced a run is
    structural, and it decides where the run is filed.
    """

    test_id: str
    test_name: str
    lifecycle: str
    bounds_result: str
    started_at: float
    ended_at: float
    dut: str = ""
    """Which DUT package produced this run - see TestCase.DUT.

    Empty on a verdict the engine synthesized, which knows the run's id and
    name but not the class that was running. The key's presence is what tells
    a reader this verdict was written by a version that records it at all."""
    reason: str = ""
    any_fatal: bool = False
    violations: List[Violation] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    completeness: Optional[Dict[str, Any]] = None

    @property
    def outcome(self) -> str:
        """A display-only join of the two real fields, for logs and the
        operator dashboard. Deliberately derived and never stored as the
        source of truth - query lifecycle/bounds_result, not this."""
        return f"{self.lifecycle}/{self.bounds_result}"

    @property
    def duration_s(self) -> float:
        return max(0.0, self.ended_at - self.started_at)

    def violated_bounds(self) -> List[str]:
        """Distinct bound labels that violated at any point, in first-seen
        order - the short answer to "what went wrong"."""
        seen: List[str] = []
        for violation in self.violations:
            if violation.transition == "violated" and violation.bound_label not in seen:
                seen.append(violation.bound_label)
        return seen

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["outcome"] = self.outcome  # derived, for readability of the raw file
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Verdict":
        return cls(
            test_id=data["test_id"],
            test_name=data["test_name"],
            lifecycle=data["lifecycle"],
            dut=data.get("dut", ""),
            bounds_result=data["bounds_result"],
            started_at=data["started_at"],
            ended_at=data["ended_at"],
            reason=data.get("reason", ""),
            any_fatal=data.get("any_fatal", False),
            violations=[Violation(**v) for v in data.get("violations", [])],
            metadata=data.get("metadata", {}),
            completeness=data.get("completeness"),
        )


def write_verdict(verdict: Verdict, output_dir: Path) -> Path:
    """Write a verdict into its run directory, atomically.

    Writes to a temp file then os.replace()s it into place, so a reader
    (the engine, coming to add completeness) never sees a half-written
    file. Atomic within the same filesystem, which holds here since the
    temp file is created in the destination directory.
    """
    final = verdict_path(output_dir, verdict.test_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(verdict.to_dict(), indent=2, sort_keys=True))
    os.replace(tmp, final)
    return final


def read_verdict(path: Path) -> Verdict:
    """Parse a verdict file. Raises on a missing/corrupt/incomplete file
    (OSError / ValueError / KeyError / TypeError) - callers that poll for
    one appearing treat any of those as "not there yet"."""
    return Verdict.from_dict(json.loads(Path(path).read_text()))


def amend_completeness(path: Path, completeness: Dict[str, Any]) -> bool:
    """Add completeness stats to an already-written verdict, atomically.

    Returns True if the file was amended, False if it couldn't be read
    (absent or corrupt) - the engine treats False as "nothing to amend"
    and moves on rather than raising, since a missing verdict is a
    condition it already handles by synthesizing CRASHED.
    """
    try:
        verdict = read_verdict(path)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    verdict.completeness = completeness
    tmp = Path(path).with_suffix(f".json.{os.getpid()}.amend.tmp")
    tmp.write_text(json.dumps(verdict.to_dict(), indent=2, sort_keys=True))
    os.replace(tmp, path)
    return True
