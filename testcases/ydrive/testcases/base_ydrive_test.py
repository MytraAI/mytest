"""Base test case for ydrive: starts the ODrive testbed, seeds this run's
state channels, and constructs (but does not start) a LiveRulebookRunner
against RULEBOOKS. No test sequence logic of its own - unlike example_dut,
there's no separate DUT abstraction here, since the ODrive IS the test's
entire hardware interface, so DEVICES is just the testbed's.

pre_test_setup() deliberately does not call self.runner.start() - a
concrete subclass decides when live evaluation begins by calling
self.runner.start(self.testbed.telemetry) itself, wherever in its own
main_execution() that should happen (see EnduranceCycleTest).
post_test_teardown() always stops the runner regardless of whether
start() was ever called.

Runnable on its own, not abstract: main_execution() here just logs and
returns, and never calls runner.start(), so running this base case
directly does no live evaluation.

use_mock is plumbed through to YdriveTestbed for developing/testing
without hardware attached - defaults to False (real hardware).
"""
from __future__ import annotations

import logging
from typing import List, Optional

from testbeds.ydrive_testbed.ydrive_testbed import YdriveTestbed
from testcases.asimov.live_rulebook_runner import LiveRulebookRunner
from testcases.asimov.rulebook import Rulebook
from testcases.base import TestCase

from ..channels import DEFAULT_STATE
from ..rulebooks.ydrive_rulebook import YDRIVE_RULEBOOK

logger = logging.getLogger(__name__)


class BaseYdriveTest(TestCase):
    """Base test case for ydrive: starts the ODrive testbed, tags its telemetry, constructs (but doesn't start) Rulebook evaluation - no test sequence logic."""

    TEST_NAME = "base_ydrive_test"
    RULEBOOKS: List[Rulebook] = [YDRIVE_RULEBOOK]
    DEVICES = YdriveTestbed.DEVICES
    """Just the testbed's devices: ydrive is purely mechanical, so there is no
    DUT façade with a device of its own to union in (see this module's
    docstring)."""

    def __init__(self, test_id: Optional[str] = None, use_mock: bool = False, require_engine: bool = True):
        super().__init__(test_id, require_engine=require_engine)
        self._use_mock = use_mock
        self.testbed: Optional[YdriveTestbed] = None

    def pre_test_setup(self) -> None:
        self.testbed = YdriveTestbed(use_mock=self._use_mock)
        self.testbed.start()

        self._seed_channels()

        # Constructed here so it's ready the moment MainExecution starts, but
        # NOT started - see this module's docstring. A concrete subclass's
        # main_execution() calls self.runner.start(self.testbed.telemetry)
        # itself, whenever it decides live evaluation should actually begin.
        self.runner = LiveRulebookRunner(
            test_id=self.test_id,
            rulebooks=self.RULEBOOKS,
            publisher=self._publisher,
        )

    def _seed_channels(self) -> None:
        """Publish a default for every state channel this test can
        produce, so each one exists in the stream from frame 1 instead
        of appearing incrementally as steps happen to compute things
        (see ../channels.py).

        Bound-status channels are derived from RULEBOOKS rather than
        hand-listed, since the Rulebook is already the single source
        of truth for bound names."""
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
            # Stop the rulebook runner's background thread before the testbed
            # - it relies on telemetry still flowing to notice the stop signal
            # promptly (see LiveRulebookRunner._run()). Safe to call even if a
            # subclass's main_execution() never called runner.start().
            self.teardown_step("stop rulebook runner", self.runner.stop)

        if self.testbed is not None:
            self.teardown_step("stop testbed", self.testbed.stop)
