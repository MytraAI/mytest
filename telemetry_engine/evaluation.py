"""Evaluator: a per-test_id multiplexer over RulebookEvaluator.

Applies the exact same bound-transition-tracking logic MainExecution
uses live (see testcases/asimov/rulebook.py), but runs it here
post-hoc against tagged frames from the aggregator's merged stream. A
fresh RulebookEvaluator is created the first time a given test_id is
seen, and registered with every Rulebook whose test_names includes
that frame's test_name.

This is a terminal sink, same as storage: it has no way to influence a
running test. See testcases/asimov/rulebook.py's docstring for why
that's a Rulebook property, not just an Evaluator one.

Untagged (raw) frames aren't evaluated at all. Rulebooks are matched
by Rulebook.test_names, and only TaggedTelemetryFrame carries a test_name.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal

from hardware.protocol import TaggedTelemetryFrame
from testcases.asimov.rulebook import Rulebook, RulebookEvaluator

from .storage import MergedItem


@dataclass(frozen=True)
class ViolationEvent:
    """One bound's pass/fail transition, for one test run."""

    test_id: str
    test_name: str
    rulebook_name: str
    bound_label: str
    channel: str
    value: Any
    seq: int
    t: float
    transition: Literal["violated", "cleared"]


class Evaluator:
    """Evaluates registered Rulebooks against tagged frames, emitting an event on each violation/clear transition."""

    def __init__(self) -> None:
        self._rulebooks_by_test_name: Dict[str, List[Rulebook]] = {}
        self._trackers_by_test_id: Dict[str, RulebookEvaluator] = {}

    def register(self, rulebook: Rulebook) -> None:
        for test_name in rulebook.test_names:
            self._rulebooks_by_test_name.setdefault(test_name, []).append(rulebook)

    def evaluate(self, item: MergedItem) -> List[ViolationEvent]:
        if not isinstance(item, TaggedTelemetryFrame):
            return []

        tracker = self._trackers_by_test_id.get(item.test_id)
        if tracker is None:
            tracker = RulebookEvaluator()
            for rulebook in self._rulebooks_by_test_name.get(item.test_name, []):
                tracker.register(rulebook)
            self._trackers_by_test_id[item.test_id] = tracker

        return [
            ViolationEvent(
                test_id=item.test_id,
                test_name=item.test_name,
                rulebook_name=transition.rulebook_name,
                bound_label=transition.bound_label,
                channel=transition.channel,
                value=transition.value,
                seq=item.seq,
                t=item.t,
                transition="violated" if transition.violated else "cleared",
            )
            for transition in tracker.evaluate(item.channels, item.t)
        ]
