"""Rulebook/Bound framework: the shape test authors use to write
evaluation rules against telemetry channels.

Bound definitions are the single source of truth for a test's channel
checks. They're used two ways:

- Live, inside MainExecution, via RulebookEvaluator below. This is the
  only place a bound violation can actually affect the running test
  (abort on a `fatal` bound) or fire a hardware event (`event_name`).
  See base.py's "no feedback loop" note.
- Offline, against stored telemetry, via telemetry_engine/replay.py (or
  evaluation.py for a stream spanning several runs). Used to ask whether
  different bounds would have caught something, and to check a stored record
  against the verdict written from it. It can never influence any test.

A Bound checks one channel against up to three constraints:

- `upper`: violated if the channel is higher than this.
- `lower`: violated if the channel is lower than this.
- `expected`: violated if the channel isn't exactly equal to this -
  meant for discrete/state channels, not noisy continuous ones.

Any combination may be set; all that are set are ANDed together (the
bound is satisfied only if every constraint that's set holds). A Bound
may also be gated on another channel's value - a hardware channel, or a flag
the test published via set_state(). It's only evaluated on frames where the
gate condition holds; frames where the gate holds a different value, or where
the bound's own channel is absent, simply produce no result for that frame. A
gate channel that doesn't exist *at all* raises rather than silently skipping
forever - see Bound.evaluate().

Live evaluation sees both kinds of gate channel because LiveRulebookRunner
evaluates against the union of the device's frame and the test's published
state, read in-process from the state publisher. State never has to travel
over the wire to be gated on.

TWO SHAPES OF BOUND CAN NEVER FIRE, and neither is refused. A Bound with
none of `upper`/`lower`/`expected` set is satisfied by every value, and so is
one whose `upper` or `lower` is a NaN, since every comparison against a NaN is
False. Either sits in a Rulebook reporting PASS for the whole run while
supervising nothing - the same false-supervision failure UnevaluableBoundError
exists to prevent, arrived at from the rulebook's side rather than the data's.
Construction is deliberately left permissive; the check is a reviewer reading
the rulebook. All 27 bounds registered in this repo today are clear of both.
Note asimov.measurement's take_measurement() does refuse the no-limits case,
because its limits are written inline at a call site rather than declared in a
file somebody reviews as a whole.

A Bound may also require `persistence_s` seconds of continuous
violation before it actually trips (e.g. a current bound that only
fails after being exceeded for 200ms, to ignore a brief noise spike).
This debounce is asymmetric and non-cumulative, by design:

- Clearing is immediate - the moment the raw condition is satisfied
  again, regardless of persistence_s.
- Any gap (the raw condition becoming satisfied, or the bound not
  applying at all for a frame) resets the "how long has this been
  continuously violated" clock to zero. It does not pause and resume.

See RulebookEvaluator.evaluate() for where this is tracked -
Bound.evaluate() itself stays a pure, stateless, instantaneous check.
"""
from __future__ import annotations

import logging

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .limits import Uncomparable, compare


logger = logging.getLogger(__name__)


class UnevaluableBoundError(Exception):
    """Raised when a Bound cannot be evaluated at all, because its channel
    carries a value it can't be compared against (a None, or a type that
    won't compare with the configured limit).

    Loud rather than skipped, deliberately: a bound that silently fails to
    evaluate leaves the hardware unsupervised while the test looks healthy.
    LiveRulebookRunner treats it exactly as it treats a silent telemetry
    stream - fatal to the test.

    Normally unreachable, since a backend verifies its declared channels
    exist at connect(). This is the backstop for a channel that exists but
    reports an uncomparable value."""

    def __init__(self, bound_label: str, channel: str, value: Any, reason: str):
        super().__init__(f"bound {bound_label} cannot be evaluated: channel {channel}={value!r} - {reason}")
        self.bound_label = bound_label
        self.channel = channel
        self.value = value


DEFAULT_UNEVALUABLE_GRACE_S = 1.0
"""How long a bound may be unevaluable before it stops the run.

A channel that reports no value cannot be checked, and a bound that cannot be
checked is not a bound that passed - so it stops the run. But "no value" arrives
one sample at a time: a thermocouple dropped one frame in 6852 of a twelve-minute
run and ended it, having supervised the whole run perfectly either side. This is
the window that separates a dropout from a loss, in the data's own time rather
than in samples, since sample rates differ per device.

Short by default, because a bound is supervision and this window is time spent
without it: a second covers the dropout of any device here (the slowest samples at
9 Hz) while leaving a genuinely absent channel caught almost as fast as before. A
sensor known to drop for longer says so on its own bounds rather than by raising
this for everything - see ydrive_rulebook's thermal bounds."""


