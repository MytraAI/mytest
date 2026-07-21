# Hardware: command + telemetry servers, and their clients

Implements the "Hardware driver process" from the telemetry engine
architecture doc: a Command server (request/reply) and a Telemetry
server (pub/sub), both built on ZeroMQ, both talking to a single
`HardwareBackend` that owns the actual device connection. Also holds
the client-side half of that same protocol (`clients/`), since it's
the same command/telemetry logic from the caller's point of view -
kept separate from `testcases/`, which orchestrates test sequences
using this interface but isn't part of it.

This process is generic across device types, not DAQ-specific.
`HardwareBackend` only requires a universal core -
`connect`/`disconnect`/`get_status`/`stream_samples` - plus one
generic `execute(action, **params)` for anything device-specific.
`command_server.py`/`telemetry_server.py` never need to change no
matter what device gets added; only a new backend (and, for
ergonomics, a thin command-client subclass) is needed. Four device
folders exist today: `mock_daq`, `mock_power_supply`, and `mock_dut`
each hold a single simulated backend (`MockDutBackend` being the DUT
itself - a first-order position/velocity/current servo approximation,
proving the generic framework fits a control loop, not just
readbacks). `odrive/` is the first real (non-simulated) device: it
holds both `OdriveBackend` (real, talks to an actual ODrive motor
controller over USB via the official `odrive` package) and
`MockOdriveBackend` (simulated, for local development without hardware
attached) side by side - see that folder's own section below. Each
device runs as its own process, on its own ports - see "Running it"
below.

## Layout

`hardware/` is a sibling of `testcases/` and `telemetry_engine/`
under the repo root (`Mytest/`), alongside `AI/` (the architecture
doc). Per the architecture doc, these are separate OS processes that
only talk over the command/telemetry wire protocol, never via direct
imports of each other's internals.

```
Mytest/
  hardware/
    protocol.py                shared message schemas (CommandRequest/Reply, TelemetryFrame, TaggedTelemetryFrame)
    backend.py                  abstract HardwareBackend interface (universal core + execute())
    command_server.py          ZeroMQ REP server, dispatches commands to the backend
    telemetry_server.py         ZeroMQ PUB server, forwards backend.stream_samples()
    runner.py                   shared process wiring (connect, run servers, signal-stop, disconnect) - device-agnostic
    mock_daq/
      mock_backend.py             MockDaqBackend - simulated, sine waves + noise, no real hardware needed
      mock_channels.py             TELEMETRY_CHANNELS/COMMAND_CHANNELS - this backend's declared channel surface
      mock_daq_command_client.py   DaqCommandClient - named sugar for DAQ actions, layered on the generic CommandClient
      main.py                      entry point: MockDaqBackend + DAQ default ports, logs a "SIMULATED DEVICE" warning, calls runner.run()
    mock_power_supply/
      mock_backend.py             MockPowerSupplyBackend - simulated, setpoint + noise readback
      mock_channels.py             TELEMETRY_CHANNELS/COMMAND_CHANNELS - this backend's declared channel surface
      mock_power_supply_command_client.py   PowerSupplyCommandClient - named sugar for power supply actions, layered on the generic CommandClient
      main.py                      entry point: MockPowerSupplyBackend + its default ports, logs a "SIMULATED DEVICE" warning, calls runner.run()
    mock_dut/
      mock_backend.py             MockDutBackend - simulated, first-order position/velocity/current servo approximation
      mock_channels.py             TELEMETRY_CHANNELS/COMMAND_CHANNELS - this backend's declared channel surface
      mock_dut_command_client.py   DutCommandClient - named sugar for DUT command channels (position_input/gains), layered on the generic CommandClient
      main.py                      entry point: MockDutBackend + DUT default ports, logs a "SIMULATED DEVICE" warning, calls runner.run()
    odrive/
      odrive_backend.py            OdriveBackend - REAL, talks to actual ODrive hardware over USB via the official odrive package (firmware 0.6.x / Pro/S1)
      mock_backend.py              MockOdriveBackend - simulated, same channel surface as the real backend
      odrive_channels.py           TELEMETRY_CHANNELS/COMMAND_CHANNELS - curated (not exhaustive): ~110 telemetry + ~74 command channels a test author would realistically use or need for diagnosis - see its docstring for what was deliberately cut
      odrive_command_client.py     OdriveCommandClient - named sugar for ODrive actions, layered on the generic CommandClient
      main.py                      entry point: real backend by default, --mock to run MockOdriveBackend instead, calls runner.run()
    clients/
      command_client.py            generic CommandClient - universal core + execute() + verify_actions()
      telemetry_client.py          Telemetry Client/subscriber - fully generic + verify_channels(), no device-specific subclasses needed
    demos/
      demo_end_to_end.py          launches the DAQ driver, drives a mock test, prints telemetry frames
      demo_power_supply.py         launches the power supply driver, sets/enables an output, prints telemetry frames
      demo_dut.py                  launches the DUT driver, commands position/gains, prints telemetry frames
      demo_odrive.py                launches the ODrive driver (always --mock), verifies channels, commands velocity, prints telemetry frames
    README.md                   this file
  testcases/               test case lifecycle framework + Telemetry Publisher + Rulebook framework,
                           with concrete test cases/rulebooks per device under testcases/<device>/
                           (see its own docstrings)
  telemetry_engine/        aggregator + evaluation + storage
  AI/                      architecture doc
  pyproject.toml           dependencies (incl. pyzmq) for the whole repo
```

