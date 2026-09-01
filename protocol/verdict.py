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


class MeasurementsResult:
    """Whether every measurement the run took satisfied its limits.

    Separate from BoundsResult because the two answer different questions
    about a run. A bound is ambient supervision the DUT lives under for the
    whole run; a measurement is a deliberate spot check a step chose to make.
    A run can keep every bound and fail a measurement, or the reverse, and a
    record that collapsed them could not say which."""

    PASS = "PASS"  # at least one measurement was taken and all of them passed
    FAIL = "FAIL"  # at least one measurement failed its limits
    NOT_TAKEN = "NOT_TAKEN"  # no measurement was taken at all
    """Zero measurements - the test defines none, or the run ended before
    reaching its first.

    NEVER means "could not be judged": a value that cannot be compared against
    its limits raises UnmeasurableError and ends the run (see
    asimov/measurement.py), so it surfaces as an ERRORED lifecycle rather than
    hiding here. A measurement's own outcome is exactly pass or fail."""


_RESULT_SEVERITY = {
    "PASS": 0,  # BoundsResult.PASS and MeasurementsResult.PASS are the same word
    "NOT_EVALUATED": 1,  # BoundsResult only
    "FAIL": 2,  # both
}
"""How the two result fields rank when outcome collapses them into one word.

Keyed by the bare strings rather than by the enum members, because the two
enums deliberately share PASS and FAIL and writing both spellings would put
duplicate keys in this literal - three entries wearing five names, with no way
for a reader to tell which are live.

FAIL is worse than NOT_EVALUATED, which is worse than PASS: unsupervised is
not the same as clean, and neither is as bad as a breach.

MeasurementsResult.NOT_TAKEN is deliberately absent, so outcome ignores it.
It only ever means "this run took none", which for a test that defines none is
not a defect and would otherwise put NOT_TAKEN in the headline of every run in
this repo. NOT_EVALUATED is ranked because it means the opposite thing: bounds
that should have been supervising the run never evaluated a frame."""


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


@dataclass(frozen=True)
class Measurement:
    """One deliberate spot check: a value a test step read at a moment it
    chose, and the limits it had to satisfy.

    Frozen, unlike Violation beside it: a measurement is finished the moment
    it is taken, and MeasurementLog hands its list out as a shallow copy - so
    without this, a caller holding that copy could edit the run's own record
    through it.

    The counterpart to Violation. A Violation is something that happened to a
    run while nobody asked; a Measurement is something a run went and found
    out. Both land in the same verdict, and the run's result is the worse of
    what they say.

    RECORDED WHETHER IT PASSED OR FAILED, in full. A run has tens of these,
    not the thousands of frames a bound sees, so there is no size pressure to
    reduce a pass to a tally - and a pass's value is most of the point: it is
    what lets a stored run answer "what did the bus read that day", and what
    lets fifty runs show a DUT drifting toward a limit it has not crossed yet.

    The limits are copied in beside the value for the same reason: a record
    that says 47.9 without saying what it was judged against is only readable
    with the source at that commit to hand.

    `t` is wall-clock rather than a frame's timestamp. A measurement is not
    tied to any one telemetry frame - the value may be derived from several,
    or from none - and with a thread per stream there is no single seq to
    borrow. Wall clock still lines it up against the recorded CSVs.

    `step` is whatever @step last published as current_step, so a failure says
    where in the sequence it happened. None for a measurement taken outside
    any step."""

    name: str
    """This measurement's identity, unique within the run.

    Unique because it is how a measurement is found again - in this verdict,
    and across every run that took one by the same name. asimov's
    MeasurementLog refuses a second take of a name, so a value measured in a
    loop is the author's own aggregate taken once (e.g. the worst of them),
    not eight hundred rows nobody can compare across runs."""

    value: Any
    passed: bool
    upper: Optional[float] = None
    lower: Optional[float] = None
    expected: Optional[Any] = None
    units: str = ""
    t: float = 0.0
    step: Optional[str] = None

    statistic: Optional[str] = None
    """Which statistic of a sampled window `value` is - "mean", "min", "max"
    or "stdev" - or None for a measurement of one instantaneous reading.

    The field that keeps two very different measurements from reading alike: a
    bus voltage glanced at once and the minimum it reached over ten seconds are
    both a number with a limit, and only this says which was recorded."""

    seconds: Optional[float] = None
    """How long the window actually ran, not how long it was asked to.

    The loop overshoots its target by up to one sample interval, and a field
    that rounded that away would be reporting the request rather than the
    measurement."""

    samples: Optional[int] = None
    """How many usable readings the window's statistic was computed from.

    HOW MANY WERE TAKEN, NOT HOW MANY THE DEVICE PUBLISHED. The sampling loop
    reads through TelemetryClient.latest_frame(), which returns the newest
    frame and discards whatever queued behind it - so a loop that fell behind
    the device sampled a subset, and this is the size of that subset. Divide it
    by `seconds` and compare against what the device is known to publish at:
    well under means frames went unseen, and an extreme among them was never
    judged. Nothing in a stored verdict can recover them; the per-device CSV
    beside it has every frame the engine received."""

    skipped: Optional[int] = None
    """How many readings in the window could not be used - a dropped frame, a
    channel reporting no value.

    Recorded rather than ignored so a window with a hole in it is visibly
    different from a clean one. A measurement of one reading refuses an
    unusable value outright (see take_measurement); a window tolerates them,
    on the same reasoning that gave Bound its unevaluable_grace_s - one
    dropped frame in 126 is not a lost sensor - and this is what stops that
    tolerance from being silent."""

    repeats: Optional[int] = None
    """How many usable readings in the window were identical to the reading
    before them.

    A source that consumes a telemetry frame per call cannot repeat: two
    consecutive reads take two different frames, because TelemetryClient's
    latest_frame() blocks for one when none is queued. Every testbed accessor
    is such a source. A source that touches no frame - published state, a
    derived channel, a step's own local - returns instantly, and a window over
    one is thousands of copies of a single value: the mean comes out right and
    the standard deviation comes out a confident zero, with nothing to say so.

    This is what says so. Near zero on a telemetry window, and roughly equal to
    `samples` on a window that measured the same value over and over. It does
    not prevent the mistake, and does not try to: a genuinely steady signal
    repeats too, and only the author knows which they have."""

    window_min: Optional[Any] = None
    window_max: Optional[Any] = None
    """Any rather than float: a window of flags keeps its flags, so these are
    True/False there. min over booleans has to answer in booleans or an
    `expected=True` would be judging a boolean question by numeric coincidence
    (0.0 == False in Python) and the stored record would read as a number that
    happens to be zero."""

    window_mean: Optional[float] = None
    window_stdev: Optional[float] = None
    """The whole window beside the one statistic that was judged.

    Computed anyway, so keeping them costs nothing and answers the question
    that always follows a windowed measurement: the mean passed, but what was
    the worst of it? Sample standard deviation, not population - a window is a
    sample of an ongoing process, not the whole of one.

    All four are None on a measurement of one instantaneous reading. On a
    window of flags, `window_min`/`window_max` are True/False and the other two
    are None: an average of flags is a duty cycle, a different measurement from
    the one that was taken. `window_stdev` is also None when fewer than two
    usable readings make a spread unknown rather than zero."""


