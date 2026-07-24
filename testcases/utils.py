"""Small shared helpers for the testcase execution process - not
per-DUT content, not the Rulebook framework, just plain utilities more
than one part of testcases/ needs. Named utils.py rather than
stopwatch.py now that it holds more than Stopwatch.

Stopwatch: a small elapsed-time helper so test sequencing logic reads
in human terms (seconds since start), instead of doing arithmetic on
raw timestamps directly (e.g. telemetry frame epoch floats).

Deliberately just wall-clock time (time.time()), independent of
telemetry frame timestamps. A test's own sequencing - how long has
this test been running, how long since the last position change - is
a different concern from a Rulebook bound's persistence_s, which is
correctly tied to the data's own timestamp (frame.t) rather than
wall-clock time. See testcases/asimov/rulebook.py.

spawn_operator_dashboard: starts tools/operator_dashboard.py's
lightweight status page for one test - see TestCase.run() (base.py)
for where this is called (once, at the very start, before
PreTestSetup, so the page is up even if setup itself fails) and that
module's own docstring for the full design rationale. The import of
tools.operator_dashboard is deferred into the function itself, not
module level - testcases/ is the core framework, imported by
everything; tools/ is operator-facing tooling built on top of it, and
keeping that dependency narrow (only the one function that actually
needs it) avoids every Stopwatch caller transitively depending on it.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:
    from tools.operator_dashboard import OperatorDashboard

logger = logging.getLogger(__name__)


class Stopwatch:
    """Tracks wall-clock elapsed time since construction (or the last start()).

    duration_s is optional - pass it to also get .expired, so a caller
    doesn't need to separately remember and compare against its own
    duration constant."""

    POLL_INTERVAL_S = 0.01
    """Pacing tick for wait() - how often a loop built around .expired re-checks, shared by any caller instead of each one hand-rolling its own sleep/poll-interval constant."""

    def __init__(self, duration_s: Optional[float] = None) -> None:
        self._start_time = time.time()
        self._duration_s = duration_s

    def start(self) -> None:
        """Restart the stopwatch, resetting elapsed time back to zero."""
        self._start_time = time.time()

    def elapsed_s(self) -> float:
        """Seconds elapsed since construction or the last start()."""
        return time.time() - self._start_time

    @property
    def expired(self) -> bool:
        """True once elapsed_s() has reached duration_s. Requires duration_s."""
        if self._duration_s is None:
            raise ValueError("Stopwatch.expired requires duration_s to be set at construction")
        return self.elapsed_s() >= self._duration_s

    def wait(self) -> None:
        """Sleep for POLL_INTERVAL_S - the pacing tick for a loop that
        polls .expired, so callers don't each define their own sleep
        interval just to pace a wait loop."""
        time.sleep(self.POLL_INTERVAL_S)

    def __iter__(self) -> Iterator[float]:
        """Iterate once per POLL_INTERVAL_S until expired, pacing
        itself via wait() - so `for _ in stopwatch:` is a paced wait
        loop with nothing for the caller to remember: no .expired
        check, no .wait() call. Yields elapsed_s() each tick, for
        callers that want it. Requires duration_s, same as .expired."""
        while not self.expired:
            yield self.elapsed_s()
            self.wait()


def spawn_operator_dashboard(test_id: str, test_name: str) -> Optional["OperatorDashboard"]:
    """Starts the lightweight operator status page for this test and
    opens it in a browser tab. Returns None (logged, not raised) on
    failure to start - e.g. its default port still in use - since
    this is an observability convenience, not something a test should
    fail over. See tools/operator_dashboard.py for what it actually
    shows and TestCase.run() for how status/error get pushed to it.

    Calls reclaim_stale_dashboard() first, so a previous test's operator
    closing the browser tab instead of stopping it properly doesn't cost
    *this* test its own dashboard - see that function's docstring."""
    from tools.operator_dashboard import OperatorDashboard, reclaim_stale_dashboard

    try:
        reclaim_stale_dashboard()
        dashboard = OperatorDashboard(test_id=test_id, test_name=test_name)
        dashboard.start()
    except OSError as exc:
        logger.warning("test %s: couldn't start operator status page: %s", test_id, exc)
        return None
    return dashboard