## Running it

All commands below are run from the repo root (`Mytest/`), so that
`hardware` and its sibling packages resolve as top-level imports:

```
python -m hardware.demos.demo_end_to_end
```

That launches the DAQ driver as a subprocess, connects, loads a setup,
starts acquisition, prints five telemetry frames, then stops and tears
everything down. DAQ ports default to `tcp://127.0.0.1:5555` (command)
and `tcp://127.0.0.1:5556` (telemetry) - see `protocol.py`.

```
python -m hardware.demos.demo_power_supply
```

Same shape, for the power supply backend instead: connects, sets an
output, reads frames with the output disabled and enabled, then
disables and tears down. Power supply ports default to
`tcp://127.0.0.1:5560`/`5561`, so it can run alongside the DAQ process
simultaneously without a port conflict.

```
python -m hardware.demos.demo_dut
```

Same shape again, for the DUT backend: connects, commands gains and a
position setpoint, reads frames as the servo model settles, then tears
down. DUT ports default to `tcp://127.0.0.1:5570`/`5571`, so it can run
alongside the DAQ and power supply processes simultaneously without a
port conflict.

```
python -m hardware.demos.demo_odrive
```

Same shape again, for the ODrive backend (always `--mock` - this demo
never touches real hardware): connects, verifies the declared
command/telemetry channel surface against the live process, commands
velocity control, reads frames as the simulated axis spins up, then
tears down. ODrive ports default to `tcp://127.0.0.1:5580`/`5581`.

To run a driver on its own (e.g. to point multiple clients at it):

```
python -m hardware.mock_daq.main             # DAQ
python -m hardware.mock_power_supply.main    # power supply
python -m hardware.mock_dut.main             # DUT
python -m hardware.odrive.main --mock        # ODrive, simulated
python -m hardware.odrive.main               # ODrive, REAL hardware over USB
```

Each device is its own OS process with its own dedicated entry-point
script - run whichever ones you need live at once. All accept
`--command-endpoint`/`--telemetry-endpoint` overrides if you need
non-default ports (e.g. running a second instance of the same device
type). ODrive ports default to `tcp://127.0.0.1:5580`/`5581`.

`hardware.odrive.main` is the one entry point that isn't mock-only: no
`--mock` flag means it attempts a real USB connection (via
`odrive.find_any()`), so running it without hardware attached will
fail at connect. Pass `--serial-number` if more than one ODrive is on
the same machine.

## Adding a new device type

1. Create a folder for it (e.g. `hardware/mock_<device>/`) with a
   `mock_backend.py` implementing `HardwareBackend` (see the top-level
   `backend.py` for the interface): `connect`, `disconnect`,
   `get_status`, `stream_samples`, and `execute(action, **params)` for
   whatever device-specific actions it needs (see
   `mock_power_supply/mock_backend.py` for the smallest example).
   Named `mock_backend.py` (not just `backend.py`) so it's unambiguous
   at a glance that it's simulated - a real implementation would live
   alongside it as e.g. `dewesoft_backend.py`, not replace it.
