"""Stopwatch: a small elapsed-time helper so test sequencing logic
reads in human terms (seconds since start), instead of doing
arithmetic on raw timestamps directly (e.g. telemetry frame epoch
floats).

Deliberately just wall-clock time (time.time()), independent of
telemetry frame timestamps. A test's own sequencing - how long has
this test been running, how long since the last position change - is
a different concern from a Rulebook bound's persistence_s, which is
correctly tied to the data's own timestamp (frame.t) rather than
wall-clock time. See testcases/asimov/rulebook.py.
"""
from __future__ import annotations

import time
from typing import Iterator, Optional


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
