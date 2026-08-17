"""What one of this supply's outputs can deliver, and how a stand describes what
it has wired to one.

Lives with the driver because both are facts about the CPX400DP rather than about
any stand using it. The per-stand values - which output, at what voltage, feeding
what - live on the testbed that owns the stand.

The supply is "PowerFlex": 60 V and 20 A are both reachable but not
simultaneously, the published envelope points being 60 V/7 A, 42 V/10 A and
20 V/20 A. Above ~21 V that is a constant 420 W; below it the 20 A ceiling binds
instead.
"""
from __future__ import annotations

from dataclasses import dataclass

POWER_ENVELOPE_W = 420.0
"""Maximum power per output."""

MAX_CURRENT_A = 20.0
MAX_VOLTAGE_V = 60.0


def deliverable_current_a(voltage: float) -> float:
    """The most current one output can source at `voltage`.

    Used to check a configured current limit against physics before a test
    trusts it. A limit above this value is not a limit at all: the output
    reaches the power envelope first and goes *unregulated* - the voltage sags
    rather than the current being held - so the protection a current limit
    exists to provide never engages. A rail configured that way has to be
    watched through `in_power_limit_<n>` instead of `current_<n>`."""
    if voltage <= 0:
        return MAX_CURRENT_A
    return min(MAX_CURRENT_A, POWER_ENVELOPE_W / voltage)


@dataclass(frozen=True)
class Rail:
    """One supply output, and what a stand has wired to it."""

    name: str
    output: int
    """Which output: 1 is the Master (left-hand), 2 the Slave."""
    voltage_v: float
    current_limit_a: float

    @property
    def power_w(self) -> float:
        return self.voltage_v * self.current_limit_a

    @property
    def is_within_envelope(self) -> bool:
        """Whether the configured current limit can actually be reached at this
        rail's voltage. False means the limit will never engage - see
        `deliverable_current_a` for what happens instead."""
        return self.current_limit_a <= deliverable_current_a(self.voltage_v)