2. Add a `main.py` in that same folder: pick default command/telemetry
   endpoints, instantiate the new backend, log a warning that it's a
   simulated device, call `runner.run(backend, command_endpoint,
   telemetry_endpoint)` - copy `mock_power_supply/main.py` as a
   template.
3. Optionally add a thin `CommandClient` subclass in that same device
   folder (e.g. `mock_<device>/mock_<device>_command_client.py`) with
   named methods that call `self.execute(...)` - pure convenience for
   callers, not required. It imports the generic `CommandClient` from
   `clients/command_client.py` - only the truly device-agnostic
   client/telemetry code lives in `clients/` itself.
4. Nothing in `runner.py`, `command_server.py`, `telemetry_server.py`,
   or `TelemetryClient` needs to change - they only depend on the
   universal core and the generic wire protocol.

To go from a mock to a real device, implement the same
`HardwareBackend` interface against the real API as a new file
alongside the mock (e.g. `mock_daq/dewesoft_backend.py` for DewesoftX
via DCOM automation or the DSRemote Python library, leaving
`mock_daq/mock_backend.py` in place for local development without
hardware). `hardware/odrive/` is the working example of this shape:
`odrive_backend.py` (real, `odrive` package over USB) and
`mock_backend.py` (simulated) both implement `HardwareBackend`
identically, and `main.py` picks between them via a `--mock` flag
rather than main.py being mock-only like the other three devices'.
Note DewesoftX's Windows-only DCOM/DSRemote control means that
particular backend (and therefore its process) will need to run on the
Windows test stand PC, not in a general Linux environment - other
device types aren't necessarily subject to that constraint.

## What's deliberately NOT here

Per the architecture doc, any interlock that must trip in under 10ms
is configured directly in DewesoftX's own Math/Alarm/Digital-Out
modules, wired to a hardware relay. It does not go through
`set_digital_output` here, and it does not depend on this process
being alive. This driver's command/telemetry path is the soft
real-time supervisory layer, not the safety layer.

Also not here: the Telemetry Publisher (tags data with test-case
context before forwarding to the aggregator) lives in `testcases/`,
and the aggregator/evaluation/storage components live in
`telemetry_engine/` - see the architecture doc.

## Verified

`demos/demo_end_to_end.py` was run in development: channel list, setup load,
start/stop acquisition, and status all round-trip correctly, and
telemetry frames stream at the configured rate (~50 Hz in the mock).
Calling `start_acquisition` while already acquiring, and sending an
unknown command, both return a clean `CommandClientError` instead of
crashing the server.

`demos/demo_power_supply.py` was also run in development, on its own ports
alongside the DAQ process: connect, set_output, and enable_output all
round-trip correctly, telemetry reads back near-zero with the output
disabled and near-setpoint with it enabled - all through the exact
same `CommandServer`/`TelemetryServer` code as the DAQ, confirming the
generalized interface actually holds for a second, unrelated device
type.

`demos/demo_dut.py` was also run in development, on its own ports alongside
the DAQ and power supply processes: set_gains and set_position_input
round-trip correctly, and telemetry shows the commanded position step
settling smoothly through the first-order model - confirming the
generalized interface holds for a third, unrelated device type that's
a control loop (command channels feed back into read channels over
time) rather than a simple readback.

