"""Offline replay: re-evaluate a stored run's telemetry against Rulebooks.

This is what "post-hoc evaluation" is actually good for, and the reason
the shared RulebookEvaluator logic still has two callers even though only
one of them runs live. It answers questions you can only ask after the
fact:

- Would a tighter bound have caught this run? (replay with a modified
  Rulebook and see)
- Does the stored record actually explain its own verdict? (replay with
  the *same* Rulebook and compare against verdict.json)

That second use is why this exists now rather than later: it's the only
check that proves the record is self-sufficient. If replaying a run's
stored telemetry reproduces the transition timeline the live runner
recorded, then the wide per-device CSV format lost nothing, and collapsing
to a single online evaluator lost nothing either. If it *doesn't*, the
stored telemetry is missing something - which is worth discovering now,
not after a database port.

One honest caveat when comparing. A Rulebook's persistence_s debounce is
timed off frame timestamps, so frames lost in transit (the transport is
best-effort by design - see the verdict's completeness stats) make replay
see a gap the live runner never saw, and a debounced bound can legitimately
resolve differently. A mismatch on a run with a non-zero seq_gap_count is
the completeness numbers telling you they matter, not a bug in replay.

Reads the wide CSV written by wide_csv_storage.py: one row per frame,
`seq`/`t` then one column per channel. Empty cells mean the channel wasn't
in that frame and are omitted from the reconstructed frame, so a Bound
sees a genuinely absent channel as absent (Bound.evaluate returns None)
rather than as a zero.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from protocol.paths import TELEMETRY_FILENAME, VERDICT_FILENAME
from protocol.verdict import Verdict, Violation, read_verdict
from testcases.asimov.rulebook import Rulebook, RulebookEvaluator

logger = logging.getLogger(__name__)


@dataclass
class ReplayFrame:
    seq: int
    t: float
    channels: Dict[str, Any]


def _coerce(raw: str) -> Any:
    """Best-effort scalar recovery from CSV text. Numbers come back as
    numbers so numeric bounds compare correctly; bool-looking values come
    back as bools so an `expected=True` bound matches; anything else stays
    a string (Bound.expected can gate on discrete string states)."""
    if raw in ("True", "False"):
        return raw == "True"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def read_frames(path: Path) -> Iterator[ReplayFrame]:
    """Stream frames from a wide telemetry CSV, in file order."""
    with open(path, "r", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            channels = {
                name: _coerce(value)
                for name, value in row.items()
                if name not in ("seq", "t") and value not in ("", None)
            }
            yield ReplayFrame(seq=int(row["seq"]), t=float(row["t"]), channels=channels)


def replay(path: Path, rulebooks: List[Rulebook]) -> List[Violation]:
    """Evaluate a stored run's telemetry and return the transition
    timeline, in the same shape the live runner records - so it can be
    compared to a verdict's `violations` directly."""
    evaluator = RulebookEvaluator()
    for rulebook in rulebooks:
        evaluator.register(rulebook)

    timeline: List[Violation] = []
    for frame in read_frames(path):
        for transition in evaluator.evaluate(frame.channels, frame.t):
            timeline.append(
                Violation(
                    bound_label=transition.bound_label,
                    rulebook_name=transition.rulebook_name,
                    channel=transition.channel,
                    value=transition.value,
                    fatal=transition.fatal,
                    transition="violated" if transition.violated else "cleared",
                    seq=frame.seq,
                    t=frame.t,
                )
            )
    return timeline


@dataclass
class Comparison:
    """How a replayed timeline lines up with the recorded one."""

    recorded: List[Violation]
    replayed: List[Violation]
    seq_gap_count: Optional[int] = None

    @property
    def recorded_labels(self) -> List[str]:
        return [v.bound_label for v in self.recorded if v.transition == "violated"]

    @property
    def replayed_labels(self) -> List[str]:
        return [v.bound_label for v in self.replayed if v.transition == "violated"]

    @property
    def matches(self) -> bool:
        """Compared on the sequence of (bound, transition) pairs, not on
        values or timestamps: replay reads values back through CSV text, so
        exact float equality is the wrong bar. What must hold is that the
        same bounds violated and cleared, in the same order."""
        return [(v.bound_label, v.transition) for v in self.recorded] == [
            (v.bound_label, v.transition) for v in self.replayed
        ]

    def explain(self) -> str:
        if self.matches:
            return f"replay reproduces the recorded timeline ({len(self.recorded)} transition(s))"
        detail = (
            f"recorded {len(self.recorded)} transition(s) {self.recorded_labels}, "
            f"replay produced {len(self.replayed)} {self.replayed_labels}"
        )
        if self.seq_gap_count:
            detail += (
                f" - note this run lost {self.seq_gap_count} frame(s) in transit, which can legitimately "
                "change a debounced (persistence_s) bound's outcome on replay"
            )
        return detail


def compare_with_verdict(verdict: Verdict, timeline: List[Violation]) -> Comparison:
    gaps = (verdict.completeness or {}).get("seq_gap_count")
    return Comparison(recorded=verdict.violations, replayed=timeline, seq_gap_count=gaps)


def replay_run(run_dir: Path, device: str, rulebooks: List[Rulebook]) -> Comparison:
    """Replay one stored run directory's device telemetry and compare it
    against the verdict written beside it."""
    verdict = read_verdict(run_dir / VERDICT_FILENAME)
    timeline = replay(run_dir / device / TELEMETRY_FILENAME, rulebooks)
    return compare_with_verdict(verdict, timeline)
