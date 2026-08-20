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
ergonomics, a thin command-client subclass) is needed. Five device
folders exist today: `mock_daq`, `mock_power_supply`, and `mock_dut`
each hold a single simulated backend (`MockDutBackend` being the DUT
itself - a first-order position/velocity/current servo approximation,
proving the generic framework fits a control loop, not just
readbacks). `odrive/` is the first real (non-simulated) device: it
holds both `OdriveBackend` (real, talks to an actual ODrive motor
controller over USB via the official `odrive` package) and
`MockOdriveBackend` (simulated, for local development without hardware
attached) side by side - see that folder's own section below.
`n6974a/` is the third real device: a Keysight N6974A Advanced Power
System (80 V, 25 A, 2 kW, two-quadrant), also over ethernet, spoken to
as SCPI on Keysight's standard socket port 5025. Like `cpx400dp/` it has
no mock backend - see its section below, and note how differently it is
built despite both being ethernet SCPI supplies: it batches an entire
telemetry frame into one compound message and keeps no settings cache at
all.

`cpx400dp/` is the second real device and the first reached over
**ethernet** rather than a local bus: a TTi CPX400DP dual-output bench
power supply, spoken to as line-oriented SCPI over a raw TCP socket.
Unlike `odrive/` it has **no** mock backend - see its section below for
why. Each device runs as its own process, on its own ports - see
"Running it" below.

## Layout

`hardware/` is a sibling of `testcases/` and `telemetry_engine/`
under the repo root (`Mytest/`), alongside `AI/` (the architecture
doc). Per the architecture doc, these are separate OS processes that
only talk over the command/telemetry wire protocol, never via direct
imports of each other's internals.

```
Mytest/
  protocol/
    wire.py                     shared wire schemas (CommandRequest/Reply, TelemetryFrame, RunStateFrame) + endpoint constants
    verdict.py                  per-test verdict record shared by the testcase process and the telemetry engine
  hardware/
    backend.py                  abstract HardwareBackend interface (universal core + execute())
    command_server.py          ZeroMQ REP server, dispatches commands to the backend
    telemetry_server.py         ZeroMQ PUB server, forwards backend.stream_samples()
    runner.py                   shared process wiring (connect, run servers, signal-stop, disconnect) - device-agnostic
    driver_logging.py           shared --log-file setup: console at INFO, a detailed DEBUG file beside that device's telemetry
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
      odrive_channels.py           TELEMETRY_CHANNELS/COMMAND_CHANNELS - curated (not exhaustive): 99 telemetry + 71 command channels a test author would realistically use or need for diagnosis, every one verified to exist on the real board at connect() - see its docstring for what was deliberately cut
      odrive_errors.py             decodes active_errors/disarm_reason/last_drv_fault and the state enums into words, from odrive.enums
      odrive_command_client.py     OdriveCommandClient - named sugar for ODrive actions, layered on the generic CommandClient
      main.py                      entry point: real backend by default, --mock to run MockOdriveBackend instead, calls runner.run()
    cpx400dp/
      cpx400dp_backend.py          Cpx400dpBackend - REAL, a TTi CPX400DP dual-output bench supply over ethernet (raw socket, port 9221). No mock counterpart.
      transport.py                 TtiSocketTransport - the line protocol alone (LF out, CRLF back), serialized on one lock; the seam tests substitute
      cpx400dp_channels.py         TELEMETRY_CHANNELS/COMMAND_CHANNELS - 41 telemetry channels in four acquisition tiers, and all 66 documented commands
      rails.py                     the supply's power envelope, and a Rail describing one of its outputs - shared by the stands that use this model
      cpx400dp_command_client.py   Cpx400dpCommandClient - named sugar per command channel; 10 s default timeout, since `with verify` blocks 5 s
      main.py                      entry point: always real hardware, --host/--port/--max-voltage/--max-current/--interface-lock, calls runner.run()
    n6974a/
      n6974a_backend.py            N6974aBackend - REAL, a Keysight N6974A Advanced Power System over ethernet (raw SCPI socket, port 5025). No mock counterpart.
      transport.py                 KeysightSocketTransport - the line protocol alone (LF both ways), compound multi-query messages, count-checked, with reopen-on-desync
      n6974a_channels.py           TELEMETRY_CHANNELS/COMMAND_CHANNELS - 121 telemetry channels in one message per frame, and 234 commands
      n6974a_command_client.py     N6974aCommandClient - named sugar per command channel; 25 s default timeout, since `*TST?` blocks 5.2 s
      main.py                      entry point: always real hardware, --host/--port and a required --dissipators, calls runner.run()
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
and `tcp://127.0.0.1:5556` (telemetry) - see `protocol/wire.py`.

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
python -m hardware.cpx400dp.main             # CPX400DP, REAL hardware over ethernet
python -m hardware.n6974a.main --dissipators 1   # N6974A APS, REAL hardware over ethernet
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

