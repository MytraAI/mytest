"""@step decorator: marks a function as one named step of a test's
main_execution sequence. Step functions are called directly as
step_fn(test_case, ...) rather than as methods.

Logs the step's start/completion with elapsed time, publishes a
current_step state channel via test_case.set_state(), and calls
test_case.check_fatal_violation()/check_stop_requested() once before
and once after the step runs - so a fatal Rulebook violation or an
external stop request (see tools/stop_test.py) is noticed at step
boundaries even if the step never polls internally. This doesn't catch
either mid-step for a step that runs long without polling on its own -
see testcases/asimov/live_rulebook_runner.py's docstring for why, and
for test_case.wait_for() to close that gap yourself in a long-running
step.

The first positional argument must have .set_state(), .test_id,
.check_fatal_violation(), and .check_stop_requested() - i.e. a
TestCase.
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
        test_case.check_fatal_violation()
        test_case.check_stop_requested()
        test_case.set_state("current_step", step_name)
        logger.info("test %s: starting step %s", test_case.test_id, step_name)
        clock = Stopwatch()
        result = func(test_case, *args, **kwargs)
        test_case.check_fatal_violation()
        test_case.check_stop_requested()
        logger.info("test %s: step %s completed in %.1fs", test_case.test_id, step_name, clock.elapsed_s())
        return result

    return wrapper
