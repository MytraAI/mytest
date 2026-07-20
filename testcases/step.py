"""@step decorator: marks a plain function as one named step of a
test's main_execution sequence.

Step functions are called directly as step_fn(test_case, ...) rather
than as methods - see testcases/example_dut/teststeps/ for a concrete
example. The decorator logs the step's start and completion (with
elapsed time, via Stopwatch), and publishes a current_step state
channel holding the step's name via test_case.set_state() - the same
mechanism used for test_status/position_target elsewhere. That means
which step was running at a given moment is also recorded in
telemetry/CSV output, e.g. to correlate a Rulebook violation with a
specific step.

The first positional argument (the test case instance) must have
.set_state() and .test_id, matching TestCase/BaseExampleDutTest's
setup. This decorator itself is generic across any DUT's test steps -
unlike the concrete step functions that use it, which are DUT-specific.
"""
from __future__ import annotations

import functools
import logging
from typing import Any, Callable, TypeVar

from .stopwatch import Stopwatch

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def step(func: F) -> F:
    """Decorator for one named step of a test's main_execution sequence."""
    step_name = func.__name__

    @functools.wraps(func)
    def wrapper(test_case, *args, **kwargs):
        test_case.set_state("current_step", step_name)
        logger.info("test %s: starting step %s", test_case.test_id, step_name)
        clock = Stopwatch()
        result = func(test_case, *args, **kwargs)
        logger.info("test %s: step %s completed in %.1fs", test_case.test_id, step_name, clock.elapsed_s())
        return result

    return wrapper
