"""Documents every state channel xdeploy's tests publish.

DEFAULT_STATE seeds them (see BaseXdeployTest.pre_test_setup) so they exist in
the stream from frame 1 instead of appearing when a step first computes them.

THIS IS NOT COSMETIC. The telemetry engine fixes a wide file's header from the
first HEADER_SAMPLE_FRAMES it sees and drops any channel that appears later. The
drivers are already publishing before a run's first step runs, so a channel first
written partway through a sequence would be logged once and dropped, and the
measurement the run exists to take would be missing from the recorded file while
the run reported a clean pass.

THE RULE CUTS BOTH WAYS: add a channel here only once something writes it. One
seeded and never written records its default on every row of every run, which
reads like a measurement that was taken and never moved.

Bound-status channels ({bound.label}_status, plus test_status) are not listed
here - they are derived from RULEBOOKS, which is already the single source of
truth for bound names.
"""
from __future__ import annotations

from typing import Any, Dict

DEFAULT_STATE: Dict[str, Any] = {
    "operator_prompt": None,
    # What the run is waiting for a person to do, or None when it is not waiting.
    # Carried on every frame of the wait, so a stored run shows what it was
    # waiting for rather than looking like a hang - which on this stand is the
    # difference between "nobody had switched the bench supply on yet" and "the
    # test hung".
    "dut_serial_number": None,
    "er_ticket": None,
    "load_lb": None,
    # Answered by the operator before anything is energized, and then carried on
    # every recorded row - so a stored run says which DUT it was, under what load,
    # against which ticket, without anyone keeping a separate note. Published
    # under a channel name rather than a label, so rewording the prompt cannot
    # rename the channel stored runs are keyed by.
    #
    # Seeded even though ManualTest never asks: the fields are declared on
    # BaseXdeployTest for the tests that will, and a channel the engine has
    # already dropped cannot be added back by the first test that publishes it.
    "position_target": None,
    # Where the last move was aimed, written by move_to().
    "cycle_count": 0,
    "cycle_time_s": 0.0,
    # Completed cycles, and how long the last one's two legs took. cycle_time_s
    # carries no bound yet on purpose - it is the evidence a cycle_time_bound
    # would be set from, and a cycle that slows is how a jam, rising friction or
    # a derating board first shows. Excludes the dwell and any thermal wait, so
    # a cooled-down cycle and a hot one are still comparable.
    "total_travel_turns": 0.0,
    # How far the drive has travelled since cycling began, derived from the
    # ODrive's own turns_traveled. In turns rather than metres: this stand has no
    # measured metres-per-turn, and inventing one would put a fabricated geometry
    # into every stored run. Seeded like any other channel - a derived channel
    # appears only once its derivation has something to report, which is later
    # than the engine fixes the header.
    "thermal_waits": 0,
    # How many 60 s holds a run has taken waiting to cool. Published so a stand
    # that cannot cool reads as a collapsed cycle rate rather than a silent one.
}
