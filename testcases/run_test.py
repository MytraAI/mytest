"""Generic entry point for running any registered TestCase by name -
see registry.py for how a test gets registered and REGISTERED_TESTS for
what's available. Not a demo: runs the selected test for real by
default. --mock is forwarded to every factory uniformly; a test that
has no real/mock distinction (e.g. anything under example_dut) simply
ignores it.

Run with (from the repo root):
    python -m testcases.run_test --test ydrive.manual
    python -m testcases.run_test --test ydrive.endurance_cycle --mock
"""
from __future__ import annotations

import argparse
import logging

from .registry import REGISTERED_TESTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test", required=True, choices=sorted(REGISTERED_TESTS), help="registered test key, e.g. ydrive.manual"
    )
    parser.add_argument("--mock", action="store_true", help="run against a mock backend, for tests that support it")
    parser.add_argument("--test-id", default=None, help="override the auto-generated test_id")
    args = parser.parse_args()

    test_case = REGISTERED_TESTS[args.test](args.test_id, args.mock)
    logger.info("running %s (test_id=%s)", args.test, test_case.test_id)
    test_case.run()


if __name__ == "__main__":
    main()
