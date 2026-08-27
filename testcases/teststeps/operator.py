"""Test steps that wait for a person, shared by every DUT.

await_operator: block until somebody acknowledges an instruction.
prompt_for_run_details: block until somebody says which DUT, which
ticket and what load - the answers a stored run has to carry to be
attributable later.

Both are functions taking a test_case, not methods on it: a step is
business logic and belongs beside the other steps, while TestCase holds
only what every test needs regardless of what it does. A DUT package
imports what it wants from here and leaves the rest - a manual test that
exists to poke at hardware by hand has nothing to attribute and does not
prompt at all.

Neither blocks on input(). Both poll for a marker file, so
check_should_continue() still runs on every tick and a fatal bound, a
stop request or a lost recorder ends the run instead of waiting on
somebody who may have walked away. That matters most at exactly the
point these are called: on a lifting axis, the step before an operator
prompt can be the one that leaves the load held by nothing.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Dict, NamedTuple, Optional, Sequence, Tuple

from testcases.base import TestCase
from testcases.step import step
from testcases.teststeps.duts import serials_for
from testcases.utils import Stopwatch, spawn_operator_prompt

logger = logging.getLogger(__name__)

OPERATOR_POLL_INTERVAL_S = 0.1
"""How often the marker file is checked while waiting for a person. Short
because the wait is answered by a human clicking, and a slow poll shows up as
the window taking a moment to close after they did."""


class RunDetail(NamedTuple):
    """One thing the operator is asked for before a run.

    `label` is read, `channel` is stored, and they are separate so rewording a
    prompt cannot rename a channel that stored runs are keyed by. `choices` makes
    the prompt a dropdown, and is enforced on the answer however it arrives."""

    label: str
    channel: str
    choices: Tuple[str, ...] = ()


def run_detail_fields(dut: str) -> Tuple[RunDetail, ...]:
    """The three details every attributable run carries, for one DUT package.

    The serial is a dropdown of what this DUT can actually run (see
    duts.serials_for); the ticket and the load are free text. A DUT with no
    catalogued units gets an empty dropdown, which the prompt will refuse to
    accept an answer for - a stand whose units nobody has listed cannot file a
    run against one."""
    return (
        RunDetail("DUT SN", "dut_serial_number", serials_for(dut)),
        RunDetail("ER Ticket", "er_ticket"),
        RunDetail("Load (lb)", "load_lb"),
    )


def _await_ack(
    test_case: TestCase,
    instruction: str,
    fields: Sequence[str] = (),
    choices: Optional[Dict[str, Sequence[str]]] = None,
) -> str:
    """Publish an instruction, wait for the operator's marker, and return its
    contents - empty for a plain acknowledgement, JSON when values were asked for.

    A window opens with a button (tools/operator_prompt.py), and
    `python -m tools.operator_ack` leaves the same marker from a terminal, which
    is what a stand with no display uses. Either way this polls for the marker
    file rather than blocking on input(), so every tick still runs
    check_should_continue().

    The instruction is published as `operator_prompt` and cleared afterwards, so
    a recorded run shows what it was waiting for rather than looking like a
    hang."""
    path = test_case.operator_ack_path()
    path.unlink(missing_ok=True)  # a stale ack from an earlier run must not skip this
    test_case.set_state("operator_prompt", instruction)
    logger.warning("test %s: WAITING FOR OPERATOR - %s", test_case.test_id, instruction)
    logger.warning("test %s: click the window, or `python -m tools.operator_ack`", test_case.test_id)

    window = spawn_operator_prompt(test_case.test_id, instruction, fields, choices)
    clock = Stopwatch()
    try:
        while not path.exists():
            test_case.check_should_continue()
            time.sleep(OPERATOR_POLL_INTERVAL_S)
        answered = path.read_text()
        path.unlink(missing_ok=True)
        logger.info(
            "test %s: operator acknowledged after %.0fs", test_case.test_id, clock.elapsed_s(),
        )
        return answered
    finally:
        test_case.set_state("operator_prompt", None)
        # However this ended, the window is asking for something nobody is
        # waiting for any more, and a stale one left on a stand's screen is worse
        # than none.
        if window is not None:
            window.terminate()


@step
def await_operator(test_case: TestCase, instruction: str) -> None:
    """Block until a person acknowledges `instruction`. See _await_ack for the
    wait itself."""
    _await_ack(test_case, instruction)


@step
def prompt_for_run_details(
    test_case: TestCase, fields: Sequence[RunDetail]
) -> Dict[str, str]:
    """Ask the operator for the details that identify this run, and publish them.

    A field with choices is a dropdown and its answer is checked against them - the
    window cannot produce anything else, but `tools.operator_ack --answer` can.

    Published as run state, so the engine merges them into every recorded row. The
    channels have to be seeded (each DUT's channels.py) or the engine fixes its
    header before they exist and drops them. Called before anything is energized:
    it needs a person and does not need the stand."""
    answered = _await_ack(
        test_case,
        "enter this run's details",
        [field.label for field in fields],
        {field.label: field.choices for field in fields if field.choices},
    )
    try:
        answers = json.loads(answered) if answered else {}
    except ValueError:
        answers = {}

    details: Dict[str, str] = {}
    for field in fields:
        value = answers.get(field.label)
        if not value:
            raise RuntimeError(
                f"test {test_case.test_id}: no answer for {field.label!r} - a run that cannot be "
                "attributed to a DUT is not worth the hours it takes. Acknowledge with the "
                "window, or `python -m tools.operator_ack --answer "
                f"'{field.label}=...'` for a stand with no display"
            )
        if field.choices and value not in field.choices:
            raise RuntimeError(
                f"test {test_case.test_id}: {value!r} is not one of the values {field.label!r} "
                f"accepts ({', '.join(field.choices)}). A serial the record cannot match to a DUT "
                "is worse than no serial, so this is refused rather than stored"
            )
        details[field.channel] = value
        test_case.set_state(field.channel, value)
    logger.info("test %s: run details %s", test_case.test_id, details)
    return details
