# Mytest

A hardware-in-the-loop test framework for motion test stands: one process per
device, a test process that sequences the stand and decides pass/fail, and a
telemetry engine that records every run. `AI/Mytest.md` is the architecture and
the reasoning behind it; this file is how to run one.

## Setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/). Python 3.14+.

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell), then open a new terminal so PATH picks it up
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

uv sync
```

Every command below is run from the repo root and works the same on Windows,
macOS and Linux.

## Running a test

Two terminals. **The telemetry engine goes first** - a test refuses to start if
nothing is recording, and aborts if recording stops mid-run, because a run's
whole product is its record.

```bash
# terminal 1 - leave it running
uv run python -m telemetry_engine.main

# terminal 2
uv run python -m tools.run_test --test ydrive.endurance_cycle
```

`--test` takes any key from `testcases/registry.py`:

| key | what it does |
|---|---|
| `ydrive.endurance_cycle` | cycles the ydrive axis indefinitely, brake holding each dwell |
| `ydrive.manual` | keeps the stand alive for an operator to drive by hand (pair with `tools.manual_gui`) |
| `ydrive.base` | setup and teardown only, no sequence |
| `example_dut.cycle_position` | fully simulated - no hardware touched, good for checking the stack works |
| `example_dut.base` | fully simulated, returns immediately |

A status page opens automatically for every run and stays up afterwards showing
the final result.

## Stopping a test

```bash
uv run python -m tools.stop_test
```

With no arguments it discovers whichever test is running. This is the preferred
way on every OS: it leaves a marker file the test's own poll loop checks, so
teardown always runs - the axis is idled and both rails dropped before the
drivers are terminated. The status page's "Stop test" button does the same
thing, and `Ctrl+C` in the test's terminal also tears down cleanly.

Nothing helps against Task Manager's "End Task", a bare `taskkill /F`, or
`SIGKILL` - those bypass teardown entirely and can leave the stand energized.

## Where results go

`~/Desktop/mytestresults`, created by the engine at startup. One directory per
run, named after the test and when it started:

```
~/Desktop/mytestresults/
  runs/endurance_cycle_test_2026-08-17_16-29-31/
    verdict.json              how the run ended, every bound violation, completeness
    odrive/telemetry.csv      one row per frame, one column per channel
    odrive/logs.txt           that driver's own detailed log, e.g. a decoded fault
    cpx400dp/telemetry.csv
    cpx400dp/logs.txt
  raw/<device>/telemetry_<session>.csv    frames belonging to no run
```

Pass `--output-dir` to the engine to put them elsewhere; the test process reads
the engine's actual directory from its heartbeat, so the two cannot disagree.
`--test-id <name>` on `run_test` names the run directory yourself instead of
using the test-and-timestamp default.

## Things that will stop you

- **`--mock` only substitutes the ODrive.** The CPX400DP driver has no mock
  backend, so `ydrive.* --mock` still opens a socket to a real supply and still
  switches real rails. It is not a dry run. For a genuinely hardware-free check,
  use `example_dut.cycle_position`.
- **The supply's address moves.** It reports DHCP on a segment with no DHCP
  server, so it self-assigns a link-local address that changes on a collision.
  `uv run python -m tools.find_cpx400dp` finds it again by asking every address
  on the segment who it is; the answer goes in that stand's testbed as
  `CPX400DP_HOST`.
- **The ODrive's USB setup is undocumented.** Linux needs udev rules for
  non-root USB access; Windows needs a WinUSB/libusb driver (commonly via
  Zadig). Neither is covered here yet and either will block first bring-up.

## Tests

```bash
uv run pytest
```

Hardware-free and fast - no subprocesses, no instruments, no ZeroMQ.

## Layout

| | |
|---|---|
| `hardware/` | one driver per device, plus the command/telemetry servers they share |
| `testbeds/` | the physical fixture: starts a stand's drivers, owns its rails |
| `testcases/` | test cases, their steps, and the Rulebooks that judge a run |
| `telemetry_engine/` | the recording process |
| `protocol/` | anything two processes must agree on: wire schemas, paths, verdicts |
| `tools/` | operator entry points - see `tools/README.md` |
| `tests/` | the hardware-free unit suite |
