"""What the TC DAQ publishes, and what it accepts.

Eight thermocouple channels, streamed as one CSV line per sample. There are no
command channels at all: the device takes no commands, so there is nothing for a
test to set - not the units, not the thermocouple type, not the sample rate.

THE STREAM IS ANONYMOUS. The line carries eight values and nothing else - no
header, no channel labels, no units, no device identity, no timestamp. Every
name below is therefore positional: `temperature_1_c` is the first field of the
line, and which physical thermocouple that is depends on how the harness is
wired. A testbed that cares which channel is which documents it there; this
driver cannot know.

CELSIUS IS AN ASSUMPTION. With five channels reading 21-22.5 degrees in a room
at that temperature, Celsius is the only sensible reading of the numbers - but
there is no command to ask the device what it is configured for, so nothing here
confirms it. A device switched to Fahrenheit would stream plausible numbers
under these names and nothing would notice.

FAULT IS A VALUE, NOT A FAILURE. The device writes `FAULT` in place of a number
for a channel it cannot read; with nothing plugged in, that is an open
thermocouple, which is what three channels reported when this was written. Other
causes - out of range, a broken junction, an ADC fault - would look identical
from here. Two channels carry it:

- `temperature_<n>_c` is None for that sample. None rather than 0.0 or a
  retained previous value, both of which are a lie that reads as a real
  temperature. It lands as an empty cell in the wide CSV, which offline replay
  reconstructs as an absent channel, so a Bound on it returns no result instead
  of passing on a fabricated number.
- `fault_<n>` is True. Carried explicitly because an empty cell alone is
  ambiguous: a dropped frame looks the same. This is the channel to bound.
"""
from __future__ import annotations

from typing import List, Tuple

CHANNEL_COUNT = 8
"""Thermocouple inputs, and so fields per line. Checked against what actually
arrives at connect(), since a short line means either a wrong baud rate or a
different device."""

FAULT_TOKEN = "FAULT"
"""What the device writes instead of a number for a channel it cannot read."""


def _per_channel(prefix: str, suffix: str = "") -> List[str]:
    return [f"{prefix}_{n}{suffix}" for n in range(1, CHANNEL_COUNT + 1)]


TEMPERATURE_CHANNELS: Tuple[str, ...] = tuple(_per_channel("temperature", "_c"))
FAULT_CHANNELS: Tuple[str, ...] = tuple(_per_channel("fault"))

TELEMETRY_CHANNELS: Tuple[str, ...] = (
    *TEMPERATURE_CHANNELS,  # float or None - None means this sample read FAULT
    *FAULT_CHANNELS,        # bool - the device could not read this channel
    "fault_count",          # int - how many of the eight are faulted this sample
    "malformed_lines",       # int - cumulative, see the backend
)
"""Every channel a frame carries.

`fault_count` is derived rather than left to whoever reads the record: "no
thermocouple may be open" is one bound against it, instead of eight identical
bounds that each have to be remembered when a channel is added.

`malformed_lines` counts lines this driver could not parse, cumulatively over
the run. A line the device never sent is not distinguishable from one this
driver read wrongly, so the count going up is the visible symptom of a baud
mismatch, a marginal cable, or a device firmware that changed shape - and
putting it in the telemetry means the stored run shows it, not just whatever
scrolled past in a terminal."""

COMMAND_CHANNELS: Tuple[str, ...] = ()
"""Deliberately empty: this device accepts no commands. `execute()` refuses
everything and `list_actions()` answers with nothing, so a caller that expects
an action fails loudly at verify_actions() rather than having a write silently
go nowhere."""