@dataclass(frozen=True)
class Bound:
    """One channel check: violated if `channel`'s value is above `upper`, below `lower`, or not `expected`.

    `fatal` and `event_name` only matter to a live evaluator (e.g.
    MainExecution's). Post-hoc evaluation ignores them, since it has no
    live test to abort or hardware event to fire."""

    channel: str
    upper: Optional[float] = None
    lower: Optional[float] = None
    expected: Optional[Any] = None
    name: Optional[str] = None
    gate_channel: Optional[str] = None
    gate_value: Optional[Any] = None
    fatal: bool = False
    event_name: Optional[str] = None
    persistence_s: Optional[float] = None

    unevaluable_grace_s: float = DEFAULT_UNEVALUABLE_GRACE_S
    """How long this bound may report no value before that stops the run.

    Separate from persistence_s, which debounces a *violation*: this debounces
    the absence of anything to judge. A sensor that drops a sample is not a
    sensor that is gone."""

    @property
    def label(self) -> str:
        """Stable identifier for this bound, for reporting and for
        tracking violation-state transitions - defaults to a
        human-readable description if no explicit name was given."""
        if self.name:
            return self.name
        constraints = []
        if self.lower is not None:
            constraints.append(f">={self.lower}")
        if self.upper is not None:
            constraints.append(f"<={self.upper}")
        if self.expected is not None:
            constraints.append(f"=={self.expected}")
        return f"{self.channel} {' and '.join(constraints)}"

    def evaluate(self, channels: Dict[str, Any]) -> Optional[bool]:
        """Return True if violated, False if satisfied, or None if this
        bound doesn't apply to this frame (gate held a different value, or its
        channel isn't present).

        Raises UnevaluableBoundError if the channel is present but carries a
        value that can't be compared against this bound's limits, or if this
        bound's `gate_channel` doesn't exist at all - see that exception for
        why this must be loud rather than skipped. Note an `expected`-only
        bound needs no ordering, so it accepts any type and never raises.

        The gate distinction matters: a gate channel that *exists* and holds
        some other value means the bound legitimately doesn't apply right now,
        which is the whole point of gating. A gate channel that exists nowhere
        means the bound can never apply, so it would sit there being silently
        skipped on every frame while the run reported a clean pass - the same
        false-supervision failure as a declared-but-absent channel."""
        if self.gate_channel is not None:
            if self.gate_channel not in channels:
                raise UnevaluableBoundError(
                    self.label,
                    self.gate_channel,
                    None,
                    "its gate channel is not present in the frame or the test's published state, "
                    "so this bound could never be evaluated",
                )
            if channels[self.gate_channel] != self.gate_value:
                return None
        if self.channel not in channels:
            return None

        actual = channels[self.channel]
        try:
            return compare(actual, upper=self.upper, lower=self.lower, expected=self.expected)
        except Uncomparable as exc:
            # The comparison itself is shared with measurement.py (see
            # asimov/limits.py); what an unjudgeable value *means* is not. Here
            # it is a bound left unsupervised, which a runner tolerates for
            # unevaluable_grace_s before ending the run.
            raise UnevaluableBoundError(self.label, self.channel, actual, exc.reason) from exc


@dataclass(frozen=True)
class Rulebook:
    """A named collection of Bounds, applying to one or more test types
    (TestCase.TEST_NAME values) via test_names.

    Multiple Rulebooks can be registered for the same test_name (e.g. a
    "safety" rulebook and a "performance" rulebook, checked
    independently) - there is no single Rulebook-per-test limit. The
    reverse also holds: the same Rulebook can list more than one
    test_name, e.g. a shared safety rulebook several concrete test
    cases all start their runner against.
    test_names only matters when looking up rulebooks by test name during
    offline replay; live evaluation runs whatever Rulebooks a test case
    explicitly passes it, regardless of test_names."""

    name: str
    test_names: List[str]
    bounds: List[Bound]


@dataclass(frozen=True)
class BoundTransition:
    """One bound's pass/fail transition, produced by exactly one
    RulebookEvaluator instance. It's scoped to whatever single run that
    evaluator is tracking (a live test, or one post-hoc test_id), so
    unlike protocol/verdict.py's Violation - which the live runner builds
    from this, stamping on the frame's own seq/t - this carries no
    test_id/test_name/seq/t of its own."""

    rulebook_name: str
    bound_label: str
    channel: str
    value: Any
    fatal: bool
    event_name: Optional[str]
    violated: bool


