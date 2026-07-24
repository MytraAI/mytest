# Tools

Operator-facing entry points that sit outside the
hardware/testcases/telemetry_engine process architecture proper - none of
them are one of the five processes described in `AI/Mytest.md`, and nothing
in that architecture depends on any of them existing. `run_test.py` starts a
test case; `stop_test.py` and `manual_gui.py` each connect to an
already-running process as just another client, rather than being part of
the test lifecycle themselves.

## run_test.py

Generic entry point for running any registered `TestCase` by name - see
`testcases/registry.py` for how a test gets registered and what's available.
Not a demo: runs the selected test for real by default.

```
python -m tools.run_test --test ydrive.manual
python -m tools.run_test --test ydrive.endurance_cycle --mock
```

`--mock` is forwarded to every factory uniformly; a test with no real/mock
distinction (e.g. anything under `example_dut`) simply ignores it. Replaces
one-off runner scripts per test case - `demo_testcase_run.py`/
`demo_ydrive_test_run.py` still exist alongside it for their own distinct
purpose (proving the Telemetry Publisher's tagging plumbing works), not for
operationally running a test case.

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
tagged telemetry stream (there's only ever one) rather than requiring the
operator to already know its auto-generated `test_id`. Cannot stop a test
that's been killed via Task Manager, a bare `taskkill /F`, or `SIGKILL` -
those bypass all in-process code on any OS, by design; this only helps if
it's what's actually used to stop a test, instead of a raw kill.

## manual_gui.py

A Tkinter GUI for manually viewing telemetry and sending commands to hardware
while a test case (typically `ManualTest` - see
`testcases/ydrive/testcases/testcases.py`) is running. It has no
device-specific knowledge at all: telemetry channels are discovered live from
whatever keys show up in an incoming frame, and a command device's available
actions are discovered live via `list_actions()` - so it works unmodified
against any current or future testcase, including hardware this repo doesn't
support yet. See the module's own docstring for the full design rationale
(per-device telemetry vs. the shared tagged stream, why command values are
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
channels to send to them. The tagged-telemetry stream (test-level context -
`test_id`/`test_name`/Rulebook bound-status) auto-connects on startup; no
test case needs to be running for the GUI itself to open, or for raw
per-device telemetry/command connections to work. When you're done, stop the
test case with `python -m tools.stop_test` in a separate terminal rather than
killing its process directly.

## Adding a new tool

Prefer a new top-level module here (matching the shape of the tools above)
over growing an existing one into something it isn't; update this README with
its own section the same way.