## `n6974a/` - the Keysight N6974A Advanced Power System, over ethernet

A Keysight N6974A: 80 V, 25 A, 2 kW, single output, and **two-quadrant**
- it sinks as well as sources. Driven as SCPI over a raw TCP socket on
port 5025. Ports default to `tcp://127.0.0.1:5610`/`5611`.

**No mock backend, for the same reason as the CPX400DP** but against a
different risk. Here the risk is *message building*: a telemetry frame is
60 queries batched into one compound SCPI message, and three status
registers are decoded bit by bit. A backend-level mock would replace
exactly that. So the seam is the transport, and
`tests/test_n6974a.py` substitutes a fake instrument whose replies were
recorded from the real unit *by asking the driver's own query tables*, so
the fixture cannot quietly agree with the driver instead of the
instrument.

**One message per frame, and no cached tier.** Semicolons batch commands
into one message, and the whole reply comes back as one line of
semicolon-separated values, so every one of the 121 declared channels is
re-read on every frame for a single round trip. That is the opposite of
the CPX400DP's four tiers, and it is affordable for a specific reason:
the settings queries cost ~1.4 ms *combined*, while the measurement
inside the frame costs ~21 ms on its own. Since nothing is cached, this
driver makes no assumption that it is the only thing touching the
instrument - a front-panel knob or a second socket client shows up in the
next frame.

The lever on frame rate is therefore not the poll interval but
`set_nplc`: a MEASure is an acquisition over `SENSe:SWEep:NPLCycles`
power line cycles. At the 1 PLC default a frame is ~32 ms and the driver
publishes at ~19 Hz; at 0.1 PLC a frame is ~4 ms, at the cost of the
line-frequency noise rejection an integral number of cycles buys.

**Measure once, fetch the rest.** `MEASure:VOLTage?` performs the
acquisition and `FETCh:CURRent?`/`FETCh:POWer?` read the *same* one. That
halves the frame cost and, more importantly, makes V, I and P
simultaneous - power computed from a row agrees with the power reported
in it.

**Condition and event registers, so no software latch is needed.** Each
of the three status registers is read twice per frame: the condition
register for what is true now, the event register for what became true
since the last frame. The instrument's positive-transition filter passes
every bit (`STATus:QUEStionable:PTRansition?` reads 16383), so a
protection that trips and clears between two frames still lands in that
frame's `*_event` channel. Where the CPX400DP driver needs a sticky
software latch, this one gets the same guarantee from the hardware. Two
event bits are self-inflicted: `measure_active_event` and
`waiting_for_measure_trigger_event` fire every frame because the frame's
own MEASure initiates the measurement system.

**`--dissipators` is required, and verified.** Each Keysight N7909A power
dissipator adds 1 kW of sinking capability; a 2 kW model needs two to
sink 100% of its rating, and with one it sinks 50%. This decides how hard
the supply may discharge whatever is attached, so it is declared rather
than assumed - and checked, because the instrument offers no query for
it. Per the guide the only indication is the magnitude of
`CURRent:LIMit:NEGative? MIN`, which reads 10%, 50% or 100% of the maximum
programmable current. A declared count the instrument contradicts refuses
to start: an N7909A is only recognised at **power-on**, so one cabled to a
running supply reads as absent and does nothing, which is exactly the
case a trusted declaration would paper over.

