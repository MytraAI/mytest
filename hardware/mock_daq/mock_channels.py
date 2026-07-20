"""MockDaqBackend's declared telemetry/command surface - the source of
truth for anything that wants to check this backend's actual channels
(the same role hardware/mock_dut/mock_channels.py plays for the DUT).

Not yet wired up to any verify_channels()/verify_actions() call - no
test case watches the DAQ's own telemetry today (see the architecture
doc's open decisions) - but declared here so that's a one-line addition
whenever a test case actually needs it.

Kept separate from mock_backend.py so a channel/action list can be
read (or imported elsewhere) without pulling in the backend's asyncio
implementation.
"""
from __future__ import annotations

TELEMETRY_CHANNELS = [
    "chan_temp",  # arbitrary synthetic units - no stated real-world unit (sine wave centered ~20)
    "chan_pressure",  # arbitrary synthetic units - no stated real-world unit (sine wave centered ~100)
    "chan_vibration",  # arbitrary synthetic units - Gaussian noise centered at 0
]

COMMAND_CHANNELS = [
    "get_channel_list",  # returns channel names - no unit
    "load_setup",  # setup_name: str - no unit
    "start_acquisition",  # test_id: str - no unit
    "stop_acquisition",  # no params
    "set_digital_output",  # boolean on/off - no unit
    "trigger_event",  # name: str - no unit
]