@dataclass
class Verdict:
    """One test run's authoritative record, keyed by test_id.

    `violations` is the *full* transition timeline, not a summary: every
    violate and every clear, in order. It lives here rather than in a
    separate store because a run's result is one record - splitting the
    timeline into a second file written by a different process on a
    different lifecycle is how a verdict ends up referencing violations
    nobody recorded. Ports to a database as one row plus one child table.

    `measurements` is the run's deliberate spot checks, in order and in full -
    the pointwise counterpart to the ambient bounds above, judged by
    `measurements_result` on its own axis for the same reason `lifecycle` and
    `bounds_result` are separate. See Measurement.

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
    used_mock: bool = False
    """Whether this run drove a simulated backend instead of the hardware.

    False on a verdict the engine synthesized, and on one written before this
    was recorded - neither knows, and neither should claim a run was mocked."""
    reason: str = ""
    any_fatal: bool = False
    violations: List[Violation] = field(default_factory=list)
    measurements_result: str = MeasurementsResult.NOT_TAKEN
    """Whether every measurement this run took satisfied its limits.

    Defaults to NOT_TAKEN so a verdict the engine synthesized, and one written
    before measurements existed, reports what is true of it: no measurement was
    taken. See MeasurementsResult."""
    measurements: List[Measurement] = field(default_factory=list)
    """Every measurement this run took, in the order it took them - passes
    included, in full. See Measurement."""
    metadata: Dict[str, Any] = field(default_factory=dict)
    completeness: Optional[Dict[str, Any]] = None

    @property
    def outcome(self) -> str:
        """A display-only join of the real fields, for logs and the operator
        dashboard. Deliberately derived and never stored as the source of
        truth - query lifecycle/bounds_result/measurements_result, not this.

        The second half is the WORSE of bounds_result and measurements_result,
        not a third segment: a headline that read COMPLETED/PASS for a run
        that failed a measurement would be a false pass, and one that spelled
        out every mechanism would grow a segment each time one is added. Which
        mechanism failed is a field away, in a record that always keeps them
        apart."""
        return f"{self.lifecycle}/{self._worst_result()}"

    def _worst_result(self) -> str:
        """The worse of bounds_result and measurements_result, ranked
        FAIL > NOT_EVALUATED > PASS. See _RESULT_SEVERITY for why
        MeasurementsResult.NOT_TAKEN is not ranked and so cannot win."""
        candidates = [
            result
            for result in (self.bounds_result, self.measurements_result)
            if result in _RESULT_SEVERITY
        ]
        if not candidates:
            # Neither field carries a ranked value - a run with no bounds
            # evaluated and no measurements taken. Report the bounds field,
            # which is the one that is always set.
            return self.bounds_result
        return max(candidates, key=lambda result: _RESULT_SEVERITY[result])

    def failed_measurements(self) -> List[Measurement]:
        """The measurements that failed their limits, in order - the short
        answer to "what was out of spec". Derived rather than stored, so it
        cannot disagree with the list it comes from."""
        return [m for m in self.measurements if not m.passed]

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
            used_mock=data.get("used_mock", False),
            bounds_result=data["bounds_result"],
            started_at=data["started_at"],
            ended_at=data["ended_at"],
            reason=data.get("reason", ""),
            any_fatal=data.get("any_fatal", False),
            violations=[Violation(**v) for v in data.get("violations", [])],
            # Absent on every verdict written before measurements existed, and
            # on any the engine synthesized - both of which took none.
            measurements_result=data.get("measurements_result", MeasurementsResult.NOT_TAKEN),
            measurements=[Measurement(**m) for m in data.get("measurements", [])],
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
