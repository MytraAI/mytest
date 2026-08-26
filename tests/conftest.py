"""Suite-wide guards: no test opens a window, and none of them can forget to.

Two production calls reach the operator's screen - spawn_operator_dashboard(),
which starts an HTTP server and opens a browser tab, and
spawn_operator_prompt(), which launches a Tk window in its own process. Both are
right for a run with a person in front of it and wrong for a test suite.

Individual tests were already patching them one call site at a time, which works
until a test does not: tests/test_heartbeat.py calls TestCase.run(), which opens
the dashboard, and running the suite on a developer's machine opened a browser
tab and left a server bound to its port. A test that has to remember is a test
that eventually forgets, so this is autouse and applies to every test in the
suite whether it knows about these functions or not.

Both are replaced with something returning None, which is what the production
code already handles: each documents that failing to start is logged rather than
raised, because the window is a convenience over the marker file the wait
actually polls for. A test that wants to assert on prompt behaviour still
patches its own stand-in, and that patch wins - a test's own monkeypatch is
applied after this fixture.
"""
from __future__ import annotations

import pytest

_SCREEN_CALLS = (
    ("testcases.utils", "spawn_operator_dashboard"),
    ("testcases.utils", "spawn_operator_prompt"),
    ("testcases.base", "spawn_operator_dashboard"),
    ("testcases.ydrive.teststeps.teststeps", "spawn_operator_prompt"),
    ("testcases.zdrive.teststeps.teststeps", "spawn_operator_prompt"),
)
"""Every binding, not just the definition. These are imported by name
(`from testcases.utils import spawn_operator_prompt`), so each importing module
holds its own reference and patching the source module alone would miss them."""


@pytest.fixture(autouse=True)
def no_operator_windows(monkeypatch):
    for module_name, attribute in _SCREEN_CALLS:
        module = pytest.importorskip(module_name)
        if hasattr(module, attribute):
            monkeypatch.setattr(module, attribute, lambda *args, **kwargs: None)
