"""Base test case for zdrive: starts the zdrive testbed, seeds this run's state
channels, and constructs (but does not start) a LiveRulebookRunner against
RULEBOOKS. No test sequence logic of its own - there is no separate DUT
abstraction here, since the ODrive is the test's entire hardware interface, so
DEVICES is just the testbed's.

pre_test_setup() deliberately does not call self.runner.start() - a concrete
subclass decides when live evaluation begins by calling self.runner.start()
itself, wherever in its own main_execution() that should happen.
post_test_teardown() always stops the runner regardless of whether start() was
ever called.

ALL THREE TELEMETRY STREAMS ARE NEEDED, and getting that wrong is invisible:
zdrive_rulebook's bus bounds are on N6974A channels, its motor bounds on the
ODrive's and its thermal bounds on the TC DAQ's, no device publishes another's,
and a bound whose channel is absent from a frame returns no result - so a runner
started against fewer streams evaluates part of the rulebook and reports a clean
pass for the rest. Every concrete test must pass all three, as ManualTest does.

Runnable on its own, not abstract: main_execution() here just logs and returns,
and never calls runner.start(), so running this base case directly does no live
evaluation.

pre_test_setup() energizes nothing, and leaves the brake engaged. The stand comes
up with the motor bus and every rail off, so the magnet-applied brake is already
holding and the axis is still IDLE. Releasing here would leave the load held by
nothing until a subclass arms the axis: the handover goes controller-first, which
is why releasing is a concrete test's job.

use_mock is plumbed through to ZdriveTestbed for developing without an ODrive
attached. It substitutes the ODrive only - neither supply's driver has a mock
backend, so a reachable CPX400DP and N6974A are needed either way.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from testbeds.zdrive_testbed.zdrive_testbed import ZdriveTestbed
from asimov.live_rulebook_runner import LiveRulebookRunner
from asimov.rulebook import Rulebook
from testcases.base import TestCase
from testcases.teststeps.operator import run_detail_fields

from ..channels import DEFAULT_STATE
from ..rulebooks.zdrive_rulebook import BASE_ZDRIVE_TEST_NAME, ZDRIVE_RULEBOOK

logger = logging.getLogger(__name__)


class BaseZdriveTest(TestCase):
    """Base test case for zdrive: starts the testbed, tags its telemetry, constructs (but doesn't start) Rulebook evaluation - no test sequence logic."""

    DUT = "zdrive"
    TEST_NAME = BASE_ZDRIVE_TEST_NAME
    RUN_DETAIL_FIELDS = run_detail_fields(DUT)
    """What the operator is asked for before a run, if a test chooses to ask.

    Held here rather than on the tests that use it, so every test on this DUT
    can prompt and each one decides whether to - a manual test that exists to
    poke at hardware by hand has nothing to attribute. The serial dropdown is
    whatever the catalogue says this DUT can run."""

    RULEBOOKS: List[Rulebook] = [ZDRIVE_RULEBOOK]

    DEVICES = ZdriveTestbed.DEVICES
    """Just the testbed's devices: zdrive is purely mechanical, so there is no
    DUT façade with a device of its own to union in."""

    def __init__(self, test_id: Optional[str] = None, use_mock: bool = False, require_engine: bool = True):
        super().__init__(test_id, require_engine=require_engine)
        self.used_mock = use_mock
        self.testbed: Optional[ZdriveTestbed] = None

    def pre_test_setup(self) -> None:
        # SEEDED BEFORE THE DRIVERS EXIST, and the order is load-bearing. The
        # engine fixes each wide file's header from that device's first
        # HEADER_SAMPLE_FRAMES frames, so the window is a frame count and closes
        # sooner on a faster device - about 2 s on the CPX400DP at ~24 Hz, which
        # is less than ZdriveTestbed.start() takes to bind sockets and connect
        # four backends. Seeding after start() therefore lands inside the window
        # for the slow devices and outside it for the fast one, and that device's
        # file silently loses every state channel. Nothing here needs the testbed:
        # set_state only needs the publisher.
        self._seed_channels()

        self.testbed = ZdriveTestbed(
            use_mock_odrive=self.used_mock, output_dir=self._output_dir, test_id=self.test_id
        )
        self.testbed.start()

        # Constructed here so it's ready the moment MainExecution starts, but
        # NOT started - see this module's docstring.
        self.runner = LiveRulebookRunner(
            test_id=self.test_id,
            rulebooks=self.RULEBOOKS,
            publisher=self._publisher,
        )

    def _seed_channels(self) -> None:
        """Publish a default for every state channel this test can produce, so
        each one exists in the stream from frame 1 rather than appearing when a
        step first computes it.

        Seeding is what keeps them in the recorded file at all: the engine fixes
        a wide file's header from its first frames and drops a channel that
        appears later, so a value a step computes partway through a sequence
        would never be written. See ../channels.py.

        MUST RUN BEFORE THE DRIVERS DO. The header window is counted in frames
        per device, so the fastest publisher closes it first - see
        pre_test_setup(), which calls this before starting the testbed for that
        reason.

        Bound-status channels are derived from RULEBOOKS rather than hand-listed,
        since the Rulebook is already the single source of truth for bound
        names."""
        for name, default in DEFAULT_STATE.items():
            self.set_state(name, default)

        self.set_state("test_status", "PASS")
        for rulebook in self.RULEBOOKS:
            for bound in rulebook.bounds:
                self.set_state(f"{bound.label}_status", "PASS")

    def main_execution(self) -> None:
        logger.info("test %s: base case has no test logic - completing immediately", self.test_id)

    def post_test_teardown(self) -> None:
        if self.runner is not None:
            # Stop the rulebook runner's background thread before the testbed -
            # it relies on telemetry still flowing to notice the stop signal
            # promptly. Safe to call even if main_execution() never started it.
            self.teardown_step("stop rulebook runner", self.runner.stop)

        if self.testbed is not None:
            self.teardown_step("stop testbed", self.testbed.stop)
