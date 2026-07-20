"""Base test case for example_dut.

Handles the shared setup any example_dut test needs:
- starts the physical testbed (DAQ + power supply instruments), which
  also gives us a ready-to-use power supply command client
- powers the DUT via the testbed's power supply
- starts the DUT itself
- tags the DUT's own telemetry (position/velocity/current) via the
  Telemetry Publisher
- wires up live Rulebook evaluation

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

Note: TelemetryPublisher here is pointed at the DUT's raw telemetry
stream (not the DAQ's) via raw_endpoint. The DUT's own
position/velocity/current are what's evaluated for this DUT's tests,
not the DAQ's generic channels. The DAQ still runs as part of the
testbed - it's an instrument this test stand always has available,
this test just doesn't happen to watch it.

Also seeds a default for every state channel a subclass's steps might
publish (see ../channels.py and _seed_channels() below), so the full
channel set exists in the stream from frame 1 instead of appearing
incrementally as steps happen to compute things.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from hardware.protocol import DEFAULT_DUT_TELEMETRY_ENDPOINT
from testbeds.example_testbed.example_testbed import ExampleTestbed
from testcases.asimov.live_rulebook_runner import LiveRulebookRunner
from testcases.asimov.rulebook import Rulebook
from testcases.base import TestCase
from testcases.telemetry_publisher import TelemetryPublisher

from ..channels import DEFAULT_STATE
from ..dut.example_dut import ExampleDut

logger = logging.getLogger(__name__)


class BaseExampleDutTest(TestCase):
    """Base test case for example_dut: starts the testbed + DUT, tags DUT telemetry, wires evaluation - no test sequence logic."""

    TEST_NAME = "base_example_dut_test"
    RULEBOOKS: List[Rulebook] = []

    POWER_SUPPLY_VOLTAGE = 24.0
    POWER_SUPPLY_CURRENT = 2.0

    def __init__(self, test_id: Optional[str] = None):
        super().__init__(test_id)
        self.testbed: Optional[ExampleTestbed] = None
        self.dut: Optional[ExampleDut] = None
        self._publisher: Optional[TelemetryPublisher] = None
        self.runner: Optional[LiveRulebookRunner] = None
        self._publisher_started = False
        self._runner_started = False

    def pre_test_setup(self) -> None:
        self.testbed = ExampleTestbed()
        self.testbed.start()

        power_supply = self.testbed.power_supply
        power_supply.set_output(voltage=self.POWER_SUPPLY_VOLTAGE, current=self.POWER_SUPPLY_CURRENT)
        power_supply.enable_output(True)

        self.dut = ExampleDut()
        self.dut.start()

        self._publisher = TelemetryPublisher(
            test_id=self.test_id,
            test_name=self.TEST_NAME,
            raw_endpoint=DEFAULT_DUT_TELEMETRY_ENDPOINT,
        )
        self._publisher.start()
        self._publisher_started = True
        self._seed_channels()

        self.runner = LiveRulebookRunner(
            test_id=self.test_id,
            rulebooks=self.RULEBOOKS,
            publisher=self._publisher,
        )
        self.runner.start(self.dut.telemetry)
        self._runner_started = True

    def set_state(self, name: str, value: Any) -> None:
        """Publish a named state value (e.g. current_step, a derived
        channel, a gating flag) merged into every tagged telemetry
        frame from now on.

        This is the one sanctioned way for test steps and the @step
        decorator to record test-case state - callers never need to
        see the underlying TelemetryPublisher."""
        self._publisher.set_state(name, value)

    def _seed_channels(self) -> None:
        """Publish a default for every state channel this test can
        produce, so each one exists in the stream from frame 1 instead
        of appearing incrementally as steps happen to compute things
        (see ../channels.py).

        Bound-status channels are derived from RULEBOOKS rather than
        hand-listed, since the Rulebook is already the single source
        of truth for bound names."""
        for name, default in DEFAULT_STATE.items():
            self._publisher.set_state(name, default)

        self._publisher.set_state("test_status", "PASS")
        for rulebook in self.RULEBOOKS:
            for bound in rulebook.bounds:
                self._publisher.set_state(f"{bound.label}_status", "PASS")

    def main_execution(self) -> None:
        logger.info("test %s: base case has no test logic - completing immediately", self.test_id)

    def post_test_teardown(self) -> None:
        if self._runner_started:
            # Stop the rulebook runner's background thread before the DUT -
            # it relies on telemetry still flowing to notice the stop signal
            # promptly (see LiveRulebookRunner._run()).
            self._teardown_step("stop rulebook runner", self.runner.stop)

        if self._publisher_started:
            self._teardown_step("stop telemetry publisher", self._publisher.stop)

        if self.dut is not None:
            self._teardown_step("stop DUT", self.dut.stop)

        if self.testbed is not None:
            self._teardown_step("stop testbed", self.testbed.stop)