**Setpoints are clamped, not refused.** A commanded voltage or current
beyond what the hardware allows is applied *at* the limit, with a warning
naming asked-versus-applied, the applied value returned to the caller,
and a sticky `clamped_*` telemetry channel keeping it visible for the rest
of the run. Bounds are read from the instrument (`VOLTage? MAX` and
friends - it allows 2% over nameplate, so 81.6 V and 25.5 A), with the
negative bound additionally held to what the declared dissipator count
permits. Only quantities that carry energy are clamped; a watchdog delay
or comparator level out of range raises with the instrument's own error
text instead, since silently altering those would hide a mistake rather
than contain a hazard.

**Behaviours of this instrument that shape the driver**, all measured:

- **A malformed command discards the whole message.** `OUTP?;:BOGUS?;:FUNC?`
  answers *nothing at all*, and the `OUTP?` reply is left in the output
  queue **without its terminating newline**, so the next query's answer is
  appended to it (a following `SYST:ERR?` was read back as
  `0-113,"Undefined header"`). Every read after that point is answered by
  the wrong query. So a timeout or a wrong value count is treated as a
  desynchronised link, not a slow one, and the transport closes and
  reopens the socket - the only reliable resynchronisation, ~400 ms, and
  it also gets a clean error queue since the queue is per-I/O-session.
  `link_resynchronisations` is published as telemetry, because a link
  recovering repeatedly is a fault even when each recovery works.
- **An unavailable command is answered with silence**, naming itself only
  in the error queue (`+302,"Option not installed"`,
  `+310,"...not supported by this model"`, `-113,"Undefined header"`).
  Combined with the above, one absent channel inside the frame would
  desync the link on every frame for a whole run - which is why connect()
  probes all 60 declared queries **individually**, and reports each
  failure with the instrument's own explanation.
- **A setting readback in the same message as its write answers one step
  stale.** `VOLT 2.5;:VOLT?` returns the *previous* setpoint,
  deterministically, for the source-programming parameters (`VOLTage`,
  `CURRent:LIMit`, `CURRent:LIMit:NEGative`), while protection parameters
  answer freshly. Neither `*WAI` nor `*OPC?` between them changes it. So a
  write is two messages inside one transaction - command with its
  `SYSTem:ERRor?` check, then the readback - for ~0.9 ms instead of
  ~0.5 ms. Worth it: the readback is what tells a caller a clamp
  happened, so a stale one is worse than none.
- **A common command takes no root colon.** `VOLT 1;:*WAI` is
  `-113,"Undefined header"` and, per the first point, costs the entire
  message; `VOLT 1;*WAI` is correct. `join_message` picks the separator.
- **An error message can contain a semicolon** -
  `+310,"The command is not supported by this model;"` - so a reply is
  split at most `expected - 1` times and any query whose answer may
  contain one goes last in the message.
- **A parameter belonging to the other priority mode may be silently
  ignored.** `CURRent` written in voltage priority is accepted with no
  error and does not take effect, where `VOLTage:LIMit` at least answers
  `+315,"Settings conflict error"`. Rather than enumerate which parameter
  belongs to which mode, every numeric write compares the readback
  against what was commanded and warns when they differ.
- **Switching priority mode turns the output off and reverts every output
  setting** to its reset value, so `set_priority_mode` is refused while
  the output is on.
- **`*TST?` runs a real self-test and takes 5.2 s**, longer than the
  ordinary read ceiling - it and `*OPC?`/`*WAI` get their own longer
  timeout, rather than raising the ceiling for everything and turning an
  unimplemented mnemonic's silence into a long stall.
- **`protection_mode` LOWZ** (the reset default) actively sinks the load's
  energy for 2 ms at up to 120% of the current rating while shutting down;
  HIGHZ disconnects without sinking. HIGHZ is the one for a load that
  stores energy. The instrument reverts to LOWZ by itself on a priority
  mode change.
