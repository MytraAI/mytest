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
ergonomics, a thin command-client subclass) is needed. Three concrete
backends exist today: `MockDaqBackend`, `MockPowerSupplyBackend`, and
`MockDutBackend` - the last being the DUT itself (a first-order
position/velocity/current servo approximation), proving the generic
framework fits a control loop, not just readbacks. Each device runs as
its own process, on its own ports - see "Running it" below.

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
    clients/
      command_client.py            generic CommandClient - universal core + execute() + verify_actions()
      telemetry_client.py          Telemetry Client/subscriber - fully generic + verify_channels(), no device-specific subclasses needed
    demo_end_to_end.py          launches the DAQ driver, drives a mock test, prints telemetry frames
    demo_power_supply.py         launches the power supply driver, sets/enables an output, prints telemetry frames
    demo_dut.py                  launches the DUT driver, commands position/gains, prints telemetry frames
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
python -m hardware.demo_end_to_end
```

That launches the DAQ driver as a subprocess, connects, loads a setup,
starts acquisition, prints five telemetry frames, then stops and tears
everything down. DAQ ports default to `tcp://127.0.0.1:5555` (command)
and `tcp://127.0.0.1:5556` (telemetry) - see `protocol.py`.

```
python -m hardware.demo_power_supply
```

Same shape, for the power supply backend instead: connects, sets an
output, reads frames with the output disabled and enabled, then
disables and tears down. Power supply ports default to
`tcp://127.0.0.1:5560`/`5561`, so it can run alongside the DAQ process
simultaneously without a port conflict.

```
python -m hardware.demo_dut
```

Same shape again, for the DUT backend: connects, commands gains and a
position setpoint, reads frames as the servo model settles, then tears
down. DUT ports default to `tcp://127.0.0.1:5570`/`5571`, so it can run
alongside the DAQ and power supply processes simultaneously without a
port conflict.

To run a driver on its own (e.g. to point multiple clients at it):

```
python -m hardware.mock_daq.main             # DAQ
python -m hardware.mock_power_supply.main    # power supply
python -m hardware.mock_dut.main             # DUT
```

Each device is its own OS process with its own dedicated entry-point
script - run whichever ones you need live at once. Both accept
`--command-endpoint`/`--telemetry-endpoint` overrides if you need
non-default ports (e.g. running a second instance of the same device
type).

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

To go from a mock to a real device (e.g. DewesoftX for the DAQ via
DCOM automation or the DSRemote Python library), implement the same
`HardwareBackend` interface against the real API as a new file
alongside the mock (e.g. `mock_daq/dewesoft_backend.py`, leaving
`mock_daq/mock_backend.py` in place for local development without
hardware), and point that device's `main.py` at the new class and drop
its "SIMULATED DEVICE" warning. Note DewesoftX's Windows-only
DCOM/DSRemote control means that particular backend (and therefore its
process) will need to run on the Windows test stand PC, not in a
general Linux environment - other device types aren't necessarily
subject to that constraint.

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

`demo_end_to_end.py` was run in development: channel list, setup load,
start/stop acquisition, and status all round-trip correctly, and
telemetry frames stream at the configured rate (~50 Hz in the mock).
Calling `start_acquisition` while already acquiring, and sending an
unknown command, both return a clean `CommandClientError` instead of
crashing the server.

`demo_power_supply.py` was also run in development, on its own ports
alongside the DAQ process: connect, set_output, and enable_output all
round-trip correctly, telemetry reads back near-zero with the output
disabled and near-setpoint with it enabled - all through the exact
same `CommandServer`/`TelemetryServer` code as the DAQ, confirming the
generalized interface actually holds for a second, unrelated device
type.

`demo_dut.py` was also run in development, on its own ports alongside
the DAQ and power supply processes: set_gains and set_position_input
round-trip correctly, and telemetry shows the commanded position step
settling smoothly through the first-order model - confirming the
generalized interface holds for a third, unrelated device type that's
a control loop (command channels feed back into read channels over
time) rather than a simple readback.