`hardware.odrive.main --mock` was run end-to-end in development:
launched as its own process, `OdriveCommandClient.verify_actions()`
and `TelemetryClient.verify_channels()` both passed against the live
process (74/74 command channels, 110/110 telemetry channels), a
representative sample of commands round-tripped across every
subsystem (control mode/state, a config setter, a motor-config setter,
a trap_traj setter, a board-level setter, a two-arg method), and a
telemetry frame containing all 109 channels round-tripped through
JSON exactly as the real wire protocol does. `OdriveBackend`,
`odrive_command_client.py`, and `mock_backend.py` are all generated
from one systematically-derived attribute-path table (see
`odrive_backend.py`'s module docstring) that was cross-validated in
code against `odrive_channels.py`'s declared channel lists - every
declared channel has exactly one table entry and vice versa, so
there's no drift between what's declared and what's implemented.
`odrive_backend.py` also asserts this same coverage match at import
time (`_validate_channel_coverage()`), so a future channel added to
one place and forgotten in the other fails loudly instead of silently
producing an incomplete frame. The generic path-resolution mechanism
itself (`_get_path`/`_set_path`/`_call_path` walking dotted attribute
chains) was sanity-checked against a synthetic stub object graph
mirroring the real `odrive` package's structure.

The channel list started out deliberately exhaustive (every channel in
ODrive's own 0.6.x API reference - ~450 telemetry + ~301 command
channels) to prove the generation approach against the full documented
surface, then was pruned back to the 110/74 kept today - removing
internal FOC/ACIM/sensorless diagnostics, per-phase calibration
coefficients, per-encoder-type config for hardware this test stand
doesn't use, CAN bus config, and other one-time-commissioning/
firmware-internals channels a test author would realistically never
reference and that wouldn't help diagnose a test failure. See
`odrive_channels.py`'s module docstring for the exact rationale.

`OdriveBackend` (the real backend) has NOT been run against actual
ODrive hardware. Every channel kept after the pruning pass is from the
"core" set whose exact attribute paths were individually cross-checked
against ODrive's own 0.6.x API reference (state, errors, motor
telemetry, controller setpoints/gains, trap_traj, board bus,
temperatures, endstops) - unlike the exhaustive version's long tail,
nothing kept here is unverified-but-plausible; it's all from the
verified core. Still worth confirming against odrivetool or an actual
device before trusting it blind, per the module docstring.

Robustness gaps found by building the first real (non-mock) backend -
none of which a mock could ever exercise, since mocks never fail this
way - were fixed and verified with synthetic tests (a stub `odrive`
module, a stub attribute-graph object, and a backend whose
`stream_samples()` raises on purpose):
- `runner.py`'s `run()` previously discarded a server task's exception
  via `asyncio.gather(..., return_exceptions=True)` without checking
  the result, so a real backend's `stream_samples()` raising (e.g. on
  a lost USB connection) would silently kill telemetry while the
  process kept running with the command server still up. `run()` now
  treats any server task dying on its own the same as a shutdown
  signal: it cancels the other task, disconnects the backend, logs
  which task failed and why, and re-raises so the process actually
  exits. Verified with a fake backend that raises after one frame -
  confirmed the exception propagates and shutdown happens cleanly -
  and confirmed a normal SIGTERM shutdown of an unrelated device
  (`mock_dut`) still exits 0 as before.
- `OdriveBackend.connect()` had no timeout on `odrive.find_any()`,
  which blocks indefinitely by default - a missing/unpowered device or
  wrong serial number would hang `connect()` (and the `connect`
  command's REQ/REP round-trip) forever. Added a `discovery_timeout_s`
  constructor param (also `main.py --discovery-timeout`) and a clear
  `HardwareError` on timeout. Verified with a stubbed `odrive` module
  whose `find_any()` always raises.
- `_read_all_channels()`'s bare `except AttributeError: return None`
  couldn't distinguish "this hardware config doesn't have this
  channel" (benign) from "the path table has a typo" (a real bug) -
  both silently returned `None` forever with no way to tell them
  apart. Now logs a warning the first time each channel is missing
  (not on every 20 Hz tick) - anything that isn't `AttributeError`
  still propagates uncaught, which is what lets the `runner.py` fix
  above actually catch a real disconnect. Verified against a stub
  object exposing only one of 109 channels: that one channel read
  correctly, the other 108 each warned exactly once across two calls.
- Added logging (`connect`/`disconnect`/missing-channel warnings) -
  previously silent - and `hardware/demos/demo_odrive.py`, matching every
  other device's `demo_<device>.py` convention (always `--mock`); this
  session's other verification had all been one-off scratch scripts
  that didn't persist in the repo.
