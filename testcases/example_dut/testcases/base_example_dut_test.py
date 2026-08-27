"""Base test case for example_dut.

Handles the shared setup any example_dut test needs:
- starts the physical testbed (DAQ + power supply instruments), which
  also gives us a ready-to-use power supply command client
- powers the DUT via the testbed's power supply
- starts the DUT itself
- declares all three devices via DEVICES, so the engine records every one
  of them into this run's directory
- wires up live Rulebook evaluation against the DUT's own telemetry
  (position/velocity/current)

It has no test sequence logic of its own.

Concrete subclasses override TEST_NAME, RULEBOOKS (the Rulebook(s)
matching their own TEST_NAME - empty here, since this base case
doesn't evaluate anything), and main_execution() with their actual
test sequence.

This class is deliberately runnable on its own, not abstract:
main_execution() here just logs and returns immediately. A base case
with no test logic proves the setup/teardown/telemetry-tagging/
evaluation-wiring plumbing works before any real test sequence is
layered on top of it.

Note what this test claims versus what it evaluates. DEVICES covers all
three driver processes - the DAQ and power supply from the testbed, the DUT
from its façade - so the telemetry engine records all three into this run's
directory. Live rule evaluation is a separate choice: the runner is handed
the DUT's telemetry client, because the DUT's own position/velocity/current
are what these tests judge, not the DAQ's generic channels. Recording
breadth and evaluation focus are independent, and this test uses different
sets for each on purpose.

Also seeds a default for every state channel a subclass's steps might
publish (see ../channels.py and _seed_channels() below), so the full
channel set exists in the stream from frame 1 instead of appearing
incrementally as steps happen to compute things.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from testbeds.example_testbed.example_testbed import ExampleTestbed
from asimov.live_rulebook_runner import LiveRulebookRunner
from asimov.rulebook import Rulebook
from testcases.base import TestCase

from ..channels import DEFAULT_STATE
from ..dut.example_dut import ExampleDut

logger = logging.getLogger(__name__)


class BaseExampleDutTest(TestCase):
    """Base test case for example_dut: starts the testbed + DUT, tags DUT telemetry, wires evaluation - no test sequence logic."""

    TEST_NAME = "base_example_dut_test"
    RULEBOOKS: List[Rulebook] = []

    DEVICES = ExampleTestbed.DEVICES + ExampleDut.DEVICES
    """The union of what the testbed owns (DAQ, power supply) and what the DUT
    façade owns (the DUT). Each declares only its own, so neither has to know
    about the other - see testcases/base.py's DEVICES."""

    POWER_SUPPLY_VOLTAGE = 24.0
    POWER_SUPPLY_CURRENT = 2.0

    def __init__(self, test_id: Optional[str] = None, require_engine: bool = True):
        super().__init__(test_id, require_engine=require_engine)
        self.testbed: Optional[ExampleTestbed] = None
        self.dut: Optional[ExampleDut] = None
        self._runner_started = False

    def pre_test_setup(self) -> None:
        self.testbed = ExampleTestbed()
        self.testbed.start()

        power_supply = self.testbed.power_supply
        power_supply.set_output(voltage=self.POWER_SUPPLY_VOLTAGE, current=self.POWER_SUPPLY_CURRENT)
        power_supply.enable_output(True)

        self.dut = ExampleDut()
        self.dut.start()

        self._seed_channels()

        self.runner = LiveRulebookRunner(
            test_id=self.test_id,
            rulebooks=self.RULEBOOKS,
            publisher=self._publisher,
        )
        self.runner.start(self.dut.telemetry)
        self._runner_started = True

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
        if self._runner_started:
            # Stop the rulebook runner's background thread before the DUT -
            # it relies on telemetry still flowing to notice the stop signal
            # promptly (see LiveRulebookRunner._run()).
            self.teardown_step("stop rulebook runner", self.runner.stop)

        if self.dut is not None:
            self.teardown_step("stop DUT", self.dut.stop)

        if self.testbed is not None:
            self.teardown_step("stop testbed", self.testbed.stop)