- **The three sense-lead faults are not equivalent.** An OPEN sense lead
  raises the sense fault (SF, questionable bit 13) within ~50 us; the
  instrument falls back to local sensing and **keeps regulating**, with the
  output terminals ~1% above the programmed value, and clears by itself when
  the leads are reconnected. That is an accuracy fault, not a shutdown - and
  1% of the programmed value is 0.8 V at 80 V, wider than most tolerances a
  test would assert. Whether the measured `voltage` channel shows that error is
  undocumented and unverified here, so `sense_fault` is the evidence, not the
  reading. A
  SHORTED sense lead trips over-voltage protection and disables the output; a
  REVERSED one trips negative over-voltage protection. Neither of the latter
  two is programmable, and neither is detectable until the output is enabled,
  so mis-wiring is only found by briefly energizing the load. A test that
  cannot tolerate the 1% should promote the fault to a shutdown: `OpenSense`
  is a signal-expression input, so routing it to the user protection makes it
  latching (verified on the instrument).
- **The I/O watchdog** (`OUTPut:PROTection:WDOG`) shuts the output down
  when SCPI traffic stops, which is a genuine dead-man's switch for a
  driver that polls continuously - a SIGKILLed driver leaves the
  instrument to de-energize itself. This driver exposes it and never arms
  it: that is a test's decision. Note traffic on *any* remote interface
  resets the timer, so a browser on the instrument's web page holds it
  off.

**What is deliberately not declared.** This unit reports `*OPT?` as `0`,
so the option-gated subsystems are absent and are not declared: output
lists and Arb (Option 303), the digitizer's programmable sample rate,
external datalogging and array readback (302), the low current range and
seamless ranging (301), the black box recorder, and the
disconnect/polarity-reverse relays (760/761). Also unavailable on this
model regardless: `VOLTage:BWIDth`, `SENSe:WINDow` and
`MEASure:POWer:MAXimum?`/`MINimum?` - though the voltage and current
MAX/MIN/HIGH/LOW measurements do work. And three things the instrument
supports but this driver will not expose: the `CALibrate` subsystem
(which can degrade accuracy, and which the instrument itself guards with
a password), `SYSTem:SECurity:IMMediate` (erases all user memory and
reboots), and `HCOPy:SDUMp:DATA?` (a binary image block a line-oriented
transport cannot carry).

## `cpx400dp/` - the CPX400DP power supply, over ethernet

A TTi CPX400DP dual-output bench supply, driven as line-oriented SCPI
over a raw TCP socket on port 9221. Ports default to
`tcp://127.0.0.1:5590`/`5591`.

**No mock backend, on purpose.** The ODrive keeps a real and a
simulated backend side by side because its risk was wrong attribute
paths, which a mock can mirror faithfully. This driver's risk is
different: it is **response parsing**. The instrument replies to
`OVP1?` with `VP1 66.00` and to `OCP1?` with `CP1 22.00` - not the
mnemonics that were sent - measured readbacks carry unit suffixes
(`-0.005V`), and `OP1?` answers a bare integer. A backend-level mock
would replace exactly the code most likely to be wrong. So the seam
for testing is the *transport*, not the backend: `tests/test_cpx400dp.py`
substitutes a fake instrument whose replies are byte-exact transcripts
from the real device, and everything above it is the real
implementation. The consequence is that `main.py` has no `--mock` flag
and needs a real supply.

**Four telemetry tiers.** All 41 declared channels appear in every
frame, but they are acquired four different ways, and the differences
are load-bearing rather than cosmetic:

1. **State** (4 queries/frame) - output on/off and the limit status
   register. Instrument state, which changes at the speed of the events
   causing it. Polling this fast is what caught an OVP trip inside a
   single frame period.
