# Tools

Operator-facing tools that sit outside the hardware/testcases/telemetry_engine
process architecture proper - they connect to already-running processes as
just another client, rather than being part of the test lifecycle themselves.

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
`python -m testcases.run_test --test ydrive.manual`), then launch the GUI,
add the device's command endpoint (e.g. `tcp://127.0.0.1:5580` for the
ODrive) via the "Add devices" panel, check channels to graph them or command
channels to send to them. The tagged-telemetry stream (test-level context -
`test_id`/`test_name`/Rulebook bound-status) auto-connects on startup; no
test case needs to be running for the GUI itself to open, or for raw
per-device telemetry/command connections to work.

## Adding a new tool

There's no scaffolding to follow yet - this directory holds exactly one tool
today. If a second one is added, prefer a new top-level module here (matching
`manual_gui.py`'s shape) over growing this one into something it isn't;
update this README with its own section the same way.
