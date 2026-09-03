"""Manual registry mapping a "<dut>.<test>" key to a factory that
constructs that TestCase - the single place a new test case gets
registered so tools/run_test.py can look it up by name instead of
every caller importing/instantiating it directly. A deliberately
manual list authors append to as new DUTs/test cases are added under
testcases/, rather than introspection/auto-discovery magic.

Every factory has the same call signature - (test_id, use_mock) -
regardless of whether the underlying TestCase's own __init__ actually
accepts use_mock (e.g. example_dut's tests don't, since that DUT has no
real backend to choose between). The factory itself absorbs that
difference, not whoever calls it - so run_test.py stays generic across
every registered test without needing to inspect each one's __init__.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

from .base import TestCase
from .example_dut.testcases.base_example_dut_test import BaseExampleDutTest
from .example_dut.testcases.halt_tests import CycleDutPositionTest
from .xdeploy.testcases.base_xdeploy_test import BaseXdeployTest
from .xdeploy.testcases.testcases import (
    CycleTest as XdeployCycleTest,
    ManualTest as XdeployManualTest,
)
from .ydrive.testcases.base_ydrive_test import BaseYdriveTest
from .ydrive.testcases.testcases import (
    BrakeEnduranceTest,
    CycleBrakeEnduranceTest,
    EnduranceCycleTest,
    ManualTest,
)
from .zdrive.testcases.base_zdrive_test import BaseZdriveTest
from .zdrive.testcases.testcases import (
    BrakeEnduranceTest as ZdriveBrakeEnduranceTest,
    BrakeHoldTest,
    CycleBrakeHoldTest,
    ManualTest as ZdriveManualTest,
)

REGISTERED_TESTS: Dict[str, Callable[[Optional[str], bool], TestCase]] = {
    "example_dut.base": lambda test_id, use_mock: BaseExampleDutTest(test_id=test_id),
    "example_dut.cycle_position": lambda test_id, use_mock: CycleDutPositionTest(test_id=test_id),
    "xdeploy.base": lambda test_id, use_mock: BaseXdeployTest(test_id=test_id, use_mock=use_mock),
    "xdeploy.manual": lambda test_id, use_mock: XdeployManualTest(test_id=test_id, use_mock=use_mock),
    "xdeploy.cycle": lambda test_id, use_mock: XdeployCycleTest(test_id=test_id, use_mock=use_mock),
    "ydrive.base": lambda test_id, use_mock: BaseYdriveTest(test_id=test_id, use_mock=use_mock),
    "ydrive.manual": lambda test_id, use_mock: ManualTest(test_id=test_id, use_mock=use_mock),
    "ydrive.endurance_cycle": lambda test_id, use_mock: EnduranceCycleTest(test_id=test_id, use_mock=use_mock),
    "ydrive.brake_endurance": lambda test_id, use_mock: BrakeEnduranceTest(test_id=test_id, use_mock=use_mock),
    "ydrive.cycle_brake_endurance": lambda test_id, use_mock: CycleBrakeEnduranceTest(test_id=test_id, use_mock=use_mock),
    "zdrive.base": lambda test_id, use_mock: BaseZdriveTest(test_id=test_id, use_mock=use_mock),
    "zdrive.manual": lambda test_id, use_mock: ZdriveManualTest(test_id=test_id, use_mock=use_mock),
    "zdrive.brake_hold": lambda test_id, use_mock: BrakeHoldTest(test_id=test_id, use_mock=use_mock),
    "zdrive.cycle_brake_hold": lambda test_id, use_mock: CycleBrakeHoldTest(test_id=test_id, use_mock=use_mock),
    "zdrive.brake_endurance": lambda test_id, use_mock: ZdriveBrakeEnduranceTest(test_id=test_id, use_mock=use_mock),
}