2. **Metered** (4 queries, at 5 Hz) - measured voltage and current.
   These are capped by the instrument itself: its specification gives
   a **4 Hz meter reading rate**, with 10 mV / 10 mA resolution and
   0.1% / 0.3% of reading ±2 digits accuracy. Polling them per frame
   re-read a register refreshed four times a second - visible directly
   in our capture, where a decaying output read back as a staircase
   holding each value across 6-10 consecutive polls. They are now read
   at 5 Hz (a deliberate margin over 4 Hz, so the poll cannot sit just
   behind the instrument's unsynchronised update) and held in between.
   A repeated value in consecutive recorded rows may therefore be a
   held reading rather than a re-measured one - as it already was when
   the instrument itself returned the duplicates.
3. **Cached** - setpoints, OVP/OCP, step sizes, tracking config,
   address. Settings that only this driver writes, read once at connect
   and refreshed after a command that changes them, then carried in
   every frame from memory at no round-trip cost. This rests on the
   assumption that nobody turns the front-panel knobs mid-run.
4. **Not telemetry** - the read-and-clear error registers (`EER?`,
   `QER?`, `*ESR?`), consumed by the driver's own post-write check.
   Streaming them would race that check for a single-copy value. They
   are reachable as explicit actions instead.

Note the 4 Hz ceiling is a *reporting* rate, not a control or
protection one - the supply reacts far faster than it reports. OVP is
specified at ~1 ms and tripped inside one 19 ms frame; OCP is
"measure-and-compare implemented in firmware" at ~500 ms, about two
meter updates, consistent with that comparison being fed by the same
measurement path.

**Every write is checked.** The instrument accepts writes it then
silently discards: `V2 999` leaves the setpoint untouched, sends
nothing back, and reports itself only as `EER?` = 100. Both registers
are read after every command because they catch different failures - a
range error sets `EER?` and `*ESR?` bit 4, an unrecognised mnemonic
sets only `*ESR?` bit 5.

**Connect and disconnect are passive.** `connect()` opens the link,
confirms the model in `*IDN?`, clears its own error registers, verifies
every declared channel answers, reads the cached tier, and logs the
output state it inherited. It does not enable an output, disable one it
finds on, or set protection levels. `disconnect()` closes the link and
releases an interface lock if it took one; it does **not** switch
outputs off. Energized-state safety that does not depend on this
process belongs to the instrument's own OVP/OCP, which this driver
exposes but never asserts.

**Optional guards, both off by default.** `--max-voltage`/`--max-current`
are a driver-side ceiling: they change nothing on the instrument, they
make this process refuse to *command* a setpoint above them. That
catches the failure the instrument cannot - a value well inside its own
60 V range and fatal to the load. `--interface-lock` takes `IFLOCK` at
connect so the web page and VXI-11 cannot change settings mid-run.

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
   telemetry_endpoint, device=DEVICE_<NAME>, sample_interval_s=...)` -
   copy `mock_power_supply/main.py` as a template. Add the
   `DEVICE_<NAME>` constant to `protocol/wire.py` alongside the others:
   it's stamped onto every frame this driver publishes, becomes the
   directory name this device's telemetry is stored under, and is what
   keeps two devices' identically-named channels apart.
   `sample_interval_s` is the backend's own `SAMPLE_INTERVAL_S`, used to
   size the publisher's high-water mark in seconds of buffer.
3. Optionally add a thin `CommandClient` subclass in that same device
   folder (e.g. `mock_<device>/mock_<device>_command_client.py`) with
   named methods that call `self.execute(...)` - pure convenience for
   callers, not required. It imports the generic `CommandClient` from
   `clients/command_client.py` - only the truly device-agnostic
   client/telemetry code lives in `clients/` itself.
4. Nothing in `runner.py`, `command_server.py`, `telemetry_server.py`,
   or `TelemetryClient` needs to change - they only depend on the
   universal core and the generic wire protocol.
5. Don't re-implement what `HardwareBackend` already provides:
   `_require_connected()` (override the `is_connected` property instead if
   your connection state is a handle rather than the `_connected` flag) and
   `to_jsonable()` for coercing device values onto the JSON wire. Declare
   `device` and `sample_interval_s` as class attributes - `runner.run()`
   reads both off the backend, so no entry point passes them.

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
process (all declared command and telemetry channels), a
representative sample of commands round-tripped across every
subsystem (control mode/state, a config setter, a motor-config setter,
a trap_traj setter, a board-level setter, a two-arg method), and a
telemetry frame containing every declared telemetry channel round-tripped through
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
surface, then was pruned back to the 99/71 kept today - removing
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
- `_read_all_channels()`'s `except AttributeError: return None` was
  removed entirely, and absence is now a **setup-time error**.
  Substituting `None` meant a declared-but-absent channel still
  appeared as a key in every frame, so `verify_channels()` saw nothing
  missing and the run looked healthy while recording an empty column
  for its whole duration - and a `Bound` against such a channel raised
  `TypeError` inside the live evaluation thread, killing it and leaving
  the test unsupervised. `connect()` now probes every declared path
  once and raises `MissingChannelError` naming each one that doesn't
  resolve (see `_verify_declared_channels_exist`). Confirmed against a
  real ODrive Pro: 11 telemetry + 3 command channels were absent and
  have been pruned, after which all 168 declared channels verify and a
  live frame contains zero `None` values. Anything that isn't
  `AttributeError` still propagates uncaught, which is what lets the
  `runner.py` fix above catch a real disconnect.
- Added logging (`connect`/`disconnect`/missing-channel warnings) -
  previously silent - and `hardware/demos/demo_odrive.py`, matching every
  other device's `demo_<device>.py` convention (always `--mock`); this
  session's other verification had all been one-off scratch scripts
  that didn't persist in the repo.

`hardware.cpx400dp.main` was run end-to-end against the real
instrument - Thurlby Thandar CPX400DP, serial 599542, firmware
2.03-4.12, on a link-local ethernet link. The driver process launched,
`Cpx400dpCommandClient.verify_actions()` passed for all 66 declared
command channels and `TelemetryClient.verify_channels()` for all 41
telemetry channels, live frames carried exactly the 41 declared keys
with every tier populated, a checked write round-tripped
(`set_voltage(1, 2.5)` -> `setpoint_voltage_1` = 2.5), the driver-side
ceiling refused 48 V before anything reached the wire, and an
instrument-refused write (`OVP1 999`) surfaced as `EER 100 - range
error` instead of silently succeeding. Teardown exited 0.

The command set and every response format were derived from the
instrument itself and its manual, not assumed: a full read-only query
sweep recorded byte-exact replies (which is where `VP1`/`CP1` and the
unit suffixes came from), and `tests/test_cpx400dp.py`'s fake replies
are those transcripts. `RANGE<n>?`, `SENSE<n>?`, `DAMPING<n>?` and
`EXR?` were confirmed **absent** on this firmware - they belong to
other TTi models - and are not declared. Timing was measured over 200
back-to-back frames, before and after the meter tier was split out of
the per-frame poll: median frame cost fell from 18.9 ms to **9.4 ms**,
back-to-back throughput rose from 29.5 Hz to **63.2 Hz**, and the
published rate at `sample_interval_s = 0.02` from ~19 Hz to ~28 Hz. The
meters lost nothing by it - held staleness stayed bounded at the
intended 200 ms (max observed 199 ms, mean 87 ms), against an
instrument that only refreshes them every 250 ms anyway. The frame
period is still not steady: the worst frame in both runs was ~250 ms,
an intermittent stall in the instrument that no tuning here removes, so
a consumer should read frame `t` rather than assume a period.

Behaviour confirmed on hardware that the manual gets wrong or omits:
- `LSR<n>?` bit 0 is a **level, not an edge**. The manual says "Set
  when output *enters* voltage limit"; measured, the register clears on
  read and is set again on the very next read for as long as the output
  regulates. Hence `in_cv_<n>` rather than `entered_cv_<n>`.
- The error registers **outlive the socket**. A deliberate `V2 999` on
  one connection was still readable as `EER?` = 100 and `*ESR?` = 16
  from a *new* connection - so a fresh driver would otherwise attribute
  a dead process's failure to its own first write. `connect()` issues
  `*CLS` for exactly this reason.
- An interface lock also outlives its socket, but is **recoverable**: a
  reconnecting client inherits ownership (`IFLOCK?` = 1) and can
  release it, so a crashed driver cannot strand the instrument.
- Only **one** raw-socket connection is accepted; a second is refused
  while the first is open. The manual's "two TCP socket interfaces"
  evidently counts VXI-11 (port 111 is open). This is why `connect()`
  is idempotent - `runner.run()` connects at process start and every
  client then sends `connect` over the wire, and a second `open()`
  would fail on a link that is working.
- A `with verify` command blocks the link for the full documented 5 s
  (measured 5.01 s) when the output cannot reach the target, which is
  why the transport's read timeout is 8 s and the command client's
  default is 10 s.
- The output **ramps** (4.515 V a quarter-second after enabling at
  5 V), switching off does **not** mean zero volts (2.748 V still
  present immediately after, decaying into an open circuit), and
  `current_<n>` reads a nonzero offset at zero (0.019 A on output 1,
  0.053 A on output 2, with nothing connected).

A second round of hardware tests then exercised the trip and
regulation paths that the first could not, three of them into a
deliberate short across output 2 at a 0.1 A limit:

- **Constant current** (`in_cc_<n>`): shorted, the output regulated at
  its current limit with `in_cv` false and `in_cc` true, held as a
  level across every poll.
- **OVP trip** (`tripped_ov_<n>`): with OVP below the setpoint,
  enabling the output tripped it **within one frame period** - the
  first poll after `OP2 1` already showed `output_enabled_2` false,
  with the terminal voltage decaying 3.93 -> 1.32 -> 0.34 -> 0 V. This
  is the case that justifies streaming `output_enabled` rather than
  caching it: the instrument switched its own output off with no
  command from us.
- **OCP trip** (`tripped_oc_<n>`): lowering OCP below the current being
  drawn tripped it the same way, `LSR` reading 10 (in CC *and*
  over-current) before settling to 8.
- **Neither trip set bit 6** (`tripped_latching_<n>`), so both are soft
  trips needing no front-panel reset. Bit 6 and `in_power_limit_<n>`
  remain the only unverified channels; nothing reachable from software
  provoked either.
- **Error 104** decoded correctly from real hardware: changing `CONFIG`
  with output 2 on was refused with "command not valid while the output
  is on". Voltage-tracking mode works on the setpoint - `V1` at 4.0 V
  drove `V2` to 2.0 V at a 50% ratio.
- **SIGKILL recovery**: a driver killed with `-9` while streaming and
  holding the interface lock left both the socket and the lock behind.
  A fresh driver connected cleanly, **inherited** the lock (`IFLOCK?` =
  1), streamed again, and released it on clean teardown, after which a
  third driver saw `IFLOCK?` = 0. The crash story is measured, not
  argued.

**`TRIPRST` does not clear a trip**, despite being documented as
"attempt to clear all trip conditions" - it had no observable effect in
any case tried, and the two trips need *different* recovery. An OVP
trip cleared as soon as `ovp_<n>` was raised back above the voltage
setpoint, with no `TRIPRST` involved. An OCP trip ignored both a raised
`ocp_<n>` and `TRIPRST`, and cleared only on an explicit
`enable_output_<n>(False)` - even though the trip had already switched
the output off. A recovery step should therefore remove the cause,
explicitly command the output off, and re-enable; a step that calls
`trip_reset` and waits for the trip to clear will wait forever.

That result also settles the shape of the limit-status channels: every
bit is a **level**, not an event, verified for CV, CC, OVP and OCP. The
`limit_status_latched_<n>` channel is still needed, and for a sharper
reason than "trips latch" - since nothing latches in the instrument, a
condition beginning and ending between two frames leaves no trace at
all unless the driver keeps one.

One measurement caveat worth knowing before writing a Bound:
`current_<n>` is not accurate to better than a few tens of milliamps at
the bottom of its range, and not as a subtractable offset. It read
0.019 A (output 1) and 0.053 A (output 2) with nothing connected and
the output off, and 0.115 A while genuinely regulating at a 0.100 A
limit - consistent with a 20 A-class instrument's readback resolution
rather than a calibration error.
