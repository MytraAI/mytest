"""The one comparison: is a value inside the limits it was given?

Both of asimov's evaluation mechanisms ask this same question and must
answer it identically, so they ask it here:

- a `Bound` (rulebook.py), continuously, against every telemetry frame a
  LiveRulebookRunner sees;
- a measurement (measurement.py), once, at a moment a test step chose.

Up to three limits - `upper`, `lower`, `expected` - any combination of
which may be set, ANDed together: the value satisfies them only if every
limit that is set holds. `expected` is exact equality, meant for
discrete/state values rather than noisy continuous ones.

WHAT THIS DELIBERATELY DOES NOT DO is decide what an unjudgeable value
means. It raises `Uncomparable` carrying only the reason, and each caller
turns that into its own failure: a Bound into `UnevaluableBoundError`,
which a runner tolerates for a grace window because one dropped sample in
a stream of thousands is not a lost sensor; a measurement into
`UnmeasurableError`, which stops the run immediately, because a step that
hands over an unjudgeable value has no second sample coming.

Extracted rather than duplicated because the rule has four subtleties a
second copy would lose: a `None` cannot be compared and must be loud
rather than skipped, a non-numeric value against a numeric limit is the
same failure, a NaN is too - every comparison against one is False, so an
unchecked NaN satisfies every limit it is given and reports a clean pass
while nothing is supervised - and `bool` is deliberately exempt from the
non-numeric rule -
it is an `int` subclass, compares fine, and a bool against a numeric limit
is an authoring mistake to be found by reading the rulebook, not a frame
this codebase should refuse to evaluate.
"""
from __future__ import annotations

import math
from typing import Any, Optional


class Uncomparable(Exception):
    """Raised by compare() when a value cannot be judged against its limits
    at all - it carries no value, a type that will not order against a numeric
    limit, or a value that is not finite.

    Deliberately reason-only and caller-agnostic: it says what is wrong with
    the value, and nothing about what should happen next. Callers catch it
    and raise their own error, which is where the difference between a
    tolerated dropout and a stopped run belongs."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def compare(
    actual: Any,
    upper: Optional[float] = None,
    lower: Optional[float] = None,
    expected: Optional[Any] = None,
) -> bool:
    """True if `actual` is OUTSIDE the limits given, False if it satisfies
    every limit that is set.

    Phrased as "is it out?" rather than "is it in?" because that is the
    question a Bound asks (`violated`); a measurement inverts it once, at
    the point it records `passed`.

    Raises Uncomparable if `actual` cannot be judged against the numeric
    limits set. An `expected`-only comparison needs no ordering, so it
    accepts any type and never raises - including None, which is simply not
    equal to whatever was expected.
    """
    if upper is not None or lower is not None:
        if actual is None:
            raise Uncomparable("no value was reported, so its numeric limits can't be checked")
        if not isinstance(actual, (int, float)):
            # bool is an int subclass and compares fine, so it passes here
            # deliberately - see this module's docstring.
            raise Uncomparable(f"a {type(actual).__name__} can't be compared against a numeric limit")
        if isinstance(actual, float) and not math.isfinite(actual):
            # EVERY COMPARISON AGAINST A NaN IS FALSE, so without this a NaN
            # satisfies every limit it is given and reports a clean pass while
            # nothing is being supervised. Not hypothetical on this hardware:
            # zdrive's pos_estimate reads NaN with every other channel looking
            # healthy - no active errors, the encoder status NOMINAL, velocity
            # tracking normally - which is why the testbed has
            # _require_finite_position at all. An infinity is refused with it:
            # it compares, but a limit is a question about a real quantity.
            #
            # Guarded on float rather than asked of every number: math.isfinite()
            # converts its argument to a float first, so on a large enough int it
            # raises OverflowError - which is neither Uncomparable nor
            # UnevaluableBoundError, so it would escape the live runner's thread
            # uncaught and leave a test running with no monitoring and
            # fatal_violation unset. An int cannot be non-finite anyway, and a
            # bool is not a float, so neither needs asking.
            raise Uncomparable(
                f"{actual} is not a finite number, so its numeric limits can't be checked"
            )
    if upper is not None and actual > upper:
        return True
    if lower is not None and actual < lower:
        return True
    if expected is not None and actual != expected:
        return True
    return False
