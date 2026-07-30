# Tools

Operator-facing entry points that sit outside the
hardware/testcases/telemetry_engine process architecture proper - none of
them are one of the five processes described in `AI/Mytest.md`, and nothing
in that architecture depends on any of them existing. `run_test.py` starts a
test case; `stop_test.py`, `operator_dashboard.py`, and `manual_gui.py` each
connect to an already-running process as just another client, rather than
being part of the test lifecycle themselves.

## run_test.py

Generic entry point for running any registered `TestCase` by name - see
`testcases/registry.py` for how a test gets registered and what's available.
Not a demo: runs the selected test for real by default.

```
python -m tools.run_test --test ydrive.manual
python -m tools.run_test --test ydrive.endurance_cycle --mock
```

**The telemetry engine must already be running**, or the test refuses to
start:

```
python -m telemetry_engine.main          # in its own terminal, first
```

A run's whole product is its record, so a test that would record nothing
fails immediately rather than moving hardware for no result - and if the
engine dies mid-run, the test aborts and tears down cleanly. Each run lands
in `<output_dir>/runs/<test_id>/` as a `verdict.json` plus per-device wide
telemetry CSVs; see `AI/Mytest.md`'s "The per-run result record". Only the
demo scripts opt out of this (`require_engine=False`).

`--mock` is forwarded to every factory uniformly; a test with no real/mock
distinction (e.g. anything under `example_dut`) simply ignores it. Replaces
one-off runner scripts per test case - `demo_testcase_run.py`/
`demo_ydrive_test_run.py` still exist alongside it for their own distinct
purpose (proving the Telemetry Publisher's tagging plumbing works), not for
operationally running a test case.

A deliberate stop (the dashboard's "Stop test" button, `stop_test.py`, or a
plain `SIGTERM`) is reported as a plain "test ...: stopped" log line and a
clean exit (code 0), not a traceback - `TestCase.run()` re-raises
`StopRequested`/`SystemExit` after tearing down cleanly purely so a caller
can tell a stop happened, not because it's a failure.

## stop_test.py

Cross-platform, OS-signal-independent way to stop a running test case - see
`TestCase.check_stop_requested()` (`testcases/base.py`) and `AI/Mytest.md`'s
OS compatibility section for why this exists: on Windows, neither
`Popen.terminate()` nor `os.kill(pid, SIGTERM)` reach a process's own signal
handling. This sidesteps OS signals entirely - it leaves a marker file the
target test's own poll loop already checks every ~10ms.

```
python -m tools.stop_test
python -m tools.stop_test --test-id <test_id>
```

With no `--test-id`, discovers whichever test is currently running via the
run-state stream (there's only ever one) rather than requiring the operator
to already know its auto-generated `test_id`. That stream carries nothing but
the run's identity and published state, so discovery reads one small message
rather than filtering telemetry to find it. Cannot stop a test
that's been killed via Task Manager, a bare `taskkill /F`, or `SIGKILL` -
those bypass all in-process code on any OS, by design; this only helps if
it's what's actually used to stop a test, instead of a raw kill.

Also works with `--test-id` against a test that's *already finished* but
still lingering on its operator status page (see `operator_dashboard.py`
below) - closes it out the same way. Auto-discovery doesn't work for that
case, though: by the time a test is done its run-state publisher has been
stopped, so there's no state frame left to discover a `test_id` from.

## operator_dashboard.py

Lightweight, locally-hosted status page automatically spawned at the start
of every test case (`TestCase.run()`, before `pre_test_setup()` even
starts) - not launched directly by an operator like the other tools here.
Read-only except for one "Stop test" button, which reuses `stop_test.py`'s
own `request_stop()` rather than a second implementation of what "stop"
means. Shows `test_id`/`test_name`, a live status
(`running`/`passing`/`failing`/`stopped`), and an error message if
something actually breaks - deliberately no command-sending capability at
all, since this is shown for every test including fully automated ones,
where letting an operator inject arbitrary commands could interfere with
the test's own control logic.

Built on the stdlib `http.server`, not a new web framework dependency - the
whole surface is three endpoints (the page, a JSON `/status` poll, and
`POST /stop`). Status/error are pushed into it explicitly by
`TestCase.run()` rather than it polling the test case itself, so it can
correctly show an error even for a failure that happens before any
Rulebook runner exists yet.

The header reads "Mytest Status: Running/Complete/Aborted" ("Mytest" in the
brand's purple), and the page background/a "Result: Pass/Fail" line reflect
pass/fail state - light green and "Pass" when passing, light red and "Fail"
when failing. While the test is still running, both are driven by the
Rulebook's own *live* `test_status` off the run-state stream (a
non-fatal bound violating doesn't raise anything, so this can go PASS/FAIL
before the test reaches its own conclusion); once the test finishes, its
actual outcome takes over. A large ASCII animation in the middle of the
page - a `-` bouncing back and forth while running, a plain `X` once it
isn't - gives an at-a-glance running/not-running signal independent of the
color.

Once the test finishes, the page stays up showing that final result
(`TestCase.run()`'s `_wait_until_interrupted()` blocks the process for
exactly this - a daemon thread dies with its process either way, so
persisting the page requires keeping the process itself alive) until closed
via `Ctrl+C`/`SIGTERM` in that terminal, or `stop_test.py --test-id <id>`
again.

If an operator instead just closes the browser tab, none of that happens -
the lingering process (and its port) would otherwise sit there
indefinitely, silently costing the *next* test its own dashboard. So
`spawn_operator_dashboard()` calls `reclaim_stale_dashboard()` first: if
the port is still held by a previous test, it leaves it the same stop
marker `stop_test.py` would (not a raw kill) and waits a few seconds for
it to let go before the new dashboard binds. See that function's
docstring in `operator_dashboard.py` for the full reasoning, including
what happens if the old process doesn't free up in time.

Not run directly - see `testcases/utils.py`'s `spawn_operator_dashboard()`
for how it gets started, and `testcases/base.py`'s `TestCase.run()` for how
its status is set.

## manual_gui.py

A Tkinter GUI for manually viewing telemetry and sending commands to hardware
while a test case (typically `ManualTest` - see
`testcases/ydrive/testcases/testcases.py`) is running. It has no
device-specific knowledge at all: telemetry channels are discovered live from
whatever keys show up in an incoming frame, and a command device's available
actions are discovered live via `list_actions()` - so it works unmodified
against any current or future testcase, including hardware this repo doesn't
support yet. See the module's own docstring for the full design rationale
(per-device telemetry vs. the run-state stream, why command values are
edited as JSON rather than bare values, the live graph's buffering/pause
semantics).

Run with (from the repo root):

```
python -m tools.manual_gui
```

Typical session: start a test case first (e.g.
`python -m tools.run_test --test ydrive.manual`), then launch the GUI,
add the device's command endpoint (e.g. `tcp://127.0.0.1:5580` for the
ODrive) via the "Add devices" panel, check channels to graph them or command
channels to send to them. The run-state stream (test-level context -
`test_id`/`test_name`/Rulebook bound-status/`current_step`) auto-connects on
startup; no
test case needs to be running for the GUI itself to open, or for raw
per-device telemetry/command connections to work. When you're done, stop the
test case with `python -m tools.stop_test` in a separate terminal rather than
killing its process directly.

## Adding a new tool

Prefer a new top-level module here (matching the shape of the tools above)
over growing an existing one into something it isn't; update this README with
its own section the same way.
