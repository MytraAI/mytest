"""Base test case for zdrive: starts the testbed, seeds this run's state channels,
and constructs (but does not start) a LiveRulebookRunner against RULEBOOKS.

pre_test_setup() deliberately does not call self.runner.start() - a concrete
subclass decides when evaluation begins. It energizes nothing and leaves the
brake engaged: releasing here would leave the load held by nothing.

ALL THREE TELEMETRY STREAMS ARE NEEDED, and getting it wrong is invisible: a
bound whose channel is absent from a frame returns no result, so fewer streams
silently evaluates part of the rulebook and passes the rest."""
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
    """Base test case for zdrive: starts the testbed, tags its telemetry, and constructs (but does not start) Rulebook evaluation."""

    DUT = "zdrive"
    TEST_NAME = BASE_ZDRIVE_TEST_NAME
    RUN_DETAIL_FIELDS = run_detail_fields(DUT)
    """What the operator is asked for before a run, if a test chooses to ask."""

    RULEBOOKS: List[Rulebook] = [ZDRIVE_RULEBOOK]

    DEVICES = ZdriveTestbed.DEVICES
    """Just the testbed's devices: zdrive is purely mechanical."""

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
        """Publish a default for every state channel this test can produce, so each
        exists in the stream from frame 1.

        Seeding is what keeps them in the recorded file at all: the engine fixes a
        wide file's header from its first frames and drops a channel that appears
        later - see ../channels.py. MUST RUN BEFORE THE DRIVERS DO; see
        pre_test_setup()."""
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