class RulebookEvaluator:
    """Tracks per-bound violated/cleared state across repeated
    evaluate() calls against the channel snapshots of one run.

    ONE EVALUATOR COVERS EVERY STREAM a live runner watches, so a bound is
    reached by frames that do not carry its channel as well as by frames that
    do - see evaluate().

    Shared by both live in-test evaluation (MainExecution creates one
    instance for itself) and post-hoc evaluation (the telemetry engine
    creates one instance per test_id it sees). The tracking logic is
    the same either way - only who calls it, how many instances exist,
    and what they do with the resulting transitions differs."""

    def __init__(self) -> None:
        self._rulebooks: List[Rulebook] = []
        self._prior_violated: Dict[Tuple[str, str], bool] = {}
        self._pending_since: Dict[Tuple[str, str], float] = {}
        self._unevaluable_since: Dict[Tuple[str, str], float] = {}
        """When each bound last started being unevaluable, so a momentary absence
        is tolerated and a sustained one is not - see Bound.unevaluable_grace_s."""

    def register(self, rulebook: Rulebook) -> None:
        self._rulebooks.append(rulebook)

    def evaluate(self, channels: Dict[str, Any], t: float) -> List[BoundTransition]:
        """Return a BoundTransition for every bound whose violated/cleared
        state just changed. Nothing is returned for bounds that don't
        apply this frame, are still pending persistence_s, or whose
        state is unchanged. `t` is this frame's timestamp, used to time
        out persistence_s."""
        transitions = []
        for rulebook in self._rulebooks:
            for bound in rulebook.bounds:
                key = (rulebook.name, bound.label)
                try:
                    raw = bound.evaluate(channels)
                except UnevaluableBoundError:
                    # Tolerated briefly, fatal if it lasts. A thermocouple that
                    # drops one sample in a twelve-minute run is not a lost sensor,
                    # and treating it as one ends the run; a channel that stays
                    # absent is a lost sensor, and carrying on would be
                    # supervising nothing. Only this bound is skipped - the rest
                    # of the frame is still evaluated, so one flaky channel does
                    # not blind the others.
                    started = self._unevaluable_since.setdefault(key, t)
                    if t - started <= bound.unevaluable_grace_s:
                        if started == t:
                            logger.warning(
                                "bound %s cannot be evaluated (%s) - tolerating for up to %.1fs",
                                bound.label, bound.channel, bound.unevaluable_grace_s,
                            )
                        continue
                    raise
                if raw is None:
                    # Didn't apply to this frame - the channel is absent, or a gate
                    # held another value. NOT evidence that an unevaluable bound has
                    # recovered, so the grace timer is left alone. One evaluator is
                    # shared by every stream a runner watches, and a stream that does
                    # not carry the channel reaches here on every one of its frames,
                    # so clearing the timer here would reset one stream's grace at the
                    # frame rate of another and it could never expire.
                    self._pending_since.pop(key, None)
                    continue

                if self._unevaluable_since.pop(key, None) is not None:
                    logger.info("bound %s is evaluable again (%s)", bound.label, bound.channel)

                was_violated = self._prior_violated.get(key, False)

                if not raw:
                    # satisfied - clears immediately, no persistence delay on the way out
                    self._pending_since.pop(key, None)
                    violated = False
                elif was_violated or not bound.persistence_s:
                    # already confirmed violated, or no persistence required
                    violated = True
                    self._pending_since.pop(key, None)
                else:
                    # violated, not yet confirmed, persistence required - debounce.
                    # Any gap (raw False, or not applicable) resets this via the
                    # pop()s above, so setdefault only starts the clock once per
                    # continuous violation streak.
                    started = self._pending_since.setdefault(key, t)
                    if t - started < bound.persistence_s:
                        continue  # still pending, not yet long enough
                    violated = True
                    del self._pending_since[key]

                if violated == was_violated:
                    continue

                self._prior_violated[key] = violated
                transitions.append(
                    BoundTransition(
                        rulebook_name=rulebook.name,
                        bound_label=bound.label,
                        channel=bound.channel,
                        value=channels[bound.channel],
                        fatal=bound.fatal,
                        event_name=bound.event_name,
                        violated=violated,
                    )
                )
        return transitions

    def any_violated(self) -> bool:
        """True if any registered bound is currently violated."""
        return any(self._prior_violated.values())
