"""Stand configuration for the zdrive testbed: which instrument is where, and
what each supply rail is for.

A Python module of constants rather than a TOML/YAML file, matching how every
other declaration in this codebase is written (protocol/wire.py, each device's
`*_channels.py`): no parser to maintain, a typo fails at import rather than at
connect, and the docstrings can carry the *why*, which is most of the value
here. This fills the `config/` directory the testbeds have always reserved and
never used.

What belongs here is what changes when the physical stand changes - an
instrument's address, which output feeds which rail, what voltage that rail
runs at. What does not belong here is anything a test decides.
"""
from __future__ import annotations

from dataclasses import dataclass

CPX400DP_HOST = "169.254.229.133"
"""The bench supply on this stand.

NOT a stable address. The instrument reports `NETCONFIG?` = DHCP, but the
segment it sits on has no DHCP server, so it self-assigned this link-local
address; it moves if a DHCP server appears or on an address collision. Observed
happening: after the USB-ethernet adapter was re-plugged, this host's own
link-local address changed and a TCP connection failed with "no route to host"
until the ARP entry refreshed, even though the link was up and the instrument
was answering pings.

The driver confirms the model in `*IDN?` at connect, so a *stale* address fails
loudly rather than streaming another instrument's readings as `cpx400dp`. What
it cannot do is find the instrument again.

CPX400DP_MDNS_HOST below is the better identity, and choosing between them is a
deployment decision rather than a default worth changing silently."""

CPX400DP_MDNS_HOST = "t599542.local"
"""The same instrument by mDNS name, discovered from `route get`: the supply
advertises itself as `t<serial number>.local`, and 599542 is the serial in its
`*IDN?` reply. Verified to resolve and answer `*IDN?` identically.

This is the identity-based address the ODrive gets from
`find_any(serial_number=...)` - it follows the instrument wherever its IP moves,
which the raw address cannot. The reason it is not simply the default: `.local`
resolution needs an mDNS responder, which macOS has built in but a Windows or
CentOS test stand may not (Bonjour, or avahi plus nss-mdns). So the two fail in
different directions - the name breaks where mDNS is absent, the address breaks
when it moves - and which is right depends on the stand. Pass whichever suits as
`ZdriveTestbed(cpx400dp_host=...)`.

The real fix remains a static address set through the instrument's web
interface, which removes both failure modes."""

# --- The instrument's own limits, for checking a rail is even possible -------

POWER_ENVELOPE_W = 420.0
"""Maximum power per output. The CPX400DP is a PowerFlex supply: 60 V and 20 A
are both reachable but not together (60 V/7 A, 42 V/10 A, 20 V/20 A). Above
~21 V the envelope is this constant power."""

MAX_CURRENT_A = 20.0
MAX_VOLTAGE_V = 60.0


def deliverable_current_a(voltage: float) -> float:
    """The most current this supply can actually source at `voltage`.

    Used to check a rail's configured current limit against physics before a
    test trusts it. A limit above this value is not a limit at all: the output
    reaches the power envelope first and goes *unregulated* - the voltage sags
    rather than the current being held - so the protection a current limit is
    there to provide never engages. See ZdriveTestbed.start(), which warns when
    a rail is configured this way."""
    if voltage <= 0:
        return MAX_CURRENT_A
    return min(MAX_CURRENT_A, POWER_ENVELOPE_W / voltage)


@dataclass(frozen=True)
class Rail:
    """One supply output, and what it feeds on this stand."""

    name: str
    output: int
    """Which CPX400DP output: 1 is the Master (left-hand), 2 the Slave."""
    voltage_v: float
    current_limit_a: float

    @property
    def power_w(self) -> float:
        return self.voltage_v * self.current_limit_a

    @property
    def is_within_envelope(self) -> bool:
        """Whether the configured current limit can actually be reached at this
        rail's voltage."""
        return self.current_limit_a <= deliverable_current_a(self.voltage_v)


MOTOR_BUS = Rail(name="zdrive motor bus", output=2, voltage_v=48.0, current_limit_a=16.0)
"""The ODrive's DC bus. 48 V on output 2.

The 16 A limit is deliberately above what this supply can deliver at 48 V - the
420 W envelope caps output 2 at 8.75 A there - so it will never engage as a
current limit. What happens instead, if the motor tries to draw more, is that
the output goes unregulated and the bus voltage sags; the driver reports that
as `in_power_limit_2`, and a rulebook watching this bus should gate on that
channel rather than on current. ZdriveTestbed.start() logs a warning saying so
on every run. Lower this to 8.5 A or below if you want real current limiting on
the motor rail instead."""

BRAKE_BUS = Rail(name="zdrive brake", output=1, voltage_v=24.0, current_limit_a=5.0)
"""The zdrive's brake. 24 V on output 1.

Spring-applied and fail-safe: powering this rail RELEASES the brake, and
removing power lets it grab. That is why teardown drops this rail first - see
ZdriveTestbed.stop(). 5 A at 24 V is 120 W, comfortably inside the envelope, so
this rail does get real current limiting."""

RAILS = (BRAKE_BUS, MOTOR_BUS)
"""Both rails, ordered by output number. Iterated at start() to configure
setpoints; teardown order is deliberately different and is not this tuple -
see ZdriveTestbed.stop()."""
